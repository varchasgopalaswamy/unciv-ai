"""One coordinated turn boundary, used automatically by match resume."""

import contextlib
import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from civ_agent.monitoring import MonitorStore, path_for
from civ_agent.tls import private_directory, write_private_json

HEARTBEAT_SECONDS = 3


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class TurnCoordinator:
    def __init__(self, root, operator, agents, lease):
        self.root = private_directory(Path(root) / "turn-recovery")
        self.operator, self.agents, self.lease = operator, agents, lease

    def call_agent(self, agent, operation, **fields):
        # A single service operation can outlast the host's lease (restoring
        # histories and memories, for example). Renew while it is in flight,
        # keeping all operator calls on the coordinating thread. If renewal
        # fails, finish this request before leaving the services fenced.
        self.operator.call("heartbeat", leaseId=self.lease)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="turn-recovery") as worker:
            pending = worker.submit(agent.call, operation, **fields)
            while not wait([pending], timeout=HEARTBEAT_SECONDS).done:
                self.operator.call("heartbeat", leaseId=self.lease)
            return pending.result()

    def quiesce(self):
        try:
            self._quiesce()
        except BaseException:
            with contextlib.suppress(Exception):
                self.operator.call("barrier", leaseId=self.lease)
            raise

    def _quiesce(self):
        # Stop proposal approval first; already dispatched requests may complete.
        # The model computations need not finish to reach this boundary.
        for agent in self.agents.values():
            self.call_agent(agent, "barrier", paused=True)
        deadline = time.monotonic() + 45
        while True:
            if all(self.call_agent(agent, "barrier", paused=True)["ready"] for agent in self.agents.values()):
                break
            if time.monotonic() >= deadline:
                raise ValueError("Services did not reach the checkpoint barrier; match remains paused.")
            time.sleep(0.25)
        self.operator.call("barrier", leaseId=self.lease)

    def release(self):
        for agent in self.agents.values():
            self.call_agent(agent, "barrier", paused=False)
        self.operator.call("unbarrier", leaseId=self.lease)

    def capture(self, status, scheduler):
        target = self.root / "current.json"
        if target.exists():
            previous = json.loads(target.read_text())
            if (previous["epoch"], previous["handoff"]) == (status["epoch"], status["handoff"]):
                return
        id = status["epoch"] + "-handoff-" + str(status["handoff"]) + "-" + uuid.uuid4().hex[:8]
        self.quiesce()
        try:
            host = self.operator.call("snapshot", leaseId=self.lease)["checkpoint"]
            components = {}
            for civilization, agent in self.agents.items():
                components[civilization] = self.call_agent(agent, "snapshot", id=id)
            # Publication is the commit point: partial components are deliberately
            # not discoverable as restore points without this complete manifest.
            write_private_json(
                target,
                {
                    "id": id,
                    "turn": status["turn"],
                    "handoff": status["handoff"],
                    "epoch": status["epoch"],
                    "host": host,
                    "hostSha256": digest(host),
                    "players": components,
                    "scheduler": scheduler,
                },
            )
            for agent in self.agents.values():
                self.call_agent(agent, "retain_snapshot", id=id)
        except BaseException:
            # Leave every service fenced on failure. Explicit resume can retry.
            raise
        else:
            self.release()

    def restore(self, manifest, scheduler):
        if set(manifest["players"]) != set(self.agents) or digest(manifest["host"]) != manifest["hostSha256"]:
            raise ValueError("Checkpoint verification failed")
        journal = self.root / "restore.json"
        previous = json.loads(journal.read_text()) if journal.exists() else None
        if previous and not previous.get("complete") and previous["checkpoint"] != manifest["id"]:
            raise ValueError("Turn recovery refers to another checkpoint")
        epoch = (
            previous["epoch"]
            if previous and previous["checkpoint"] == manifest["id"] and not previous.get("complete")
            else "g" + uuid.uuid4().hex[:16]
        )
        self.quiesce()
        if not previous or previous.get("complete"):
            write_private_json(
                journal,
                {
                    "checkpoint": manifest["id"],
                    "epoch": epoch,
                    "complete": False,
                    "usage": scheduler.get("usage", {}),
                    "invocations": scheduler.get("invocations", 0),
                },
            )
        # Verify all remote components before replacing any component.
        for civilization, agent in self.agents.items():
            checked = self.call_agent(agent, "verify_snapshot", **manifest["players"][civilization])
            if not checked.get("valid"):
                raise ValueError("A civilization checkpoint failed verification")
        for agent in self.agents.values():
            self.call_agent(agent, "cancel_jobs")
        abandoned = self.root / ("abandoned-" + epoch + ".json")
        if not abandoned.exists():
            host = self.operator.call("snapshot", leaseId=self.lease)["checkpoint"]
            write_private_json(abandoned, {"host": host, "scheduler": scheduler})
        restored = self.operator.call("restore", leaseId=self.lease, epoch=epoch, checkpoint=manifest["host"])["status"]
        for civilization, agent in self.agents.items():
            component = manifest["players"][civilization]
            result = self.call_agent(agent, "restore", id=component["id"], sha256=component["sha256"], epoch=epoch)
            if result.get("epoch") != epoch or not result.get("restored"):
                raise ValueError("Civilization restoration is incomplete; match remains paused")
        credentials = self.operator.call("rotate_players", leaseId=self.lease, epoch=epoch)["players"]
        controllers = restored["playerControllers"]
        if {player["civilizationId"]: player["controller"] for player in credentials} != controllers or {
            name for name, role in controllers.items() if role == "AGENT"
        } != set(self.agents):
            raise ValueError("Credential rotation is incomplete; match remains paused")
        write_private_json(self.root.parent / "players.json", {"players": credentials})
        # Accounting and time budgets survive; fresh jobs get only restored memory.
        boundary = manifest["scheduler"]
        scheduler.update(
            epoch=epoch,
            initialTurn=min(scheduler["initialTurn"], restored["turn"]),
            inFlight=None,
            pendingFinalizations=[{**pending, "epoch": epoch} for pending in boundary.get("pendingFinalizations", [])],
            sessions={name: {**session, "threadId": None} for name, session in boundary.get("sessions", {}).items()},
            handoffs=boundary.get("handoffs", 0),
            reviewedBoundary=boundary["reviewedBoundary"],
            reviewAnchors=boundary.get("reviewAnchors", {}),
            reviewedWarTurn=boundary.get("reviewedWarTurn", -1),
            status="paused",
            reason="Interrupted turn restored to its starting state",
        )
        scheduler.pop("reviewPhase", None)
        # Persist the scheduler before publishing completion, so a crash cannot
        # leave its old epoch alongside an apparently completed restoration.
        MonitorStore(path_for(self.root.parent)).put("state", "run", scheduler)
        self.operator.call("restore_complete", leaseId=self.lease, epoch=epoch)
        # Keep this same starting position until the handoff commits. Repeated
        # interruptions must never replace it with a partially played turn.
        host = {**manifest["host"], "epoch": epoch}
        write_private_json(
            self.root / "current.json",
            {**manifest, "epoch": epoch, "host": host, "hostSha256": digest(host), "scheduler": scheduler},
        )
        write_private_json(journal, {"checkpoint": manifest["id"], "epoch": epoch, "complete": True})
        # Deliberately stay paused. The normal resume command reconnects services
        # under the new epoch, then releases the distributed barrier.
        return restored

    def recover(self, status, scheduler):
        path = self.root / "current.json"
        journal = self.root / "restore.json"
        pending = json.loads(journal.read_text()) if journal.exists() else {}
        if not path.exists():
            if scheduler.get("inFlight") or pending and not pending.get("complete"):
                raise ValueError("Interrupted turn has no recovery checkpoint")
            return status
        saved = json.loads(path.read_text())
        if pending and not pending.get("complete"):
            return self.restore(saved, scheduler)
        if status["phase"] == "FINISHED" or status["handoff"] > saved["handoff"]:
            # A committed handoff stands. Earlier event streams remain indexed
            # under their original epochs; no directories need to be moved.
            return status
        if status["handoff"] != saved["handoff"] or status["epoch"] != saved["epoch"]:
            raise ValueError("Turn checkpoint does not match the interrupted game")
        return self.restore(saved, scheduler)
