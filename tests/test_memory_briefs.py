"""Strategy stays provisional; turn handoffs never acquire an indefinite lifetime."""

import json

import pytest
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState
from civ_agent.memory_records import Intent, MemoryUpdate, MemoryUpdateResult
from tests.test_structured_memory import create, current, setup, strategy


def test_expired_contributions_leave_preload_before_retiring_at_writer_boundary(tmp_path):
    memory = setup(tmp_path, limit=1000)
    memory.start_job("chat", "communication", {}, game_turn=4)
    old = create(memory, {"type": "note", "text": "Old trade is active. " * 25, "expiresTurn": 5}, "chat")
    useful = create(memory, {"type": "note", "text": "Unresolved diplomatic question", "expiresTurn": 9}, "chat")
    memory.finish_job("chat", True)
    before = memory.snapshot("gameplay", game_turn=4)
    assert old in before["contextIds"]
    # Read-only selection may exclude stale data while the outgoing writer is
    # still working. It must not change its notebook or frozen evidence.
    next_context = memory.snapshot("gameplay", game_turn=5)
    assert old not in next_context["contextIds"] and useful in next_context["contextIds"]
    assert next_context["omitted"] == 0
    assert json.loads(memory.context(next_context).split("\n", 1)[1])["pendingContributions"] == 1
    assert current(memory, old)["state"] == "active"
    memory.finish_job("game", True)
    restarted = CivilizationState(memory.root, "Rome", context_limit=1000)
    resumed = restarted.start_job("next", "gameplay", {}, game_turn=5)
    assert old not in resumed["contextIds"] and useful in resumed["contextIds"]
    assert current(restarted, old)["state"] == "retired"
    assert current(restarted, old)["retirementReason"] == "expired"
    history = restarted.search_memory("next", {"id": old, "type": "contribution", "history": True, "state": "all"})
    assert [r["body"]["state"] for r in history["records"]] == ["retired", "active"]


def test_communication_preload_omits_expired_notes_without_mutating_them(tmp_path):
    memory = setup(tmp_path)
    old = create(memory, {"type": "note", "text": "Obsolete assessment", "expiresTurn": 5})
    context = memory.snapshot("communication", game_turn=5)
    assert old not in context["contextIds"]
    assert current(memory, old)["state"] == "active"


def test_equally_relevant_memory_prefers_recent_updates_when_budget_is_full(tmp_path):
    memory = setup(tmp_path, limit=1000)
    old = create(memory, {"type": "note", "text": "Earlier " * 65})
    memory.finish_job("game", True)
    memory.start_job("next", "gameplay", {}, game_turn=5)
    new = create(memory, {"type": "note", "text": "Current " * 65}, "next")
    context = memory.snapshot("gameplay", game_turn=5)
    assert new in context["contextIds"] and old not in context["contextIds"]
    assert context["omitted"] == 1


def replace(key, **content):
    return {"op": "replace", "id": key, "content": strategy(**content)}


def test_replacement_drops_omitted_content_and_preserves_history(tmp_path):
    memory = setup(tmp_path)
    key = create(
        memory, strategy(reasons=["Libraries are ready"], uncertainties=["Egypt may invade"], subjects=["science"])
    )
    assert memory.patch("game", [{"op": "amend", "id": key, "fields": {"subjects": ["Defend"]}}])["changes"][0][
        "reason"
    ].startswith("REPLACEMENT_REQUIRED")
    replacement = {
        "op": "replace",
        "id": key,
        "content": {"type": "strategy", "assessment": "Survival is the current bottleneck"},
    }
    result = memory.patch("game", [replacement], "replace-once")
    MemoryUpdateResult.model_validate(result)
    assert result["changes"][0]["accepted"]
    saved = current(memory, key)
    assert saved["reasons"] == saved["uncertainties"] == saved["subjects"] == []
    assert saved["createdTurn"] == saved["updatedTurn"] == 4
    assert memory.patch("game", [replacement], "replace-once") == result
    history = memory.search_memory("game", {"id": key, "history": True})["records"]
    assert len(history) == 2 and history[-1]["uncertainties"] == ["Egypt may invade"]


