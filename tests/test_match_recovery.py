import copy
import hashlib
import json

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.match_recovery import RoundCoordinator


class Host:
    def __init__(self):
        self.world = {"epoch": "g1", "gold": 100, "turn": 0}
        self.paused = False
        self.calls = []

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
            return {"status": {"epoch": fields["epoch"], "turn": self.world["turn"]}}
        elif op == "rotate_players":
            return {
                "players": [{"civilizationId": name, "token": name + "-new-credential"} for name in ("Rome", "Greece")]
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
            self.memory.restore(self.root / (fields["id"] + ".json"), fields["sha256"], fields["epoch"])
            return {"restored": True, "epoch": fields["epoch"]}
        return {}


def setup(tmp_path):
    host = Host()
    players = {name: Player(tmp_path / name, name) for name in ("Rome", "Greece")}
    state = {"epoch": "g1", "initialTurn": 0, "usage": {"input_tokens": 700}, "invocations": 4}
    coordinator = RoundCoordinator(tmp_path, host, players, "lease")
    return host, players, state, coordinator


def test_partial_capture_is_not_restorable_and_retry_uses_fresh_components(tmp_path):
    host, players, state, coordinator = setup(tmp_path)
    players["Greece"].fail = "snapshot"
    with pytest.raises(OSError):
        coordinator.capture({"epoch": "g1", "turn": 0}, state)
    assert not list(coordinator.root.glob("*-round-*.json")) and host.paused
    first_components = list(players["Rome"].root.glob("*.json"))
    coordinator.capture({"epoch": "g1", "turn": 0}, state)
    manifest = json.loads(next(coordinator.root.glob("*-round-*.json")).read_text())
    assert manifest["players"]["Rome"]["id"] != first_components[0].stem
    assert not host.paused


def test_partial_restore_remains_paused_then_repeats_same_epoch_without_refunding_usage(tmp_path):
    host, players, state, coordinator = setup(tmp_path)
    for player in players.values():
        player.memory.start_job("initial", "gameplay", {})
        player.memory.patch("initial", [{"op": "create", "content": {"type": "soul", "text": "Build peacefully"}}])
    coordinator.capture({"epoch": "g1", "turn": 0}, state)
    manifest = json.loads(next(coordinator.root.glob("*-round-*.json")).read_text())
    host.world["gold"] = 999
    for player in players.values():
        player.memory.patch("initial", [{"op": "revise", "id": "mem1", "content": {"type": "soul", "text": "Invade"}}])
        player.memory.ingest(
            "g1", [{"id": 1, "kind": "message", "wake": True, "body": {"text": "Abandoned future"}}], 1
        )
    players["Greece"].fail = "restore"
    history = coordinator.root.parent / "runner" / "history"
    history.mkdir(parents=True)
    (history / "old-timeline.txt").write_text("Private messages from the abandoned epoch")
    with pytest.raises(OSError):
        coordinator.restore(manifest, state)
    epoch = json.loads((coordinator.root / "restore.json").read_text())["epoch"]
    assert host.paused and all(player.memory.is_paused() for player in players.values())
    assert state["usage"] == {"input_tokens": 700}
    coordinator.restore(manifest, state)
    assert state["epoch"] == epoch and state["invocations"] == 4
    assert state["pendingFinalizations"] == []
    assert host.world["gold"] == 100 and host.paused
    assert not history.exists()
    assert (coordinator.root / ("abandoned-" + epoch) / "history" / "old-timeline.txt").exists()
    for player in players.values():
        snapshot = player.memory.snapshot("gameplay")
        assert snapshot["records"]["mem1"]["body"]["text"] == "Build peacefully"
        assert not snapshot["events"] and snapshot["epoch"] == epoch
        assert "cancel_jobs" in player.calls
    assert json.loads((coordinator.root / "restore.json").read_text())["complete"]
