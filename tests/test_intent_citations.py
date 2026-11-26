"""Plan-only action intent is valid, attributable and never synthesized prose."""

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from civ_agent.game_commands import command_arguments, command_schema
from civ_agent.memory_history import action_record
from civ_agent.memory_records import Intent
from civ_agent.startup_context import prepare_startup
from tests.domain_fixtures import create, managed, run_async, setup
from tests.memory_helpers import stored_records


@pytest.mark.parametrize(
    "intent",
    [{"cityPlan": "mem12"}, {"reason": "Feed the city"}, {"cityPlan": "mem12", "reason": "Buy before the next growth"}],
)
def test_intent_schema_and_command_validation_agree_and_round_trip(intent):
    command = {"kind": "SET_CITY_FOCUS", "target": "city1", "selection": "FoodFocus", "intent": intent}
    Draft202012Validator(command_schema({"SET_CITY_FOCUS"})).validate(command)
    assert command_arguments(command)["intent"] == intent
    normalized = Intent.model_validate(intent).model_dump()
    assert Intent.model_validate(normalized).model_dump() == normalized


@pytest.mark.parametrize(
    "intent",
    [
        {},
        {"strategy": "mem12"},
        {"cityPlan": ""},
        {"cityPlan": " "},
        {"reason": " "},
        {"cityPlan": "mem12", "reason": " "},
    ],
)
def test_empty_and_blank_intent_are_rejected_consistently(intent):
    assert not Draft202012Validator(Intent.model_json_schema()).is_valid(intent)
    with pytest.raises(ValidationError):
        Intent.model_validate(intent)


@run_async
async def test_delivered_plan_only_city_command_succeeds_and_replays_without_inferred_reason(tmp_path):
    async with managed(tmp_path) as m:
        await prepare_startup(m.gateway, m.economy)
        command = {
            "kind": "SET_CITY_FOCUS",
            "target": "city2",
            "selection": "FoodFocus",
            "intent": {"cityPlan": m.plans[1]},
        }
        result = await m.gateway.call(m.economy, "player_command", command, "focus")
        assert "error" not in result
        actions = len(m.host.executed)
        assert await m.gateway.call(m.economy, "player_command", command, "focus") == result
        assert len(m.host.executed) == actions
        records = stored_records(m.memory)
        confirmed = records[m.plans[1]]["body"]["_confirmedActions"]["SET_CITY_FOCUS"]
        assert confirmed["reason"] == ""
        decision = next(
            r["body"]
            for r in records.values()
            if r["category"] == "decision" and r["body"].get("intent", {}).get("cityPlan") == m.plans[1]
        )
        assert decision["intent"]["reason"] == ""
        assert decision["_pinned"]["cityPlan"]["version"] == confirmed["planVersion"]
        summary = action_record("decision", decision, transport="player_command")
        assert summary["reason"] == "" and summary["cityPlan"] == {
            "id": m.plans[1],
            "version": confirmed["planVersion"],
        }


@pytest.mark.parametrize("reference", ["missing", "wrong_city", "undelivered"])
def test_unverified_plan_is_reported_and_never_confirmed_as_the_actions_source(tmp_path, reference):
    memory = setup(tmp_path)
    memory.start_job("author", "economy", {}, game_turn=4)
    plan = create(memory, {"type": "city_plan", "city": "city1", "reasons": {"focus": "Grow"}}, "author")
    job = "author"
    if reference == "undelivered":
        memory.start_job("reader", "economy", {}, game_turn=4)
        job = "reader"
    target = "city2" if reference == "wrong_city" else "city1"
    identifier = "mem-missing" if reference == "missing" else plan
    queued = memory.queue(
        job,
        "player_command",
        {"kind": "SET_CITY_FOCUS", "target": target, "selection": "FoodFocus"},
        "call",
        {"cityPlan": identifier},
    )
    assert queued["intentFeedback"]["unverifiedCityPlan"] == identifier
    memory.receipt(queued["id"], {"kind": "SET_CITY_FOCUS", "actionSummary": "Food focus selected."})
    assert not stored_records(memory)[plan]["body"].get("_confirmedActions")
    memory.engine.dispose()