def test_handoff_does_not_reaffirm_strategy_or_acknowledge_its_notices(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, strategy(subjects=["Egypt"]))
    memory.ingest(
        "g1", [{"id": 1, "kind": "engine", "body": {"participants": ["Rome", "Egypt"], "action": "DECLARE_WAR"}}], 1
    )
    memory.refresh_memory("game", [key])
    before = current(memory, key)
    create(memory, {"type": "handoff", "text": "Check the worker's unfinished road."})
    assert current(memory, key) == before and before["needsReview"]
    memory.finish_job("game", True)
    memory.start_job("next", "gameplay", {}, game_turn=5)
    assert current(memory, key)["updatedTurn"] == 4
    assert memory.patch("next", [replace(key, assessment="Science remains plausible; defense needs investment")])[
        "changes"
    ][0]["accepted"]
    assert current(memory, key)["updatedTurn"] == 5 and not current(memory, key)["needsReview"]


def test_replacement_cannot_bypass_read_conflicts_or_change_type(tmp_path):
    memory = setup(tmp_path, limit=1000)
    key = create(memory, strategy(assessment="An uncertain science route. " * 50))
    before = current(memory, key)
    wrong_type = memory.patch(
        "game", [{"op": "replace", "id": key, "content": {"type": "handoff", "text": "Next turn"}}]
    )
    assert wrong_type["changes"][0]["reason"] == "TYPE_CHANGE_REQUIRES_NEW_RECORD"
    assert current(memory, key) == before
    memory.finish_job("game", True)
    memory.start_job("next", "gameplay", {}, game_turn=5)
    unread = memory.patch("next", [replace(key, assessment="Changed")])["changes"][0]
    assert unread["reason"] == "MEMORY_CONFLICT"
    assert unread["recovery"]["reads"][0]["arguments"]["ids"] == [key]
    assert current(memory, key) == before
    memory.refresh_memory("next", [key])
    assert memory.patch("next", [replace(key, assessment="Changed")])["changes"][0]["accepted"]


def test_handoff_uses_author_turn_through_background_news_restart_and_retry(tmp_path):
    memory = setup(tmp_path)
    memory.start_job("chat", "communication", {})
    memory.observe("communication", {"context": {"turn": 5}}, "chat")
    key = create(memory, {"type": "handoff", "text": "Inspect Antium's surviving defenders."})
    assert current(memory, key)["updatedTurn"] == current(memory, key)["basis"]["turn"] == 4
    assert key not in memory.snapshot("communication")["contextIds"]
    memory.finish_job("game", True)
    next_turn = memory.start_job("next", "gameplay", {}, game_turn=5)
    assert key in next_turn["contextIds"]
    memory.finish_job("next", False)
    restarted = CivilizationState(memory.root, "Rome")
    retry = restarted.start_job("retry", "gameplay", {}, game_turn=5)
    assert key in retry["contextIds"]
    restarted.finish_job("retry", True)
    after = restarted.start_job("after", "gameplay", {}, game_turn=6)
    assert key not in after["contextIds"]
    assert current(restarted, key)["state"] == "retired"
    assert (
        restarted.search_memory("after", {"id": key, "state": "all"})["records"][0]["text"]
        == "Inspect Antium's surviving defenders."
    )
    successor = create(restarted, {"type": "handoff", "text": "New unfinished work"}, "after")
    assert successor != key


def test_replacing_handoff_advances_only_its_own_authoring_turn(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, {"type": "handoff", "text": "Explore east"})
    memory.finish_job("game", True)
    memory.start_job("next", "gameplay", {}, game_turn=5)
    result = memory.patch(
        "next",
        [
            {
                "op": "replace",
                "id": key,
                "content": {"type": "handoff", "text": "East is blocked; evaluate another route"},
            }
        ],
    )
    assert result["changes"][0]["accepted"]
    memory.finish_job("next", True)
    assert key in memory.start_job("after", "gameplay", {}, game_turn=6)["contextIds"]


