import copy
import hashlib
import json
import threading

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.match_recovery import TurnCoordinator
from civ_agent.monitoring import path_for, read_collection
from civ_agent.protocol import JsonObject
from civ_agent.runner_history import EventHistory
from civ_agent.scheduler import OperatorError


class Host:
    def __init__(self):
        self.world = {"epoch": "g1", "gold": 100, "turn": 0, "handoff": 0}
        self.paused = False
        self.calls = []
        self.controllers = {"Rome": "AGENT", "Greece": "AGENT"}

    def call(self, op, **fields):
        self.calls.append(op)
        if op == "barrier":
            self.paused = True
        elif op == "unbarrier":
            self.paused = False
        elif op == "snapshot":
            assert self.paused
            return {"checkpoint": copy.deepcopy(self.world)}
        elif op == "restore":
            assert self.paused
            self.world = {**copy.deepcopy(fields["checkpoint"]), "epoch": fields["epoch"]}
            return {
                "status": {
                    "epoch": fields["epoch"],
                    "turn": self.world["turn"],
                    "handoff": self.world["handoff"],
                    "phase": "PAUSED",
                    "playerControllers": dict(self.controllers),
                }
            }
        elif op == "rotate_players":
            return {
                "players": [
                    {"civilizationId": name, "token": name + "-new-credential", "controller": role}
                    for name, role in self.controllers.items()
                ]
            }
        return {}


class Player:
    def __init__(self, root, name):
        self.root, self.name = root, name
        self.memory = CivilizationState(root, name)
        self.memory.bind("g1")
        self.fail = None
        self.calls = []

    def call(self, op, **fields):
        self.calls.append(op)
        if op == self.fail:
            self.fail = None
            raise OSError("Injected crash")
        if op == "barrier":
            self.memory.pause(fields["paused"])
            return {"ready": True}
        if op == "snapshot":
            path = self.root / (fields["id"] + ".json")
            return {"id": fields["id"], "sha256": self.memory.backup(path), "civilization": self.name}
        if op == "verify_snapshot":
            path = self.root / (fields["id"] + ".json")
            return {"valid": hashlib.sha256(path.read_bytes()).hexdigest() == fields["sha256"]}
        if op == "restore":
            interrupted = self.memory.restore(self.root / (fields["id"] + ".json"), fields["sha256"], fields["epoch"])
            return {"restored": True, "epoch": fields["epoch"], "pendingReviews": interrupted}
        return {}


def setup(tmp_path):
    host = Host()
    players = {name: Player(tmp_path / name, name) for name in ("Rome", "Greece")}
    state: JsonObject = {
        "epoch": "g1",
        "initialTurn": 0,
        "reviewedBoundary": 0,
        "usage": {"input_tokens": 700},
        "invocations": 4,
    }
    coordinator = TurnCoordinator(tmp_path, host, players, "lease")
    return host, players, state, coordinator


def test_partial_capture_is_not_restorable_and_retry_uses_fresh_components(tmp_path):
    host, players, state, coordinator = setup(tmp_path)
    players["Greece"].fail = "snapshot"
    with pytest.raises(OSError):
        coordinator.capture({"epoch": "g1", "turn": 0, "handoff": 0}, state)
    assert not list(coordinator.root.glob("current.json")) and host.paused
    first_components = list(players["Rome"].root.glob("*.json"))
    coordinator.capture({"epoch": "g1", "turn": 0, "handoff": 0}, state)
    manifest = json.loads(next(coordinator.root.glob("current.json")).read_text())
    assert manifest["players"]["Rome"]["id"] != first_components[0].stem
    assert not host.paused


