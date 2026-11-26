"""Reflection stays attached to its evidence; explicit exports use committed records."""

import json
from unittest.mock import patch

import pytest

from civ_agent.civilization_state import CivilizationState, StateConflict
from tests.domain_fixtures import create, current, setup


def test_communication_appends_contributions_at_the_next_writer_boundary(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, {"type": "note", "text": "Original"})
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
    assert "basis" not in found[0]["body"]
    assert found[0]["type"] == "contribution"
    immutable = memory.patch("next", [{"op": "amend", "id": contribution, "fields": {"text": "Rewrite"}}])
    assert immutable["changes"][0]["reason"] == "IMMUTABLE_CONTRIBUTION"
    assert memory.patch("next", [{"op": "retire", "id": contribution, "reason": "completed"}])["changes"][0]["accepted"]
    assert current(memory, key)["text"] == "Completed-turn reasoning"


def test_explicit_exports_preserve_unchanged_files_and_recover_failed_writes(tmp_path):
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
    from civ_agent.memory_history import SummaryReader

    with patch.object(SummaryReader, "build", autospec=True, side_effect=SummaryReader.build) as summaries:
        memory.export(workspace)
        assert [call.args[1:] for call in summaries.call_args_list] == [(4, None), (5, None)]
        memory.export(workspace)
        assert summaries.call_count == 4
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
    restarted.export(workspace)
    assert old.read_bytes() == original[0]


def test_explicit_export_combines_attempts(tmp_path):
    memory = setup(tmp_path)
    memory.queue("game", "player_command", {"kind": "END_TURN"}, "first")
    memory.finish_job("game", False)
    workspace = tmp_path / "workspace"
    memory.export(workspace)
    memory.start_job("retry", "gameplay", {}, game_turn=4)
    memory.queue("retry", "player_command", {"kind": "END_TURN"}, "second")
    memory.export(workspace)
    receipts = (workspace / "turn_4/receipts.jsonl").read_text().splitlines()
    assert len(receipts) == 2
