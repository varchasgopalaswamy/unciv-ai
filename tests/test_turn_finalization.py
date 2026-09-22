"""Handoff proof, overlapping archive work, and retry without replaying gameplay."""

import copy
import threading
import time
from types import SimpleNamespace

import pytest

from civ_agent.scheduler import RunnerError, Scheduler


class Operator:
    host_url = "https://localhost:8765"
    token = "private-operator"

    def __init__(self):
        self.handoff = 0
        self.activated = []

    def status(self):
        return {
            "epoch": "g1",
            "handoff": self.handoff,
            "turn": self.handoff // 2,
            "serverInstance": "server",
            "phase": "ACTIVE",
            "activeCivilization": ("Rome", "Greece")[self.handoff % 2],
        }

    def call(self, operation, **fields):
        if operation == "activate":
            self.activated.append(fields["civilization"])
            return {"status": self.status(), "playerToken": "P" * 43}
        return {"status": self.status()}


class Agent:
    TERMINAL = {"COMPLETED", "INTERRUPTED", "FAILED"}

    def __init__(self, civilization, operator):
        self.url = "https://localhost/" + civilization
        self.token = "private-agent"
        self.civilization, self.operator = civilization, operator
        self.rows = {}
        self.release = threading.Event()
        self.entered = threading.Event()
        self.fail = False
        self.proofs = []

    def call(self, operation, **fields):
        if operation == "health":
            return {"civilization": self.civilization, "runFailure": None}
        key = fields["activationId"]
        if operation == "start":
            self.rows[key] = {
                **{k: fields[k] for k in ("activationId", "epoch", "gameTurn", "handoff")},
                "civilization": self.civilization,
                "phase": "COMPLETED",
                "threadId": key,
                "backgroundFinalization": True,
                "usage": {"input_tokens": 10},
            }
            self.operator.handoff += 1
        row = self.rows[key]
        if operation == "finish_handoff":
            assert fields["committedHandoff"] == row["handoff"] + 1
            row["confirmedHandoff"] = fields["committedHandoff"]
        if operation == "finalize":
            self.entered.set()
            assert self.release.wait(5), "test did not release finalization"
            if self.fail:
                raise RunnerError("archive temporarily unavailable")
            self.proofs.append(fields["committedHandoff"])
            row["committedHandoff"] = fields["committedHandoff"]
        return copy.deepcopy(row)

    def events(self, activation, cursor):
        return {"available": True, "events": [], "nextCursor": cursor, "hasMore": False}

    def archive(self, activation):
        return {"gameTurn": self.rows[activation]["gameTurn"]}


def make_scheduler(tmp_path, state=None, operator=None, agents=None):
    saved = []
    store = SimpleNamespace(
        root=tmp_path, save=lambda value: saved.append(copy.deepcopy(value)), requested=lambda: None
    )
    operator = operator or Operator()
    agents = agents or {civ: Agent(civ, operator) for civ in ("Rome", "Greece")}
    state = state or {
        "epoch": "g1",
        "sessions": {},
        "inFlight": None,
        "invocations": 0,
        "handoffs": 0,
        "usage": {},
        "deadline": time.time() + 60,
        "limits": {"activationSeconds": 20},
    }
    scheduler = Scheduler(store, operator, agents, state)
    published = []
    scheduler.history_for = lambda *args, **kwargs: SimpleNamespace(
        cursor=0,
        append=lambda events: None,
        output=SimpleNamespace(finish_line=lambda: None, line=lambda *args: None),
        publish_archive=published.append,
    )
    return scheduler, saved, published


def test_next_player_plays_while_previous_archive_is_held(tmp_path):
    scheduler, saved, published = make_scheduler(tmp_path)
    try:
        assert scheduler.invoke(scheduler.operator.status())
        assert scheduler.agents["Rome"].entered.wait(2)
        assert not scheduler.agents["Rome"].release.is_set()
        assert saved[-1]["pendingFinalizations"][0]["confirmedHandoff"] == 1
        assert saved[-1]["inFlight"] is None
        assert scheduler.invoke(scheduler.operator.status())
        assert scheduler.operator.activated == ["Rome", "Greece"]
        assert not published
        assert len(scheduler.state["pendingFinalizations"]) == 2
        # Waiting for Rome's next turn must wait for Rome's own archive.
        waited = threading.Event()
        waiter = threading.Thread(
            target=lambda: (scheduler.collect_finalizations(wait=True, civilization="Rome"), waited.set())
        )
        waiter.start()
        assert not waited.wait(0.05)
        scheduler.agents["Rome"].release.set()
        waiter.join(2)
        assert waited.is_set()
        assert scheduler.state["sessions"]["Rome"]["completedTurn"] == 0
        scheduler.agents["Greece"].release.set()
        scheduler.collect_finalizations(wait=True)
        assert scheduler.state["pendingFinalizations"] == []
        assert scheduler.state["handoffs"] == 2
        assert scheduler.state["usage"]["input_tokens"] == 20
        assert len(published) == 2
    finally:
        for agent in scheduler.agents.values():
            agent.release.set()
        scheduler.finalizer.shutdown(wait=True)


def test_restart_retries_original_proof_after_later_handoffs(tmp_path):
    scheduler, _, _ = make_scheduler(tmp_path)
    rome = scheduler.agents["Rome"]
    rome.fail = True
    rome.release.set()
    try:
        assert scheduler.invoke(scheduler.operator.status())
        with pytest.raises(RunnerError, match="bookkeeping remains pending"):
            scheduler.collect_finalizations(wait=True)
        state = copy.deepcopy(scheduler.state)
    finally:
        scheduler.finalizer.shutdown(wait=True)
    scheduler.operator.handoff = 2
    rome.fail = False
    resumed, _, published = make_scheduler(tmp_path, state, scheduler.operator, scheduler.agents)
    try:
        resumed.check_status(scheduler.operator.status())
        resumed.collect_finalizations(wait=True)
        assert rome.proofs == [1]
        assert len(published) == 1
        assert resumed.state["pendingFinalizations"] == []
        assert resumed.state["handoffs"] == 1
        assert resumed.operator.activated == ["Rome"]
        assert resumed.state["usage"]["input_tokens"] == 10
    finally:
        resumed.finalizer.shutdown(wait=True)


def test_restored_epoch_cannot_publish_a_pending_old_archive(tmp_path):
    scheduler, _, _ = make_scheduler(tmp_path)
    scheduler.state["pendingFinalizations"] = [{"epoch": "old", "confirmedHandoff": 1, "activationId": "abandoned"}]
    try:
        with pytest.raises(RunnerError, match="current host history"):
            scheduler.check_status(scheduler.operator.status())
        with pytest.raises(RunnerError, match="abandoned epoch"):
            scheduler.start_finalizations()
        assert not scheduler.finalizations
    finally:
        scheduler.finalizer.shutdown(wait=True)
