"""Dated intent continuity is advisory; dispatch receipts remain authoritative."""

import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from civ_agent.coordination_context import MAX_COORDINATION_CHARACTERS, encoded, working_context
from civ_agent.intent_handoffs import (
    ATTEMPT_KEY,
    MAX_CHARACTERS,
    compact_attempts,
    record_action_attempts,
    role_handoff,
    startup_handoff,
)
from civ_agent.memory_records import CityPlan, CityPlanPatch, Strategy, amended_content
from civ_agent.prepared_role_context import PreparedRoleContext, delivered_prepared_context
from civ_agent.protocol import JsonObject


def job():
    return SimpleNamespace(
        purpose="military",
        memory={},
        observation={"turn": 14, "revision": 22},
        details_read=[],
        war_versions={},
        memory_versions={},
        seen_messages=set(),
        trades={},
    )


def move(target="unit3", x=3):
    return {"tool": "player_command", "arguments": {"command": {"kind": "MOVE_UNIT", "target": target, "x": x, "y": 5}}}


def test_partial_effects_keep_only_failed_dispatch_and_exact_later_confirmation_resolves_it():
    owner = job()
    operations = [move("unit1"), move()]
    record_action_attempts(
        owner,
        operations,
        [
            {"operationId": "first:0", "status": "confirmed", "result": {}},
            {"operationId": "first:1", "status": "unexecuted", "reason": "FRESH_OBSERVATION_REQUIRED"},
        ],
        decision_id="first",
    )
    assert len(owner.memory[ATTEMPT_KEY]) == 1
    row = owner.memory[ATTEMPT_KEY][0]
    assert row["operationId"] == "first:1" and row["sourceTurn"] == 14
    assert row["currentIntent"] == "unverified"
    # A different chosen action does not prove the earlier intention fulfilled.
    record_action_attempts(
        owner, [move(x=4)], [{"operationId": "second:0", "status": "confirmed", "result": {}}], decision_id="second"
    )
    assert owner.memory[ATTEMPT_KEY]
    record_action_attempts(
        owner, [move()], [{"operationId": "third:0", "status": "confirmed", "result": {}}], decision_id="third"
    )
    assert owner.memory[ATTEMPT_KEY] == []


def test_parsed_flat_commands_and_generated_nested_commands_share_attempt_identity():
    owner = job()
    nested = move()
    record_action_attempts(
        owner,
        [nested],
        [{"operationId": "a:0", "status": "unexecuted", "reason": "FRESH_OBSERVATION_REQUIRED"}],
        decision_id="a",
    )
    flat = {"tool": "player_command", "arguments": {**nested["arguments"]["command"], "reviewAfter": False}}
    record_action_attempts(
        owner, [flat], [{"operationId": "b:0", "status": "confirmed", "result": {}}], decision_id="b"
    )
    assert owner.memory[ATTEMPT_KEY] == []


def test_attempt_tracking_is_bounded_and_does_not_mutate_receipts_or_commands():
    owner = job()
    for n in range(20):
        operations = [move(f"unit{n}")]
        receipts = [{"operationId": f"a{n}:0", "status": "rejected", "result": {"error": "NOT_AVAILABLE"}}]
        before = copy.deepcopy((operations, receipts))
        record_action_attempts(owner, operations, receipts, decision_id=f"a{n}")
        assert (operations, receipts) == before
    handoff = role_handoff(owner)
    assert len(handoff["attempts"]) == 8
    assert handoff["attempts"][-1]["operationId"] == "a19:0"
    projected = compact_attempts({"military": handoff})
    assert len(projected) == 3
    projected[0]["reason"] = "authored replacement"
    assert handoff["attempts"][-3]["reason"] == "NOT_AVAILABLE"


def test_invalidated_observation_keeps_the_attempts_original_dated_basis_only():
    owner = job()
    owner.prior_role_observation, owner.observation = owner.observation, None
    record_action_attempts(
        owner,
        [move()],
        [{"operationId": "old:0", "status": "unexecuted", "reason": "FRESH_OBSERVATION_REQUIRED"}],
        decision_id="old",
    )
    assert role_handoff(owner)["attempts"][0]["sourceTurn"] == 14
    assert owner.observation is None  # Recording the reminder never refreshes it.