def test_human_turn_rewinds_without_a_human_agent_store_and_preserves_credentials(tmp_path):
    host, players, state, coordinator = setup(tmp_path)
    host.controllers["France"] = "HUMAN"
    coordinator.capture({"epoch": "g1", "turn": 0, "handoff": 0}, state)
    manifest = json.loads((coordinator.root / "current.json").read_text())
    assert set(manifest["players"]) == {"Rome", "Greece"}
    host.world["gold"] = 5
    state["inFlight"] = {"civilization": "France", "controller": "HUMAN", "handoff": 0}
    coordinator.restore(manifest, state)
    assert host.world["gold"] == 100 and state["inFlight"] is None
    credentials = json.loads((tmp_path / "players.json").read_text())["players"]
    assert {p["civilizationId"]: p["controller"] for p in credentials} == host.controllers


def test_partial_restore_remains_paused_then_repeats_same_epoch_without_refunding_usage(tmp_path):
    host, players, state, coordinator = setup(tmp_path)
    state["reviewAnchors"] = {"Rome": 5, "Greece": 5}
    state["reviewedWarTurn"] = 5
    for player in players.values():
        player.memory.start_job("initial", "gameplay", {})
        player.memory.patch("initial", [{"op": "create", "content": {"type": "note", "text": "Build peacefully"}}])
        from civ_agent.review_store import ReviewStore

        reviews = ReviewStore(player.memory)
        reviews.prepare("initial", {"turn": 5, "position": "Before capture"}, {}, 60)
    coordinator.capture({"epoch": "g1", "turn": 0, "handoff": 0}, state)
    manifest = json.loads(next(coordinator.root.glob("current.json")).read_text())
    host.world["gold"] = 999
    state["reviewAnchors"] = {"Rome": 9, "Greece": 9}
    state["reviewedWarTurn"] = 9
    for player in players.values():
        player.memory.patch("initial", [{"op": "amend", "id": "mem1", "fields": {"text": "Invade"}}])
        player.memory.ingest(
            "g1", [{"id": 1, "kind": "message", "wake": True, "body": {"text": "Abandoned future"}}], 1
        )
        player.memory.finish_job("initial", True)
    players["Greece"].fail = "restore"
    history = path_for(tmp_path)
    with pytest.raises(OSError):
        coordinator.restore(manifest, state)
    epoch = json.loads((coordinator.root / "restore.json").read_text())["epoch"]
    assert host.paused and all(player.memory.is_paused() for player in players.values())
    assert state["usage"] == {"input_tokens": 700}
    # Shutdown after a failed restore can remirror the old activation. It must
    # not survive the next attempt merely because the first archive exists.
    old_mirror = EventHistory(history, "g1", "Rome", "old", game_turn=0)
    old_mirror.append([{"sequence": 0, "kind": "assistant_message", "data": {"text": "Old attempt"}}])
    coordinator.restore(manifest, state)
    assert state["epoch"] == epoch and state["invocations"] == 4
    assert state["reviewAnchors"] == {"Rome": 5, "Greece": 5} and state["reviewedWarTurn"] == 5
    assert state["pendingFinalizations"] == []
    assert all(ReviewStore(player.memory).pending() is not None for player in players.values())
    assert host.world["gold"] == 100 and host.paused
    assert old_mirror.events[0]["data"]["text"] == "Old attempt"
    assert read_collection(history, "streams")[0]["epoch"] == "g1"
    EventHistory(history, epoch, "Rome", "fresh", game_turn=0)
    for player in players.values():
        snapshot = player.memory.snapshot("gameplay")
        assert snapshot["records"]["mem1"]["body"]["text"] == "Build peacefully"
        assert not snapshot["events"] and snapshot["epoch"] == epoch
        assert "cancel_jobs" in player.calls
    assert json.loads((coordinator.root / "restore.json").read_text())["complete"]


def test_committed_turn_keeps_its_effects_and_epoch_separates_obsolete_history(tmp_path):
    host, players, state, coordinator = setup(tmp_path)
    coordinator.capture({"epoch": "g1", "turn": 0, "handoff": 0}, state)
    root = path_for(tmp_path)
    obsolete = EventHistory(root, "old-epoch", "Rome", "abandoned", game_turn=0)
    obsolete.append([{"sequence": 0, "kind": "assistant_message", "data": {"text": "Old attempt"}}])
    valid = EventHistory(root, "g1", "Greece", "committed", game_turn=0)
    valid.append([{"sequence": 0, "kind": "assistant_message", "data": {"text": "Completed turn"}}])
    original = valid.events
    host.world.update(handoff=1, gold=150)
    status = {**host.world, "phase": "PAUSED"}
    assert coordinator.recover(status, state) == status
    assert host.world["gold"] == 150 and "restore" not in host.calls
    assert valid.events == original
    assert obsolete.events[0]["data"]["text"] == "Old attempt"
    EventHistory(root, "g1", "Rome", "new-attempt", game_turn=0)