def test_checkpoint_restores_handoff_lifetime_and_excludes_discarded_future(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, {"type": "handoff", "text": "Original unfinished work"})
    memory.finish_job("game", True)
    path = tmp_path / "round.json"
    checksum = memory.backup(path)
    memory.start_job("future", "gameplay", {}, game_turn=5)
    memory.patch("future", [{"op": "replace", "id": key, "content": {"type": "handoff", "text": "DISCARDED FUTURE"}}])
    memory.restore(path, checksum, "g2")
    memory.pause(False)
    snapshot = memory.start_job("restored", "gameplay", {}, game_turn=5)
    assert key in snapshot["contextIds"]
    assert "DISCARDED FUTURE" not in memory.context(snapshot)
    assert current(memory, key)["basis"]["turn"] == 4


def test_singletons_do_not_multiply_and_strategy_is_not_a_summary_obligation(tmp_path):
    memory = setup(tmp_path)
    create(memory, strategy())
    assert not memory.patch("game", [{"op": "create", "content": strategy(assessment="Another")}])["changes"][0][
        "accepted"
    ]
    create(memory, {"type": "handoff", "text": "Inspect a possible settlement"})
    assert memory.summary(4)["unresolved"] == []


@pytest.mark.parametrize("content", [strategy(), {"type": "handoff", "text": "Inspect a possible settlement"}])
def test_duplicate_brief_identifies_the_existing_record_for_recovery(tmp_path, content):
    memory = setup(tmp_path)
    key = create(memory, content)
    result = memory.patch("game", [{"op": "create", "content": content}])
    MemoryUpdateResult.model_validate(result)
    change = result["changes"][0]
    assert not change["accepted"] and change["recoverable"]
    assert change["recovery"]["reads"] == [{"tool": "memory_read", "arguments": {"ids": [key]}}]
    assert len(memory.search_memory("game", {"type": content["type"]})["records"]) == 1
    memory.engine.dispose()


def test_command_reference_requires_a_current_reason_and_legacy_types_are_absent():
    with pytest.raises(ValidationError):
        Intent.model_validate({"strategy": "mem1"})
    assert Intent.model_validate({"reason": "Replace the lost defender", "strategy": "mem1"}).reason
    schema = json.dumps(MemoryUpdate.model_json_schema())
    assert '"successCriteria"' not in schema and '"reviewAfterTurn"' not in schema
    for kind in ("plan", "soul"):
        with pytest.raises(ValidationError):
            MemoryUpdate.model_validate({"changes": [{"op": "create", "content": {"type": kind, "text": "Old plan"}}]})


def test_war_change_notifies_untagged_strategy_without_prescribing_or_blocking(tmp_path):
    memory = setup(tmp_path)
    view = {"turn": 4, "diplomacy": {"knownCivilizations": [{"civilizationId": "Egypt", "atWar": False}]}}
    memory.observe_player(view, "game")
    key = create(memory, strategy(subjects=[]))
    before = current(memory, key)
    view["turn"] = 5
    view["diplomacy"]["knownCivilizations"][0]["atWar"] = True
    memory.observe_player(view, "game")
    after = current(memory, key)
    assert memory.snapshot("gameplay")["records"][key]["version"] == 1
    assert after["assessment"] == before["assessment"] and after["updatedTurn"] == before["updatedTurn"]
    assert "Turn 5: Egypt war status False → True" in after["needsReview"]
    assert memory.run_failure() is None


def test_background_retirement_keeps_authorship_and_original_assessment(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, strategy(assessment="Science looked promising"))
    memory.start_job("chat", "communication", {})
    memory.observe("communication", {"context": {"turn": 5}}, "chat")
    result = memory.patch(
        "game", [{"op": "retire", "id": key, "reason": "abandoned", "assessment": "War changed this"}]
    )
    assert result["changes"][0]["accepted"]
    record = current(memory, key)
    assert record["assessment"] == "Science looked promising"
    assert record["retirementAssessment"] == "War changed this"
    assert record["updatedTurn"] == record["basis"]["turn"] == 4
