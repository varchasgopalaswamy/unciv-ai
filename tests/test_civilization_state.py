import json

import pytest

from civ_agent.civilization_state import CivilizationState, StateConflict
from tests.memory_helpers import stored_records


def state(tmp_path):
    result = CivilizationState(tmp_path / "private", "Rome")
    result.bind("g1")
    return result


def test_communication_preserves_notes_without_overwriting_the_writer(tmp_path):
    memory = state(tmp_path)
    memory.start_job("planning", "gameplay", {})
    content = {
        "type": "strategy",
        "assessment": "Attack Greece",
        "reasons": ["Advance"],
        "reconsiderWhen": ["Capture a city"],
    }
    memory.patch("planning", [{"op": "create", "content": content}])
    memory.start_job("chat", "communication", {})
    result = memory.patch(
        "chat",
        [
            {"op": "amend", "id": "mem1", "fields": {"assessment": "Peace accepted"}},
            {"op": "create", "content": {"type": "note", "text": "Peace accepted; redeploy to the west"}},
        ],
    )
    assert [entry["accepted"] for entry in result["changes"]] == [False, True]
    assert memory.snapshot("gameplay")["records"]["mem1"]["body"]["assessment"] == "Attack Greece"
    assert memory.snapshot("gameplay")["records"][result["changes"][1]["id"]]["category"] == "contribution"


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
    assert "observation:border" not in snapshot["records"]
    assert stored_records(restored)["observation:border"]["body"]["unit"] == "Greek warrior"
    restored.start_job("chat", "communication", {})
    with pytest.raises(ValueError):
        restored.patch("chat", [{"key": "observation:border", "category": "observation", "body": {"text": "No army"}}])


def test_native_offers_never_wake_or_crowd_out_player_conversations(tmp_path):
    memory = state(tmp_path)
    # Routing is determined by the event's kind, including restored queued events.
    native = [
        {"id": n, "kind": "trade", "wake": True, "body": {"proposal": f"n{n}", "mode": "NATIVE_AI"}}
        for n in range(1, 61)
    ]
    memory.ingest("g1", native, 60)
    assert not memory.has_wake() and not memory.snapshot("communication")["events"]
    assert len(memory.snapshot("gameplay")["events"]) == 50
    message = {"id": 61, "kind": "message", "wake": True, "body": {"author": "Greece", "text": "Negotiate?"}}
    memory.ingest("g1", [message], 61)
    assert memory.has_wake()
    snapshot = memory.start_job("chat", "communication", {})
    assert snapshot["events"] == [message] and snapshot["cursor"] == 61
    memory.finish_job("chat", True)
    assert not memory.has_wake()
    assert len(memory.snapshot("gameplay")["events"]) == 50


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
    restarted.receipt(request["id"], {"kind": "MOVE_UNIT", "moved": True, "actionSummary": "Warrior moved to (1, 0)."})
    assert not restarted.pending()
    workspace = tmp_path / "view"
    workspace.mkdir(mode=0o700)
    restarted.export(workspace)
    exported = json.loads((workspace / "turn_0" / "receipts.jsonl").read_text())
    assert exported["receipt"] == {"kind": "MOVE_UNIT", "moved": True, "actionSummary": "Warrior moved to (1, 0)."}
    assert not (workspace / "turn_1").exists()


def test_round_restore_fences_jobs_and_regenerates_projections(tmp_path):
    memory = state(tmp_path)
    memory.start_job("job", "gameplay", {})
    memory.patch("job", [{"op": "create", "content": {"type": "note", "text": "Develop peacefully"}}])
    path = tmp_path / "rounds" / "one.sqlite3"
    checksum = memory.backup(path)
    memory.patch("job", [{"op": "amend", "id": "mem1", "fields": {"text": "Invade"}}])
    memory.ingest("g1", [{"id": 1, "kind": "message", "body": {"text": "Abandoned future"}}], 1)
    memory.restore(path, checksum, "g2")
    memory.pause(False)
    workspace = tmp_path / "view"
    workspace.mkdir(mode=0o700)
    memory.export(workspace)
    assert "Develop peacefully" in (workspace / "notes/mem1.md").read_text()
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
