"""Reflection stays attached to its evidence; exports grow with changed work."""

import json
from unittest.mock import patch

import pytest

from civ_agent.civilization_state import CivilizationState, StateConflict
from tests.test_structured_memory import create, current, plan, setup


@pytest.mark.parametrize("operation", ["revise", "amend"])
def test_reflection_reads_frozen_evidence_and_preserves_new_review_flags(tmp_path, operation):
    memory = setup(tmp_path)
    memory.observe("border", {"turn": 4, "units": ["Greek warrior"]}, "game")
    content = plan(assumptions=[{"text": "Warrior remains nearby", "source": "observation:border"}])
    key = create(memory, content)
    memory.begin_memory_phase("game")
    memory.start_job("chat", "communication", {})
    memory.observe("border", {"turn": 5, "units": []}, "chat")
    memory.observe("new-report", {"turn": 5, "text": "NEW_INFORMATION"}, "chat")

    assert memory.refresh_memory("game", ["observation:border"])["observation:border"]["body"]["units"] == [
        "Greek warrior"
    ]
    assert memory.refresh_memory("game", ["observation:new-report"]) == {}
    assert memory.history(4, job="game")["actions"] == []
    with pytest.raises(StateConflict, match="MEMORY_ONLY"):
        memory.history(5, job="game")
    change = (
        {"op": "revise", "id": key, "content": {**content, "objective": "Defend the eastern approach"}}
        if operation == "revise"
        else {"op": "amend", "id": key, "fields": {"objective": "Defend the eastern approach"}}
    )
    rejected = memory.patch("game", [change])["changes"][0]
    assert not rejected["accepted"] and rejected["reason"] == "STALE_ASSUMPTIONS"
    assert all(read["tool"] == "memory_read" for read in rejected["recovery"]["reads"])
    assert all(
        record["type"] in {"plan", "belief", "note", "soul"}
        for record in memory.search_memory("game", {"state": "all"})["records"]
    )
    memory.refresh_memory("game", [key])
    assert memory.patch("game", [change])["changes"][0]["accepted"]
    saved = current(memory, key)
    assert saved["updatedTurn"] == 5  # Actual write time; evidence and job still belong to turn 4.
    assert saved["needsReview"] and saved["_review"]
    belief = create(
        memory,
        {
            "type": "belief",
            "claim": "The warrior was nearby on turn 4",
            "uncertainty": "low",
            "sources": ["observation:border"],
        },
    )
    assert current(memory, belief)["_evidence"][0]["version"] == 1
    memory.finish_job("game", True)
    summary = memory.completed_summary("game")
    border = next(row for row in summary["observations"] if row["id"] == "observation:border")
    assert border["body"]["units"] == ["Greek warrior"]
    assert any(row["id"] == belief for row in summary["memoryChanges"])


def test_reflection_cannot_overwrite_a_concurrent_chat_edit(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, {"type": "note", "text": "Original"})
    memory.begin_memory_phase("game")
    memory.start_job("chat", "communication", {})
    memory.refresh_memory("chat", [key])
    memory.patch("chat", [{"op": "revise", "id": key, "content": {"type": "note", "text": "New negotiated terms"}}])
    result = memory.patch("game", [{"op": "revise", "id": key, "content": {"type": "note", "text": "Old terms"}}])
    assert not result["changes"][0]["accepted"]
    assert current(memory, key)["text"] == "New negotiated terms"


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
