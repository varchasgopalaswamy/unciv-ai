"""Coordinated round boundaries and explicit whole-match emergency restoration."""

import argparse
import contextlib
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path

from civ_agent.tls import private_directory, write_private_json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RoundCoordinator:
    def __init__(self, root, operator, agents, lease):
        self.root = private_directory(Path(root) / "round-checkpoints")
        self.operator, self.agents, self.lease = operator, agents, lease

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
            agent.call("barrier", paused=True)
        deadline = time.monotonic() + 45
        while True:
            self.operator.call("heartbeat", leaseId=self.lease)
            if all(agent.call("barrier", paused=True)["ready"] for agent in self.agents.values()):
                break
            if time.monotonic() >= deadline:
                raise ValueError("Services did not reach the checkpoint barrier; match remains paused.")
            time.sleep(0.25)
        self.operator.call("barrier", leaseId=self.lease)

    def release(self):
        for agent in self.agents.values():
            agent.call("barrier", paused=False)
        self.operator.call("unbarrier", leaseId=self.lease)

    def capture(self, status, scheduler):
        id = status["epoch"] + "-round-" + str(status["turn"])
        target = self.root / (id + ".json")
        if target.exists():
            return
        self.quiesce()
        try:
            host = self.operator.call("snapshot", leaseId=self.lease)["checkpoint"]
            components = {}
            capture_id = id + "-" + uuid.uuid4().hex[:8]
            for civilization, agent in self.agents.items():
                self.operator.call("heartbeat", leaseId=self.lease)
                components[civilization] = agent.call("snapshot", id=capture_id)
            # Publication is the commit point: partial components are deliberately
            # not discoverable as restore points without this complete manifest.
            write_private_json(
                target,
                {
                    "id": id,
                    "turn": status["turn"],
                    "epoch": status["epoch"],
                    "host": host,
                    "hostSha256": digest(host),
                    "players": components,
                    "scheduler": scheduler,
                },
            )
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
            raise ValueError("Finish the pending restore before selecting another checkpoint")
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
            checked = agent.call("verify_snapshot", **manifest["players"][civilization])
            if not checked.get("valid"):
                raise ValueError("A civilization checkpoint failed verification")
        for agent in self.agents.values():
            agent.call("cancel_jobs")
        abandoned = self.root / ("abandoned-" + epoch + ".json")
        if not abandoned.exists():
            host = self.operator.call("snapshot", leaseId=self.lease)["checkpoint"]
            write_private_json(abandoned, {"host": host, "scheduler": scheduler})
        history = self.root.parent / "runner" / "history"
        history_archive = self.root / ("abandoned-" + epoch) / "history"
        if history.exists() and not history_archive.exists():
            private_directory(history_archive.parent)
            shutil.move(history, history_archive)
        restored = self.operator.call("restore", leaseId=self.lease, epoch=epoch, checkpoint=manifest["host"])["status"]
        for civilization, agent in self.agents.items():
            self.operator.call("heartbeat", leaseId=self.lease)
            component = manifest["players"][civilization]
            result = agent.call("restore", id=component["id"], sha256=component["sha256"], epoch=epoch)
            if result.get("epoch") != epoch or not result.get("restored"):
                raise ValueError("Civilization restoration is incomplete; match remains paused")
        credentials = self.operator.call("rotate_players", leaseId=self.lease, epoch=epoch)["players"]
        if {player["civilizationId"] for player in credentials} != set(self.agents):
            raise ValueError("Credential rotation is incomplete; match remains paused")
        write_private_json(self.root.parent / "players.json", {"players": credentials})
        # Accounting and time budgets survive; fresh jobs get only restored memory.
        scheduler.update(
            epoch=epoch,
            initialTurn=min(scheduler["initialTurn"], restored["turn"]),
            inFlight=None,
            sessions={},
            handoffs=manifest["scheduler"].get("handoffs", 0),
            status="paused",
            reason="Emergency round restore completed",
        )
        # Persist the scheduler before publishing completion, so a crash cannot
        # leave its old epoch alongside an apparently completed restoration.
        write_private_json(self.root.parent / "runner" / "run.json", scheduler)
        self.operator.call("restore_complete", leaseId=self.lease, epoch=epoch)
        write_private_json(journal, {"checkpoint": manifest["id"], "epoch": epoch, "complete": True})
        # Deliberately stay paused. The normal resume command reconnects services
        # under the new epoch, then releases the distributed barrier.
        return restored


def parser(*, add_help=True):
    result = argparse.ArgumentParser(add_help=add_help)
    result.add_argument("--state-dir", type=Path, required=True)
    result.add_argument("--round", type=int, required=True, help="Number shown by player_turn")
    return result


def run(args):
    from civ_agent.scheduler import Operator, RunnerError, RunStore, agent_services
    from civ_agent.tls import endpoint_configuration, read_json

    root = args.state_dir.expanduser().resolve()
    try:
        endpoint, ca = endpoint_configuration(root, None, None)
        token = read_json(root / "operator.json")["token"]
        operator = Operator(endpoint, token, ca, timeout=60)
        store = RunStore(root / "runner")
        store.acquire()
        try:
            scheduler = store.load()
            lease = str(uuid.uuid4())
            operator.call("acquire", leaseId=lease)
            coordinator = RoundCoordinator(root, operator, agent_services(root), lease)
            checkpoints = [json.loads(path.read_text()) for path in coordinator.root.glob("*-round-*.json")]
            selected = [
                value for value in checkpoints if value["turn"] == args.round and value["epoch"] == scheduler["epoch"]
            ]
            if not selected:
                # A repeated interrupted restore can already have rotated the host epoch.
                journal = coordinator.root / "restore.json"
                pending = json.loads(journal.read_text()) if journal.exists() else {}
                selected = [
                    value
                    for value in checkpoints
                    if value["id"] == pending.get("checkpoint") and not pending.get("complete")
                ]
            if len(selected) != 1:
                raise ValueError("No unique completed checkpoint for that round")
            coordinator.restore(selected[0], scheduler)
            store.save(scheduler)
            operator.call("release", leaseId=lease)
            print(f"Restored all civilizations to round {args.round}. Match is paused; run match resume to continue.")
        finally:
            store.close()
        return 0
    except (OSError, ValueError, RunnerError) as error:
        print(f"Emergency restore stopped: {error}")
        return 2
