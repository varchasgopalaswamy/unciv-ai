"""Notes and beliefs support partial edits; brief replacement is tested separately."""

import json

import pytest
from pydantic import ValidationError

from civ_agent.memory_records import MemoryUpdate, MemoryUpdateResult, memory_argument_error
from tests.domain_fixtures import create, current, setup


def belief(**changes):
    return {"type": "belief", "claim": "Egypt may attack", "uncertainty": "medium", **changes}


def test_amend_keeps_omitted_fields_and_replaces_citations_without_losing_history(tmp_path):
    memory = setup(tmp_path)
    sources = []
    for index in range(20):
        name = f"fact{index}"
        memory.observe(name, {"value": index}, "game")
        sources.append(f"observation:{name}")
    key = create(memory, belief(sources=sources, expiresTurn=9))
    old = current(memory, key)
    memory.finish_job("game", True)
    memory.start_job("next", "gameplay", {}, game_turn=5)
    memory.refresh_memory("next", [key])
    result = memory.patch("next", [{"op": "amend", "id": key, "fields": {"uncertainty": "high", "expiresTurn": None}}])
    assert result["changes"][0]["accepted"]
    updated = current(memory, key)
    assert updated["sources"] == old["sources"] and updated["_evidence"] == old["_evidence"]
    assert updated["claim"] == old["claim"] and updated["createdTurn"] == old["createdTurn"]
    assert updated["uncertainty"] == "high" and updated["expiresTurn"] is None
    assert memory.patch("next", [{"op": "amend", "id": key, "fields": {"sources": sources[-1:]}}])["changes"][0][
        "accepted"
    ]
    assert current(memory, key)["sources"] == sources[-1:]
    assert memory.search_memory("next", {"id": key, "history": True})["records"][-1]["sources"] == sources


@pytest.mark.parametrize("fields", [{"createdTurn": 99}, {"needsReview": []}, {"type": "note"}, {"claim": None}])
def test_amend_rejects_metadata_and_invalid_content_without_partial_write(tmp_path, fields):
    memory = setup(tmp_path)
    key = create(memory, belief())
    before = current(memory, key)
    with pytest.raises(ValueError, match="INVALID_ARGUMENT"):
        memory.patch("game", [{"op": "amend", "id": key, "fields": fields}])
    assert current(memory, key) == before


def test_amend_is_owned_by_gameplay(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, belief())
    memory.start_job("chat", "communication", {})
    result = memory.patch("chat", [{"op": "amend", "id": key, "fields": {"uncertainty": "low"}}])
    assert result["changes"][0]["reason"] == "NOTEBOOK_READ_ONLY"


def test_amend_batch_keeps_successful_edits_and_replays_once(tmp_path):
    memory = setup(tmp_path)
    first = create(memory, {"type": "note", "text": "Original"})
    second = create(memory, belief())
    changes = [
        {"op": "amend", "id": first, "fields": {"text": "New information"}},
        {"op": "amend", "id": second, "fields": {"text": "A note field cannot amend a belief"}},
    ]
    result = memory.patch("game", changes, "batch")
    MemoryUpdateResult.model_validate(result)
    assert [item["accepted"] for item in result["changes"]] == [True, False]
    saved = memory.snapshot("gameplay")["records"]
    assert saved[first]["body"]["text"] == "New information"
    assert saved[second]["body"]["claim"] == "Egypt may attack"
    assert memory.patch("game", changes, "batch") == result
    assert memory.snapshot("gameplay")["records"] == saved


@pytest.mark.parametrize(
    "field,value,bound,unit",
    [
        ("subjects", ["PRIVATE_TAG"] * 23, 20, "items"),
        ("sources", ["PRIVATE_EVIDENCE"] * 21, 20, "items"),
        ("claim", "PRIVATE_TEXT" * 200, 2000, "characters"),
    ],
)
def test_constraints_name_the_actual_field_without_echoing_content(tmp_path, field, value, bound, unit):
    arguments = {"changes": [{"op": "create", "content": belief(**{field: value})}]}
    with pytest.raises(ValidationError) as caught:
        MemoryUpdate.model_validate(arguments)
    error = memory_argument_error(arguments, caught.value)
    assert error["limits"] == [{"fields": ["changes", 0, "content", field], "maximum": bound, "received": len(value)}]
    assert f"{field} accepts at most {bound} {unit}" in error["instruction"]
    assert "PRIVATE_" not in json.dumps(error)
    memory = setup(tmp_path)
    key = create(memory, belief())
    before = current(memory, key)
    with pytest.raises(ValueError, match="INVALID_ARGUMENT"):
        memory.patch("game", [{"op": "amend", "id": key, "fields": {field: value}}])
    assert current(memory, key) == before
    assert memory.patch("game", [{"op": "amend", "id": key, "fields": {field: value[:bound]}}])["changes"][0][
        "accepted"
    ]


def test_unavailable_citations_warn_without_granting_new_evidence(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, belief())
    memory.start_job("chat", "communication", {})
    memory.observe("later-trade", {"turn": 5, "secret": "NOT_DELIVERED"}, "chat")
    result = memory.patch("game", [{"op": "amend", "id": key, "fields": {"sources": ["observation:later-trade"]}}])
    assert result["changes"][0]["accepted"] and result["changes"][0]["warnings"]
    assert "NOT_DELIVERED" not in json.dumps(result)
    assert memory.refresh_memory("game", ["observation:later-trade"])  # An explicit in-turn read delivers new evidence.
