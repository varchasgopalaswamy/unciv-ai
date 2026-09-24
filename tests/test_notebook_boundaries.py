"""Adversarial notebook ownership, privacy, restart and rollback checks."""

import json

import pytest
from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState, StaleAssumptions, StateConflict
from civ_agent.memory_records import MemoryUpdate
from tests.test_structured_memory import create, current, plan, setup


def test_repeated_notebook_conflicts_preserve_independent_edits_and_do_not_halt(tmp_path):
    memory = setup(tmp_path, 1000)
    ids = [create(memory, {"type": "note", "text": str(i) + "x" * 900}) for i in range(3)]
    memory.finish_job("game", True)
    memory.start_job("next", "gameplay", {}, game_turn=5)
    omitted = next(key for key in ids if key not in memory.snapshot("gameplay")["contextIds"])
    changes = [
        {"op": "amend", "id": omitted, "fields": {"text": "Unread edit"}},
        {"op": "create", "content": {"type": "note", "text": "Keep the independent reasoning"}},
    ]
    for call_id in ("first", "second"):
        result = memory.patch("next", changes, call_id)
        assert result["changes"][0]["reason"] == "MEMORY_CONFLICT"
        assert result["changes"][1]["accepted"]
        assert memory.patch("next", changes, call_id) == result
    assert memory.run_failure() is None
    assert current(memory, omitted)["text"] != "Unread edit"
    counts = memory.memory_edit_counts("next")
    assert counts["accepted"] == 2 and counts["rejected"] == 2
    memory.refresh_memory("next", [omitted])
    assert memory.patch("next", changes[:1], "reconciled")["changes"][0]["accepted"]


def test_stale_command_state_still_blocks_dispatch_but_intent_does_not(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, plan())
    memory.patch("game", [{"op": "retire", "id": key, "reason": "abandoned"}])
    memory.observe("border", {"units": 1}, "game")
    memory.observe("border", {"units": 2})
    with pytest.raises(StaleAssumptions):
        memory.queue("game", "player_command", {"kind": "ATTACK"}, {"observation:border": 1}, "stale", {"plan": key})
    assert not memory.pending()
    request = memory.queue("game", "player_command", {"kind": "ATTACK"}, {}, "fresh", {"plan": key})
    assert request["id"]


def test_contributions_and_notices_restore_with_their_timeline(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, plan(subjects=["Greece"]))
    memory.start_job("chat", "communication", {})
    retained = create(memory, {"type": "note", "text": "Earlier negotiation"}, "chat")
    memory.finish_job("game", True)
    memory.finish_job("chat", True)
    checkpoint = tmp_path / "round.json"
    checksum = memory.backup(checkpoint)
    memory.start_job("future-chat", "communication", {})
    future = create(memory, {"type": "note", "text": "DISCARDED_FUTURE"}, "future-chat")
    memory.ingest("g1", [{"id": 1, "kind": "diplomacy", "body": {"target": "Greece", "author": "Rome"}}], 1)
    assert current(memory, key)["needsReview"]
    memory.restore(checkpoint, checksum, "g2")
    memory.pause(False)
    memory.start_job("restored", "gameplay", {}, game_turn=4)
    assert not current(memory, key)["needsReview"]
    page = memory.search_memory("restored", {"type": "contribution", "history": True, "state": "all"})
    assert [r["id"] for r in page["records"]] == [retained]
    assert not memory.refresh_memory("restored", [future])
    assert "DISCARDED_FUTURE" not in memory.context(memory.snapshot("gameplay"))
    assert int(create(memory, {"type": "note", "text": "New branch"}, "restored")[3:]) > int(future[3:])
    with pytest.raises(StateConflict, match="STALE_JOB"):
        memory.patch("future-chat", [])


def test_restart_releases_writer_preserves_contributions_and_fences_old_job(tmp_path):
    memory = setup(tmp_path)
    memory.start_job("chat", "communication", {})
    contribution = create(memory, {"type": "note", "text": "Keep this message"}, "chat")
    memory.begin_memory_phase("game")
    restarted = CivilizationState(memory.root, "Rome")
    with pytest.raises(StateConflict, match="STALE_JOB"):
        restarted.patch("game", [])
    snapshot = restarted.start_job("resumed", "gameplay", {}, game_turn=4)
    assert contribution in snapshot["contributionIds"]
    assert (
        restarted.search_memory("resumed", {"type": "contribution"})["records"][0]["body"]["text"]
        == "Keep this message"
    )


def test_notice_projection_never_changes_stored_notebook_content(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, plan(subjects=["Greece"]))
    with memory.transaction() as db:
        original = db.execute(select(tables.records).where(tables.records.c.key == key)).all()
    memory.ingest("g1", [{"id": 1, "kind": "diplomacy", "body": {"target": "Greece", "author": "Rome"}}], 1)
    assert current(memory, key)["needsReview"]
    with memory.transaction() as db:
        assert db.execute(select(tables.records).where(tables.records.c.key == key)).all() == original
    assert "needsReview" not in json.loads(original[0].body)


@pytest.mark.parametrize("operation", ["revise", "replace"])
def test_removed_operations_have_no_compatibility_branch(operation):
    with pytest.raises(ValueError):
        MemoryUpdate.model_validate({"changes": [{"op": operation, "id": "mem1", "content": plan()}]})
