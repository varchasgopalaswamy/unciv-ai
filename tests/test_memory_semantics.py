"""Regressions for the retry loops observed during ordinary game reflection."""

import json

import pytest

from civ_agent.civilization_state import StaleAssumptions
from civ_agent.observations import DetailQuery, detail_scope
from tests.test_structured_memory import create, current, plan, setup


def test_notes_can_describe_unreviewed_and_retired_plans_without_authorizing_them(tmp_path):
    memory = setup(tmp_path)
    memory.observe("border", {"turn": 4, "units": ["Warrior"]}, "game")
    key = create(memory, plan(assumptions=[{"text": "Warrior remains nearby", "source": "observation:border"}]))
    memory.begin_memory_phase("game")
    memory.start_job("chat", "communication", {})
    memory.observe("border", {"turn": 5, "units": []}, "chat")
    note = create(memory, {"type": "note", "text": "This plan failed to protect the border", "plan": key})
    assert current(memory, note)["_relatedPlan"]["key"] == key
    assert not current(memory, note)["_assumptions"]
    assert current(memory, key)["needsReview"]
    memory.refresh_memory("chat", [key])
    with pytest.raises(StaleAssumptions):
        memory.queue("chat", "player_command", {"kind": "ATTACK"}, {}, "intent", {"plan": key})
    memory.patch("chat", [{"op": "retire", "id": key, "reason": "abandoned"}])
    second = create(memory, {"type": "note", "text": "Remember why we abandoned this plan", "plan": key})
    assert not current(memory, second)["needsReview"]
    assert not current(memory, note)["needsReview"]


def test_historical_memory_citations_do_not_require_live_dependencies(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, plan())
    memory.start_job("chat", "communication", {})
    memory.refresh_memory("chat", [key])
    memory.patch("chat", [{"op": "retire", "id": key, "reason": "completed"}])
    history = create(memory, plan(objective="Remember our earlier strategy", sources=[key]))
    assert current(memory, history)["_evidence"][0]["version"] == 1
    result = memory.patch(
        "game", [{"op": "create", "content": plan(assumptions=[{"text": "Plan remains active", "source": key}])}]
    )
    assert not result["changes"][0]["accepted"]


def test_revisions_keep_delivered_citations_but_new_claims_cannot_borrow_them(tmp_path):
    memory = setup(tmp_path)
    query = DetailQuery(tool="unit_details", arguments={"target": "u1"})
    scope = detail_scope(query)
    _, detail = memory.observe_detail(scope, {"query": query.model_dump(), "turn": 4, "data": {"health": 40}}, "game")
    evidence = detail["evidence"]
    assert evidence.startswith("e") and evidence[1:].isdigit()
    content = plan(sources=[evidence])
    key = create(memory, content)
    original = current(memory, key)["_evidence"]
    memory.start_job("next", "gameplay", {}, game_turn=5)
    memory.refresh_memory("next", [key])
    updated = memory.patch("next", [{"op": "revise", "id": key, "content": {**content, "approach": "Withdraw"}}])
    assert updated["changes"][0]["accepted"]
    assert current(memory, key)["_evidence"] == original
    unread = memory.patch("next", [{"op": "create", "content": content}])
    assert unread["changes"][0]["reason"].startswith("SOURCE_NOT_DELIVERED")
    assert not memory.patch(
        "next",
        [
            {
                "op": "revise",
                "id": key,
                "content": plan(assumptions=[{"text": "Unit remains injured", "source": evidence}]),
            }
        ],
    )["changes"][0]["accepted"]


def test_dropping_an_assumption_does_not_require_reading_its_obsolete_evidence(tmp_path):
    memory = setup(tmp_path)
    memory.observe("border", {"turn": 4, "units": ["Warrior"]}, "game")
    content = plan(assumptions=[{"text": "Warrior is at the border", "source": "observation:border"}])
    key = create(memory, content)
    memory.observe("border", {"turn": 4, "units": []})
    memory.refresh_memory("game", [key])
    assert current(memory, key)["needsReview"]
    kept = memory.patch("game", [{"op": "revise", "id": key, "content": content}])
    assert not kept["changes"][0]["accepted"]
    revised = memory.patch(
        "game", [{"op": "revise", "id": key, "content": plan(approach="Reassess the border before advancing")}]
    )
    assert revised["changes"][0]["accepted"]
    assert not current(memory, key)["needsReview"]
    assert not current(memory, key)["_assumptions"]


def test_detail_handles_pin_old_revisions_and_recovery_only_suggests_delivered_evidence(tmp_path):
    memory = setup(tmp_path)
    query = DetailQuery(tool="unit_details", arguments={"target": "u1"})
    scope = detail_scope(query)
    body = {"query": query.model_dump(), "turn": 4, "data": {"health": 40}}
    _, first = memory.observe_detail(scope, body, "game")
    assert memory.observe_detail(scope, body, "game")[1]["evidence"] == first["evidence"]
    _, second = memory.observe_detail(scope, {**body, "data": {"health": 20}}, "game")
    assert first["evidence"] != second["evidence"]
    remembered = create(memory, plan(sources=[first["evidence"]]))
    assert current(memory, remembered)["_evidence"][0]["version"] == 1
    rejected = memory.patch(
        "game",
        [{"op": "create", "content": plan(assumptions=[{"text": "Unit has 40 HP", "source": first["evidence"]}])}],
    )
    assert rejected["changes"][0]["reason"].startswith("ASSUMPTION_CHANGED")
    memory.begin_memory_phase("game")
    memory.start_job("chat", "communication", {})
    _, unseen = memory.observe_detail(scope, {**body, "data": {"health": 10}}, "chat")
    typo = "observation:" + scope[:-1] + "z"
    result = memory.patch("game", [{"op": "revise", "id": remembered, "content": plan(sources=[typo])}])
    rejection = result["changes"][0]
    assert not rejection["accepted"]
    assert rejection["recovery"]["reads"] == []
    candidates = rejection["recovery"]["sourceSuggestions"][0]["candidates"]
    assert candidates[0]["source"] == second["evidence"]
    assert candidates[0]["query"] == query.model_dump()
    assert unseen["evidence"] not in json.dumps(candidates)
