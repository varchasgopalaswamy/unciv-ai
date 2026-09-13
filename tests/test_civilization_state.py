import json

import pytest

from civ_agent.civilization_state import CivilizationState, StateConflict


def state(tmp_path):
    result = CivilizationState(tmp_path / "private", "Rome")
    result.bind("g1")
    return result


def test_concurrent_memory_updates_preserve_independent_results(tmp_path):
    memory = state(tmp_path)
    memory.start_job("planning", "gameplay", {})
    memory.start_job("chat", "communication", {})

    def patch(key, text):
        return {"key": key, "category": "plan", "body": {"text": text}}

    memory.patch("chat", [patch("STRATEGY", "Peace accepted; redeploy to the west")])
    result = memory.patch("planning", [patch("STRATEGY", "Attack Greece"), patch("economy", "Build a market")])
    assert [entry["accepted"] for entry in result["changes"]] == [False, True]
    assert memory.snapshot("gameplay")["records"]["STRATEGY"]["body"]["text"].startswith("Peace accepted")


def test_misleading_speech_cannot_replace_observation_and_cursor_is_durable(tmp_path):
    memory = state(tmp_path)
    memory.observe("border", {"turn": 4, "source": "player_turn", "unit": "Greek warrior"})
    events = [
        {"id": 1, "kind": "message", "wake": True, "body": {"author": "Greece", "text": "My army is nowhere near you"}}
    ]
    memory.ingest("g1", events, 1)
    memory.ingest("g1", events, 1)
    restored = CivilizationState(tmp_path / "private", "Rome")
    snapshot = restored.snapshot("communication")
    assert restored.cursor() == 1 and len(snapshot["events"]) == 1
    assert snapshot["records"]["event:1"]["category"] == "statement"
    assert snapshot["records"]["observation:border"]["body"]["unit"] == "Greek warrior"
    restored.start_job("chat", "communication", {})
    with pytest.raises(ValueError):
        restored.patch("chat", [{"key": "observation:border", "category": "observation", "body": {"text": "No army"}}])


def test_outgoing_identity_exact_retry_and_crash_recovery(tmp_path):
    memory = state(tmp_path)
    memory.start_job("job", "gameplay", {}, game_turn=0)
    request = memory.queue("job", "player_command", {"kind": "MOVE_UNIT", "target": "unit1"}, {}, "call1")
    retry = memory.queue("job", "player_command", {"kind": "MOVE_UNIT", "target": "unit1"}, {}, "call1")
    assert request["id"] == retry["id"]
    with pytest.raises(StateConflict):
        memory.queue("job", "player_command", {"kind": "END_TURN"}, {}, "call1")
    restarted = CivilizationState(tmp_path / "private", "Rome")
    assert restarted.pending()[0]["id"] == request["id"]
    with pytest.raises(StateConflict):
        restarted.patch("job", [])
    restarted.receipt(request["id"], {"kind": "MOVE_UNIT", "moved": True})
    assert not restarted.pending()
    workspace = tmp_path / "view"
    workspace.mkdir(mode=0o700)
    restarted.export(workspace)
    exported = json.loads((workspace / "turn_0" / "receipts.jsonl").read_text())
    assert exported["receipt"] == {"kind": "MOVE_UNIT", "moved": True}
    assert not (workspace / "turn_1").exists()


def test_round_restore_fences_jobs_and_regenerates_projections(tmp_path):
    memory = state(tmp_path)
    memory.start_job("job", "gameplay", {})
    memory.patch("job", [{"key": "SOUL", "category": "policy", "body": {"text": "Develop peacefully"}}])
    path = tmp_path / "rounds" / "one.sqlite3"
    checksum = memory.backup(path)
    memory.patch("job", [{"key": "SOUL", "category": "policy", "body": {"text": "Invade"}}])
    memory.ingest("g1", [{"id": 1, "kind": "message", "body": {"text": "Abandoned future"}}], 1)
    memory.restore(path, checksum, "g2")
    memory.pause(False)
    workspace = tmp_path / "view"
    workspace.mkdir(mode=0o700)
    memory.export(workspace)
    assert (workspace / "SOUL.md").read_text().strip() == "Develop peacefully"
    assert (workspace / "chat.jsonl").read_text() == ""
    with pytest.raises(StateConflict):
        memory.queue("job", "player_command", {}, {}, "late")
    memory.start_job("new", "gameplay", {})
    assert memory.snapshot("gameplay")["epoch"] == "g2"
    assert json.loads(memory.queue("new", "chat", {"text": "Hello"}, {}, "c")["payload"])["text"] == "Hello"


def test_history_search_and_newly_read_events_survive_job_completion(tmp_path):
    memory = state(tmp_path)
    memory.start_job("chat", "communication", {})
    events = [
        {
            "id": number,
            "kind": "message",
            "wake": True,
            "body": {"participants": ["Rome", other], "text": text},
        }
        for number, other, text in ((1, "Greece", "Offer 20%"), (2, "Egypt", "Offer 200"))
    ]
    memory.ingest("g1", events, 2)
    assert memory.chat_history("Greece")["events"] == events[:1]
    assert memory.chat_history(query="20%")["events"] == events[:1]  # Literal search, not SQL wildcards.
    # A job dispatched before these messages must explicitly read them before
    # claiming to have handled them or using them as sources for beliefs.
    memory.refresh_memory("chat", ["event:1", "event:2"], cursor=2)
    memory.finish_job("chat", True)
    assert not memory.has_wake()
    assert not memory.snapshot("communication")["events"]
    assert memory.chat_history("Egypt")["events"] == events[1:]
