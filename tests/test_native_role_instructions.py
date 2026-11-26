"""Native player missions retain role intent and name the callable protocol."""

import pytest

from civ_agent.game_commands import command_schema
from civ_agent.orchestration.authored_plan_tools import authored_plan_capability
from civ_agent.orchestration.gateway_tools import gateway_tools
from civ_agent.orchestration.role_instructions import native_role_instructions
from civ_agent.player_prompts import (
    CAMPAIGN_GUIDANCE,
    COMMUNICATION_INSTRUCTIONS,
    DIPLOMACY_GUIDANCE,
    ECONOMY_ROLE_MISSION,
    EVIDENCE_GUIDANCE,
    MAIN_ROLE_MISSION,
    MILITARY_RECOMMENDATION_GUIDANCE,
    MILITARY_ROLE_MISSION,
    PREWAR_GUIDANCE,
    RECOVERY_GUIDANCE,
    STRATEGY_GUIDANCE,
    VICTORY_HORIZON_GUIDANCE,
)
from civ_agent.tool_schemas import tool_model


@pytest.mark.parametrize(
    ("purpose", "mission"),
    [
        ("gameplay", MAIN_ROLE_MISSION),
        ("economy", ECONOMY_ROLE_MISSION),
        ("military", MILITARY_ROLE_MISSION),
        ("communication", COMMUNICATION_INSTRUCTIONS),
    ],
)
def test_native_roles_deliver_their_mission_with_native_completion(purpose, mission):
    instructions = native_role_instructions(purpose)
    assert instructions.count(mission) == 1
    assert "otherwise play to win" in instructions
    assert instructions.count(EVIDENCE_GUIDANCE) == 1
    assert instructions.count(RECOVERY_GUIDANCE) == 1
    assert "execute_commands(commands=[...])" in instructions
    assert "act(operations=" in instructions
    assert "advertised arguments and a short factual summary" in instructions
    assert "Accepted finish_role completes this run" in instructions
    assert "finish_report" not in instructions
    assert "Only the host advances the native turn" in instructions
    assert "historical completion does not execute a command or finish this pass" in instructions
    # Role missions must survive protocol migration without importing the old
    # response envelope or a second, conflicting plan-editing interface.
    for legacy in (
        "decision schema",
        "finish decision",
        "wait decision",
        "inspect decision",
        "submit_report",
        "finish.arguments",
        "player_command.arguments.command",
        "memory_search(type=city_plan",
        "notebook",
        "strategy.md",
        "old_text",
        "save_city_plan(plan=",
        "save_strategy(strategy=",
        "economy_wait(arguments=",
    ):
        assert legacy not in instructions


def test_native_main_retains_strategy_and_social_agency_without_an_optional_guide():
    instructions = native_role_instructions("gameplay")
    for guidance in (
        STRATEGY_GUIDANCE,
        PREWAR_GUIDANCE,
        CAMPAIGN_GUIDANCE,
        VICTORY_HORIZON_GUIDANCE,
        DIPLOMACY_GUIDANCE,
    ):
        assert instructions.count(guidance) == 1
    assert "resolved required decisions" in instructions
    assert "read_strategy and save_strategy" in instructions
    assert "economy_wait(requestIds=[...])" in instructions
    assert "Do not poll player_turn while waiting" in instructions
    assert "native turnControls permit it and you choose to do so" in instructions
    assert "deferChoices=true" in instructions


def test_native_economy_distinguishes_a_plan_from_a_construction_action_or_escalation():
    instructions = native_role_instructions("economy")
    for responsibility in ("construction queues", "unescorted workers", "explicit city overrides"):
        assert responsibility in instructions
    assert "QUEUE_CONSTRUCTION" in instructions
    assert "Saving a city plan does not place anything in its native queue" in instructions
    assert "read_city_plan(city=...) and save_city_plan(city=..., reasons=...)" in instructions
    assert "Plan edits are optional" in instructions
    assert "finish_role(escalateChoices=...)" in instructions
    assert "does not claim that construction was selected or defer the native choice" in instructions
    assert "requests argument as completed, deferred or rejected with a reason" in instructions
    assert "finish_role(workerSkips=[...])" in instructions
    assert "other roles' units are not worker skips" in instructions


def test_native_military_uses_current_review_fields_and_preserves_reasoned_holds():
    instructions = native_role_instructions("military")
    assert instructions.count(MILITARY_RECOMMENDATION_GUIDANCE) == 1
    assert instructions.count(PREWAR_GUIDANCE) == 1
    assert instructions.count(CAMPAIGN_GUIDANCE) == 1
    assert "finish_role(decisions=[...])" in instructions
    assert "host binds the delivered review basis" in instructions
    assert "reviewAfter=true" in instructions
    assert "follow its remaining pages" in instructions
    assert "Choosing to hold or accept risk remains legal" in instructions
    assert "never requires an attack or retreat" in instructions


def test_native_communication_keeps_its_bounded_message_mission():
    instructions = native_role_instructions("communication")
    assert "Leave built-in AI trade and friendship decisions for your gameplay turn" in instructions
    assert "Speak only for your own civilization" in instructions
    assert "Main's approval of the exact terms" in instructions
    assert instructions.count(DIPLOMACY_GUIDANCE) == 1
    assert "economy_wait(" not in instructions


def test_native_plan_examples_match_the_advertised_flat_tool_fields():
    def unused_client():
        raise AssertionError("Prompt/schema checks must not dispatch any operation")

    tools = authored_plan_capability(unused_client).get_toolset().tools
    for name, purpose, arguments, signature in (
        (
            "save_city_plan",
            "economy",
            {"city": "city1", "reasons": {"construction": "Build a Monument"}},
            "save_city_plan(city=..., reasons=...)",
        ),
        (
            "save_strategy",
            "gameplay",
            {"assessment": "Develop before expanding", "reasons": ["Our cities need production"]},
            "save_strategy(assessment=..., reasons=...)",
        ),
    ):
        tool = tools[name]
        assert signature in native_role_instructions(purpose)
        assert set(arguments) <= tool.function_schema.json_schema["properties"].keys()
        assert not {"plan", "strategy"} & tool.function_schema.json_schema["properties"].keys()
        tool.function_schema.validator.validate_python(arguments)


@pytest.mark.parametrize(
    ("purpose", "name", "fields", "signature"),
    [
        ("gameplay", "economy_wait", {"requestIds"}, "economy_wait(requestIds=[...])"),
        ("gameplay", "finish_role", {"deferChoices"}, "finish_role(deferChoices=true)"),
        ("economy", "finish_role", {"workerSkips"}, "finish_role(workerSkips=[...])"),
        ("economy", "finish_role", {"escalateChoices"}, "finish_role(escalateChoices=...)"),
        ("military", "finish_role", {"decisions"}, "finish_role(decisions=[...])"),
    ],
)
def test_native_control_examples_match_generated_tool_schemas(purpose, name, fields, signature):
    async def unused_gateway(*_args):
        raise AssertionError("Prompt/schema checks must not dispatch any operation")

    catalogue = [
        {"name": "player_command", "inputSchema": command_schema({"QUEUE_CONSTRUCTION"})},
        {"name": name, "inputSchema": tool_model(name, purpose).model_json_schema()},
    ]
    tools = {tool.name: tool for tool in gateway_tools(purpose, catalogue, unused_gateway)}
    schema = tools[name].function_schema.json_schema
    assert signature in native_role_instructions(purpose)
    assert fields <= schema["properties"].keys()
    assert "arguments" not in schema["properties"]
