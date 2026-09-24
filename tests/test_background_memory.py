"""Reflection stays attached to its evidence; exports grow with changed work."""

import json
from unittest.mock import patch

import pytest

from civ_agent.civilization_state import CivilizationState, StateConflict
from tests.test_structured_memory import create, current, plan, setup


def test_reflection_reads_frozen_evidence_and_preserves_new_notices(tmp_path):
    memory = setup(tmp_path)
    memory.observe("border", {"turn": 4, "units": ["Greek warrior"]}, "game")
    key = create(memory, plan(assumptions=[{"text": "Warrior remains nearby", "source": "observation:border"}]))
    before = memory.snapshot("gameplay")["records"][key]["version"]
    memory.begin_memory_phase("game")
    memory.start_job("chat", "communication", {})
    memory.observe("border", {"turn": 5, "units": []}, "chat")
    memory.observe("new-report", {"turn": 5, "text": "NEW_INFORMATION"}, "chat")
    assert memory.refresh_memory("game", ["observation:border"])["observation:border"]["body"]["units"] == [
        "Greek warrior"
    ]
    assert memory.refresh_memory("game", ["observation:new-report"]) == {}
    with pytest.raises(StateConflict, match="MEMORY_ONLY"):
        memory.history(5, job="game")
    with pytest.raises(StateConflict, match="MEMORY_ONLY"):
        memory.queue("game", "player_command", {"kind": "END_TURN"}, {}, "after-handoff")
    assert memory.snapshot("gameplay")["records"][key]["version"] == before
    accepted = memory.patch(
        "game", [{"op": "amend", "id": key, "fields": {"objective": "Defend the eastern approach"}}]
    )
    assert accepted["changes"][0]["accepted"]
    saved = current(memory, key)
    assert saved["updatedTurn"] == 5 and saved["basis"]["turn"] == 4
    assert saved["needsReview"] and saved["_review"]
    assert not memory.refresh_memory("game", [key])[key]["body"]["needsReview"]
    memory.finish_job("game", True)
    memory.start_job("next", "gameplay", {}, game_turn=5)
    assert memory.refresh_memory("next", [key])[key]["body"]["needsReview"]
    summary = memory.completed_summary("game")
    assert "NEW_INFORMATION" not in json.dumps(summary)


def test_communication_appends_contributions_at_the_next_writer_boundary(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, {"type": "note", "text": "Original"})
    memory.begin_memory_phase("game")
    memory.start_job("chat", "communication", {})
    memory.refresh_memory("chat", [key])
    rejected = memory.patch("chat", [{"op": "amend", "id": key, "fields": {"text": "New negotiated terms"}}])
    assert rejected["changes"][0]["reason"] == "NOTEBOOK_READ_ONLY"
    contribution = create(memory, {"type": "note", "text": "New negotiated terms"}, "chat")
    assert not memory.refresh_memory("game", [contribution])
    assert not memory.search_memory("game", {"type": "contribution"})["records"]
    assert memory.patch("game", [{"op": "amend", "id": key, "fields": {"text": "Completed-turn reasoning"}}])[
        "changes"
    ][0]["accepted"]
    with pytest.raises(StateConflict, match="NOTEBOOK_WRITER_BUSY"):
        memory.start_job("too-soon", "gameplay", {}, game_turn=5)
    memory.finish_job("game", True)
    snapshot = memory.start_job("next", "gameplay", {}, game_turn=5)
    assert contribution in snapshot["contributionIds"]
    found = memory.search_memory("next", {"type": "contribution"})["records"]
    assert found[0]["body"]["text"] == "New negotiated terms"
    assert found[0]["body"]["basis"]["purpose"] == "communication"
    immutable = memory.patch("next", [{"op": "amend", "id": contribution, "fields": {"text": "Rewrite"}}])
    assert immutable["changes"][0]["reason"] == "IMMUTABLE_CONTRIBUTION"
    assert memory.patch("next", [{"op": "retire", "id": contribution, "reason": "completed"}])["changes"][0]["accepted"]
    assert current(memory, key)["text"] == "Completed-turn reasoning"


def test_edit_response_carries_notice_delivery_into_background_work(tmp_path):
    memory = setup(tmp_path)
    memory.observe("border", {"units": 1}, "game")
    key = create(memory, plan(assumptions=[{"text": "Guard nearby", "source": "observation:border"}]))
    memory.observe("border", {"units": 0})
    result = memory.patch("game", [{"op": "amend", "id": key, "fields": {"approach": "Reposition"}}])
    assert result["changes"][0]["record"]["needsReview"]
    memory.begin_memory_phase("game")
    for approach in ("Defend the capital", "Bring reinforcements"):
        revised = memory.patch("game", [{"op": "amend", "id": key, "fields": {"approach": approach}}])
        assert not revised["changes"][0]["record"]["needsReview"]
        assert not current(memory, key)["needsReview"]


def test_exports_leave_completed_turns_untouched_and_recover_dirty_work(tmp_path):
    memory = setup(tmp_path)
    create(memory, {"type": "note", "text": "Old turn"})
    memory.finish_job("game", True)
    workspace = tmp_path / "workspace"
    memory.export(workspace)
    old = workspace / "turn_4/summary.json"
    original = old.read_bytes(), old.stat().st_mtime_ns
    memory.start_job("next", "gameplay", {}, game_turn=5)
    create(memory, {"type": "note", "text": "New turn"}, "next")
    memory.finish_job("next", True)
    from civ_agent.memory_history import build_summary

    with patch("civ_agent.civilization_state.build_summary", wraps=build_summary) as summaries:
        memory.export(workspace)
        assert [call.kwargs for call in summaries.call_args_list] == [{"turn": 5}]
        memory.export(workspace)
        assert summaries.call_count == 1
    assert (old.read_bytes(), old.stat().st_mtime_ns) == original
    memory.start_job("third", "gameplay", {}, game_turn=6)
    create(memory, {"type": "note", "text": "Unexported committed memory"}, "third")
    memory.finish_job("third", True)
    with patch("civ_agent.civilization_state.export_files", side_effect=OSError("disk unavailable")):
        with pytest.raises(OSError):
            memory.export(workspace)
    restarted = CivilizationState(memory.root, "Rome")
    restarted.export(workspace)
    summary = json.loads((workspace / "turn_6/summary.json").read_text())
    assert any(row["text"] == "Unexported committed memory" for row in summary["memoryChanges"])
    assert (old.read_bytes(), old.stat().st_mtime_ns) == original
    old.unlink()
    with patch("civ_agent.civilization_state.build_summary", wraps=build_summary) as summaries:
        restarted.export(workspace)
        assert [call.kwargs for call in summaries.call_args_list] == [{"turn": 4}]
    assert old.read_bytes() == original[0]


def test_export_combines_attempts_when_only_one_job_is_dirty(tmp_path):
    memory = setup(tmp_path)
    memory.queue("game", "player_command", {"kind": "END_TURN"}, {}, "first")
    memory.finish_job("game", False)
    workspace = tmp_path / "workspace"
    memory.export(workspace)
    memory.start_job("retry", "gameplay", {}, game_turn=4)
    memory.queue("retry", "player_command", {"kind": "END_TURN"}, {}, "second")
    memory.export(workspace)
    receipts = (workspace / "turn_4/receipts.jsonl").read_text().splitlines()
    assert len(receipts) == 2
