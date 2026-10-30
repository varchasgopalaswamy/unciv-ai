"""Advertised role contracts prevent observed retries without losing legal choices."""

import asyncio
import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from civ_agent.economy_roles import CITY_COMMANDS
from civ_agent.game_commands import COMMANDS, command_arguments, command_schema
from civ_agent.observations import ConstructionDetails
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.strategic_review import frozen_read
from civ_agent.tool_schemas import TradeDecision, tool_model
from civ_agent.war_state import MILITARY_COMMANDS
from tests.test_economy_optimization import managed


@pytest.mark.parametrize(
    ("arguments", "valid"),
    [
        ({"target": "city1"}, True),
        ({"target": "city1", "horizonTurns": None}, True),
        ({"target": "city1", "names": [], "horizonTurns": 10}, False),
        ({"target": "city1", "horizonTurns": 10}, False),
        ({"target": "city1", "names": ["Granary"], "horizonTurns": 10}, True),
        ({"target": "city1", "names": ["Granary"], "horizonTurns": 101}, False),
    ],
)
def test_construction_horizon_dependency_is_advertised_and_enforced(arguments, valid):
    assert Draft202012Validator(ConstructionDetails.model_json_schema()).is_valid(arguments) == valid
    if valid:
        assert ConstructionDetails.model_validate(arguments).target == "city1"
    else:
        with pytest.raises(ValidationError):
            ConstructionDetails.model_validate(arguments)


def test_review_history_is_not_advertised_but_all_supported_sections_remain():
    schema = tool_model("player_details", "review").model_json_schema()
    sections = schema["properties"]["section"]["enum"]
    assert set(sections) == {"economy", "diplomacy", "research", "policies", "map_index", "settlement"}
    assert not Draft202012Validator(schema).is_valid({"section": "turn_history"})
    assert frozen_read({}, "player_details", {"section": "turn_history"})["error"] == "REVIEW_READ_ONLY"
    assert Draft202012Validator(tool_model("player_details", "gameplay").model_json_schema()).is_valid(
        {"section": "turn_history"}
    )


@pytest.mark.parametrize("role", ["economy", "military"])
def test_specialist_completion_keeps_request_dispositions_without_main_fields(role):
    schema = tool_model("finish_role", role).model_json_schema()
    request = {"requests": [{"id": "request1", "status": "deferred", "reason": "Need research", "reviewTurn": 9}]}
    Draft202012Validator(schema).validate(request)
    assert tool_model("finish_role", role).model_validate(request).model_dump(exclude_unset=True) == request
    for args in ({"deferChoices": True}, {"deferReason": "Wait"}, {"initiativeDecisions": []}):
        assert not Draft202012Validator(schema).is_valid(args)
    assert Draft202012Validator(tool_model("finish_role", "gameplay").model_json_schema()).is_valid(
        {"deferChoices": True, "deferReason": "Wait"}
    )


@pytest.mark.parametrize("allowed", [CITY_COMMANDS, MILITARY_COMMANDS])
def test_reduced_command_schemas_preserve_every_allowed_variant_and_its_optional_fields(allowed):
    military = allowed == MILITARY_COMMANDS
    schema = command_schema(allowed, military_review=military)
    validator = Draft202012Validator(schema)
    complete = COMMANDS.json_schema()
    values = {
        "target": "unit1",
        "selection": "Worker",
        "queue": ["Mining"],
        "targets": ["notice1"],
        "destination": {"x": 0, "y": 1},
        "attackFrom": {"x": 0, "y": 0},
        "worked": False,
        "locked": True,
        "enabled": False,
        "index": 0,
        "toIndex": 1,
        "confirmWar": True,
    }
    for kind in allowed:
        reference = complete["discriminator"]["mapping"][kind]
        variant = complete["$defs"][reference.rsplit("/", 1)[1]]
        args = {"kind": kind, **{key: values[key] for key in variant["required"] if key != "kind"}}
        validator.validate(args)
        assert command_arguments(args) == args
        assert set(variant["properties"]) - {"reviewAfter"} <= schema["properties"].keys()
    assert ("reviewAfter" in schema["properties"]) == military
    assert set(schema["properties"]["kind"]["enum"]) == allowed
    for unused in ("queue", "targets", "deferReason", "deferChoices", "initiativeDecisions"):
        assert unused not in schema["properties"]
    assert len(json.dumps(schema)) < len(json.dumps(command_schema()))


def test_gateway_uses_managed_schema_but_main_retains_city_override_commands(tmp_path):
    async def run():
        async with managed(tmp_path) as fixture:
            economy = {t["name"]: t for t in await fixture.gateway.tools(fixture.economy)}
            main = {t["name"]: t for t in await fixture.gateway.tools(fixture.main)}
            assert "economy_wait" in main and "economy_wait" not in economy
            assert "queue" not in economy["player_command"]["inputSchema"]["properties"]
            assert CITY_COMMANDS <= set(main["player_command"]["inputSchema"]["properties"]["kind"]["enum"])
            assert "city_override" in main

    asyncio.run(run())


def test_trade_example_uses_the_actual_nested_intent_contract():
    tool = next(t for t in ProposalGateway.tool_catalogue([], "gameplay") if t["name"] == "trade_decide")
    example, _ = json.JSONDecoder().raw_decode(tool["description"].split("for example ", 1)[1])
    intent = TradeDecision.model_validate(example).intent
    assert intent is not None and intent.reason == "Preserve funds"
    with pytest.raises(ValidationError):
        TradeDecision.model_validate({"proposalId": "trade1", "decision": "withdraw", "reason": "Preserve funds"})