@pytest.mark.parametrize(
    "operation", ["barrier", "snapshot", "retain_snapshot", "verify_snapshot", "cancel_jobs", "restore"]
)
def test_lease_is_renewed_while_a_service_operation_is_blocked(tmp_path, monkeypatch, operation):
    host, players, state, coordinator = setup(tmp_path)
    coordinator.capture({"epoch": "g1", "turn": 0, "handoff": 0}, state)
    manifest = json.loads((coordinator.root / "current.json").read_text())
    monkeypatch.setattr("civ_agent.match_recovery.HEARTBEAT_SECONDS", 0.01)
    blocked = threading.Event()
    renewed = threading.Event()
    heartbeats = []
    host_call = host.call
    player_call = players["Rome"].call
    coordinating_thread = threading.get_ident()

    def operator(op, **fields):
        assert threading.get_ident() == coordinating_thread
        if op == "heartbeat" and blocked.is_set():
            heartbeats.append(op)
            if len(heartbeats) >= 2:
                renewed.set()
        return host_call(op, **fields)

    def player(op, **fields):
        if op == operation:
            blocked.set()
            assert renewed.wait(5), "Service operation blocked without lease renewal"
        return player_call(op, **fields)

    monkeypatch.setattr(host, "call", operator)
    monkeypatch.setattr(players["Rome"], "call", player)
    if operation in {"barrier", "snapshot", "retain_snapshot"}:
        coordinator.capture({"epoch": "g1", "turn": 1, "handoff": 1}, state)
        assert not host.paused
    else:
        coordinator.restore(manifest, state)
        assert json.loads((coordinator.root / "restore.json").read_text())["complete"]
    assert len(heartbeats) >= 2


def test_lease_loss_during_restore_stops_before_next_player_and_resume_retries(tmp_path, monkeypatch):
    host, players, state, coordinator = setup(tmp_path)
    coordinator.capture({"epoch": "g1", "turn": 0, "handoff": 0}, state)
    manifest = json.loads((coordinator.root / "current.json").read_text())
    monkeypatch.setattr("civ_agent.match_recovery.HEARTBEAT_SECONDS", 0.01)
    blocked = threading.Event()
    expired = threading.Event()
    host_call = host.call
    player_call = players["Rome"].call

    def operator(op, **fields):
        if op == "heartbeat" and blocked.is_set():
            expired.set()
            raise OperatorError("Operator revoked lease", "LEASE_LOST", 409)
        return host_call(op, **fields)

    def player(op, **fields):
        if op == "restore":
            blocked.set()
            assert expired.wait(5), "Service operation blocked without lease renewal"
        return player_call(op, **fields)

    monkeypatch.setattr(host, "call", operator)
    monkeypatch.setattr(players["Rome"], "call", player)
    with pytest.raises(OperatorError, match="Operator revoked lease"):
        coordinator.restore(manifest, state)
    journal = json.loads((coordinator.root / "restore.json").read_text())
    assert not journal["complete"]
    assert host.paused and all(player.memory.is_paused() for player in players.values())
    assert "restore" in players["Rome"].calls and "restore" not in players["Greece"].calls
    assert "restore_complete" not in host.calls and "rotate_players" not in host.calls

    monkeypatch.setattr(host, "call", host_call)
    monkeypatch.setattr(players["Rome"], "call", player_call)
    coordinator.restore(manifest, state)
    assert state["epoch"] == journal["epoch"]
    assert json.loads((coordinator.root / "restore.json").read_text())["complete"]
