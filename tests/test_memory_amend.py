import json

import pytest
from pydantic import ValidationError

from civ_agent.memory_records import MemoryUpdate, MemoryUpdateResult, memory_argument_error
from tests.test_structured_memory import create, current, plan, setup


def test_amend_keeps_omitted_fields_and_replaces_citations_without_losing_history(tmp_path):
    memory = setup(tmp_path)
    sources = []
    for index in range(20):
        name = f"fact{index}"
        memory.observe(name, {"value": index}, "game")
        sources.append(f"observation:{name}")
    key = create(memory, plan(sources=sources, expiresTurn=9))
    old = current(memory, key)
    memory.finish_job("game", True)
    memory.start_job("next", "gameplay", {}, game_turn=5)
    memory.refresh_memory("next", [key])
    result = memory.patch("next", [{"op": "amend", "id": key, "fields": {"approach": "Withdraw", "expiresTurn": None}}])
    assert result["changes"][0]["accepted"]
    updated = current(memory, key)
    assert updated["sources"] == old["sources"] and updated["_evidence"] == old["_evidence"]
    assert updated["objective"] == old["objective"] and updated["createdTurn"] == old["createdTurn"]
    assert updated["approach"] == "Withdraw" and updated["expiresTurn"] is None
    result = memory.patch("next", [{"op": "amend", "id": key, "fields": {"sources": sources[-1:]}}])
    assert result["changes"][0]["accepted"]
    assert current(memory, key)["sources"] == sources[-1:]
    history = memory.search_memory("next", {"id": key, "history": True})["records"]
    assert any(r["sources"] == sources for r in history)


@pytest.mark.parametrize("fields", [{"createdTurn": 99}, {"needsReview": []}, {"type": "note"}, {"objective": None}])
def test_amend_rejects_metadata_type_and_invalid_values_without_partial_writes(tmp_path, fields):
    memory = setup(tmp_path)
    key = create(memory, plan())
    before = memory.snapshot("gameplay")["records"][key]
    result = memory.patch("game", [{"op": "amend", "id": key, "fields": {"approach": "Should not be saved", **fields}}])
    MemoryUpdateResult.model_validate(result)
    assert not result["changes"][0]["accepted"]
    assert memory.snapshot("gameplay")["records"][key] == before


def test_amend_is_owned_by_gameplay_and_assumptions_are_advisory(tmp_path):
    memory = setup(tmp_path)
    memory.observe("border", {"units": 1}, "game")
    key = create(memory, plan(assumptions=[{"text": "Border covered", "source": "observation:border"}]))
    memory.start_job("other", "communication", {})
    memory.refresh_memory("other", [key])
    rejected = memory.patch("other", [{"op": "amend", "id": key, "fields": {"approach": "New plan"}}])
    assert rejected["changes"][0]["reason"] == "NOTEBOOK_READ_ONLY"
    memory.observe("border", {"units": 0}, "other")
    kept = memory.patch("game", [{"op": "amend", "id": key, "fields": {"approach": "Hold"}}])
    assert kept["changes"][0]["accepted"]
    assert current(memory, key)["needsReview"]
    memory.refresh_memory("game", [key])
    dropped = memory.patch("game", [{"op": "amend", "id": key, "fields": {"approach": "Reassess", "assumptions": []}}])
    assert dropped["changes"][0]["accepted"]
    assert not current(memory, key)["needsReview"]


def test_source_limit_errors_give_counts_and_do_not_echo_evidence(tmp_path):
    sources = [f"private-evidence-{i}" for i in range(21)]
    arguments = {"changes": [{"op": "create", "content": plan(sources=sources)}]}
    with pytest.raises(ValidationError) as caught:
        MemoryUpdate.model_validate(arguments)
    error = memory_argument_error(arguments, caught.value)
    assert error["limits"] == [{"fields": ["changes", 0, "content", "sources"], "maximum": 20, "received": 21}]
    assert "private-evidence" not in json.dumps(error)
    memory = setup(tmp_path)
    key = create(memory, plan())
    result = memory.patch("game", [{"op": "amend", "id": key, "fields": {"sources": sources}}])
    MemoryUpdateResult.model_validate(result)
    assert not result["changes"][0]["accepted"]
    assert result["changes"][0]["limits"] == [{"fields": ["fields", "sources"], "maximum": 20, "received": 21}]
    assert "private-evidence" not in json.dumps(result)


def test_amend_batch_keeps_successful_edits_and_replays_once(tmp_path):
    memory = setup(tmp_path)
    first = create(memory, {"type": "note", "text": "Original"})
    second = create(memory, plan())
    changes = [
        {"op": "amend", "id": first, "fields": {"text": "Keep the new objective"}},
        {"op": "amend", "id": second, "fields": {"objective": None}},
    ]
    result = memory.patch("game", changes, "amend-batch")
    assert [item["accepted"] for item in result["changes"]] == [True, False]
    saved = memory.snapshot("gameplay")["records"]
    assert saved[first]["body"]["text"] == "Keep the new objective"
    assert saved[second]["body"]["objective"] == "Defend Rome"
    assert memory.patch("game", changes, "amend-batch") == result
    assert memory.snapshot("gameplay")["records"] == saved


def test_unavailable_citations_warn_without_rejecting_an_assessment_or_delivering_content(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, plan(sources=["observation:game"]))
    memory.begin_memory_phase("game")
    memory.start_job("chat", "communication", {})
    memory.observe("later-trade", {"turn": 5, "text": "FUTURE_TRADE_CONTENT"}, "chat")
    changes = [{"op": "amend", "id": key, "fields": {"approach": "Withdraw", "sources": ["observation:later-trade"]}}]
    result = memory.patch("game", changes)["changes"][0]
    assert result["accepted"]
    assert result["warnings"][0]["sources"] == ["observation:later-trade"]
    assert "FUTURE_TRADE_CONTENT" not in json.dumps(result)
    assert memory.refresh_memory("game", ["observation:later-trade"]) == {}
    assert current(memory, key)["_evidence"] == []
    assert current(memory, key)["unverifiedSources"] == ["observation:later-trade"]
    assert memory.run_failure() is None