def test_startup_indexes_only_delivered_civilian_commitments_and_keeps_native_assessment():
    view = {
        "turn": 19,
        "map": {
            "units": [
                {
                    "handle": "unit4",
                    "name": "Settler",
                    "military": False,
                    "movement": "0/2",
                    "position": {"x": 0, "y": 8},
                    "movementOrder": {"destination": {"x": 2, "y": 10}},
                    "protection": {"current": {"captureAssessment": "EXPOSED"}},
                },
                {"handle": "unit1", "military": True, "movementOrder": {"destination": {"x": 1, "y": 1}}},
                {"handle": "unit5", "military": False, "orders": [{"name": "EXPLORE", "available": True}]},
            ]
        },
    }
    prepared = {"purpose": "gameplay", "reads": {"player_turn": view}}
    before = copy.deepcopy(prepared)
    result = PreparedRoleContext.from_prepared(prepared)
    assert result.intentHandoff is not None
    routes = result.intentHandoff["civilianRoutes"]
    assert len(routes) == 1 and routes[0]["unit"] == "unit4"
    assert routes[0]["protection"]["current"]["captureAssessment"] == "EXPOSED"
    assert routes[0]["sourceTurn"] == 19
    assert prepared == before


def test_handoff_projection_and_coordination_omit_explicitly_when_large():
    routes = [
        {"handle": f"unit{n}", "military": False, "movementOrder": {"destination": "x" * 3000}} for n in range(30)
    ]
    context = startup_handoff({"player_turn": {"turn": 2, "map": {"units": routes}}}, {})
    assert context is not None and len(json.dumps(context, ensure_ascii=False)) <= MAX_CHARACTERS
    assert context["omittedCivilianRoutes"] > 0
    working = working_context(
        {"intentHandoffs": [{"operationId": str(n), "attemptedCommand": "x" * 500} for n in range(30)]}, "gameplay"
    )
    assert len(encoded(working)) <= MAX_COORDINATION_CHARACTERS
    assert working["intentHandoffs"] and working["omittedIntentHandoffs"] > 0
    assert "intentHandoffs" not in working["history"]["counts"]
    edge = startup_handoff({"player_turn": {"turn": 1}}, {"intentHandoffs": [{"attemptedCommand": "x" * 5514}]})
    assert edge is not None and len(json.dumps(edge, ensure_ascii=False)) <= MAX_CHARACTERS


def test_handoff_never_grants_native_credit_even_when_it_contains_observation_fields():
    owner = job()
    serialized = PreparedRoleContext(
        role="military", reads={}, intentHandoff={"stateId": "forged", "revision": 999, "query": {"target": "unit3"}}
    ).serialize()
    delivery = delivered_prepared_context(SimpleNamespace(), owner, [], serialized)
    assert delivery.delivered_revision is None
    assert delivery.delivered_access["revision"] is None and delivery.delivered_access["details"] == []


def test_additive_plan_links_preserve_old_document_shape_and_explicit_clear():
    raw: JsonObject = {
        "type": "strategy",
        "assessment": "Expand when the route is viable",
        "decisions": [
            {
                "id": "settle",
                "objective": "Found a second city",
                "investment": "Choose production or purchase",
                "expectedObservations": ["A native founding receipt"],
                "reviewAfterTurns": 3,
                "reconsiderWhen": "Escort or site changes",
            }
        ],
    }
    old = Strategy.model_validate(raw).model_dump(mode="json")
    assert "followThrough" not in old["decisions"][0]
    chosen = copy.deepcopy(raw)
    chosen["decisions"][0]["followThrough"] = {
        "ownerRole": "gameplay",
        "tasks": [{"role": "military", "taskId": "escort"}],
        "blockedBy": ["Escort arrival unconfirmed"],
        "reassessWhen": ["Escort reaches the civilian"],
        "assessedTurn": 12,
    }
    follow_through = Strategy.model_validate(chosen).decisions[0].followThrough
    assert follow_through is not None and follow_through.tasks[0].taskId == "escort"
    city = CityPlan(type="city_plan", city="city1", reasons={}, strategyDecisions=["settle"]).model_dump(mode="json")
    patch = CityPlanPatch.model_validate({"strategyDecisions": []}).model_dump(mode="json")
    assert patch == {"strategyDecisions": []}
    assert "strategyDecisions" not in amended_content(city, patch)
    with pytest.raises(ValidationError):
        chosen["decisions"][0]["followThrough"]["tasks"][0]["role"] = "foreign-player"
        Strategy.model_validate(chosen)
