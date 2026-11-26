"""Canonical read fields reach native agents without changing gateway operations."""

import asyncio

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from civ_agent.decision_contracts import NATIVE_MODELS
from civ_agent.orchestration.contracts import GatewayDependencies, GatewayReceipt, PlayerRole, Scope
from civ_agent.orchestration.gateway_tools import gateway_tools, read_arguments
from civ_agent.tool_schemas import tool_model


def read_tool(name, invoke, *, purpose: PlayerRole = "military"):
    model = NATIVE_MODELS.get(name) or tool_model(name, purpose)
    shape = model.model_json_schema()
    if name == "chat_read" and purpose != "gameplay":
        shape["properties"].pop("handledMessages")
    catalogue = [{"name": name, "inputSchema": shape}]
    return next(tool for tool in gateway_tools(purpose, catalogue, invoke) if tool.name == name)


def dependencies():
    return GatewayDependencies(
        scope=Scope(game="game", player="p0", epoch="e1", activation="turn0"),
        job="military-job",
        purpose="military",
        model_step_offset=7,
    )


@pytest.mark.parametrize("wrapped", [False, True], ids=["flat", "legacy"])
@pytest.mark.parametrize(
    "name,arguments,recovery",
    [
        ("player_details", {"section": "economy"}, None),
        ("player_details", {"section": "diplomacy"}, "missing"),
        ("player_details", {"section": "research"}, "mismatched"),
        ("player_turn", {}, None),
        ("player_turn", {}, "missing"),
        ("movement_preview", {"target": "unit1", "destination": {"x": 1, "y": 2}}, None),
        ("chat_read", {"includeContext": True}, None),
        ("chat_history", {"with": "Greece"}, None),
        ("unit_details", {"target": "unit1", "sections": ["movement", "movement"]}, None),
        ("war_goals", {"action": "read"}, None),
    ],
)
def test_actual_agent_accepts_flat_and_legacy_reads_and_preserves_gateway_envelopes(name, arguments, recovery, wrapped):
    async def run():
        calls, generated = [], []

        async def invoke(deps, call_id, step, decision):
            assert deps == dependencies()
            calls.append((call_id, step, decision))
            return GatewayReceipt(result={"receipts": [{"status": "confirmed", "result": {"read": True}}]})

        tool = read_tool(name, invoke)
        schema = tool.function_schema.json_schema
        assert "arguments" not in schema["properties"] and "request" not in schema["properties"]
        assert "delivery" not in schema["properties"] and "full" not in schema["properties"]
        assert tool.max_retries == 0 and tool.sequential
        if name == "chat_read":
            assert "handledMessages" not in schema["properties"]
        wire = {"arguments": arguments} if wrapped else dict(arguments)
        if recovery is not None or name == "player_turn":
            wire["recoverBaseline"] = recovery
        canonical = read_arguments(
            name, NATIVE_MODELS.get(name) or tool_model(name, "military"), {"inputSchema": schema}
        )
        expected = canonical.model_validate(arguments).model_dump(by_alias=True, exclude_unset=True)
        operation = {"tool": name, "arguments": expected}
        if recovery is not None:
            operation["recoverBaseline"] = recovery

        def respond(messages, info):
            generated.append(messages)
            if len(generated) == 1:
                assert info.function_tools[0].parameters_json_schema == schema
                return ModelResponse(parts=[ToolCallPart(name, wire, "read1")])
            returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
            assert len(returns) == 1 and returns[0].outcome == "success"
            return ModelResponse(parts=[TextPart("Read acknowledged.")])

        result = await Agent(FunctionModel(respond), tools=[tool], deps_type=GatewayDependencies, retries=0).run(
            "Inspect the permitted information.", deps=dependencies()
        )
        assert result.output == "Read acknowledged."
        assert result.usage.requests == len(generated) == 2 and result.usage.tool_calls == 1
        assert calls == [("read1", 8, {"kind": "inspect", "operations": [operation]})]

    asyncio.run(run())


@pytest.mark.parametrize("wrapped", [False, True], ids=["flat", "legacy"])
@pytest.mark.parametrize(
    "name,arguments",
    [
        ("player_details", {"section": "economy", "delivery": "full"}),
        ("player_details", {"section": "economy", "full": True}),
        ("chat_read", {"includeContext": True, "handledMessages": ["message1"]}),
        ("movement_preview", {"target": "unit1", "destination": {"x": True, "y": 2}}),
        ("construction_details", {"target": "city1", "horizonTurns": 10}),
        ("war_goals", {"action": "finalize", "episodeId": "episode1"}),
    ],
)
def test_native_read_validation_rejects_hidden_controls_and_canonical_invalid_arguments(name, arguments, wrapped):
    async def run():
        calls = []

        async def forbidden(*args):
            calls.append(args)
            raise AssertionError("Invalid read reached the gateway")

        tool = read_tool(name, forbidden)
        wire = {"arguments": arguments} if wrapped else arguments
        with pytest.raises(ValidationError):
            tool.function_schema.validator.validate_python(wire)

        def respond(messages, info):
            return ModelResponse(parts=[ToolCallPart(name, wire, "invalid-read")])

        with pytest.raises(UnexpectedModelBehavior):
            await Agent(FunctionModel(respond), tools=[tool], deps_type=GatewayDependencies, retries=0).run(
                "Inspect.", deps=dependencies()
            )
        assert calls == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "name,wire",
    [
        ("player_details", {"arguments": {"section": "economy"}, "unknown": True}),
        ("player_details", {"arguments": {"section": "economy"}, "section": "diplomacy"}),
        ("player_details", {"arguments": {"section": "economy", "recoverBaseline": "missing"}}),
        ("player_details", {"section": "economy", "recoverBaseline": ""}),
        ("player_details", {"arguments": {"section": "economy"}, "recoverBaseline": "invented"}),
        ("chat_read", {"includeContext": True, "recoverBaseline": "missing"}),
        ("chat_read", {"arguments": {"includeContext": True}, "recoverBaseline": None}),
    ],
)
def test_legacy_read_unwrapping_preserves_closed_envelopes_and_recovery_capabilities(name, wire):
    async def forbidden(*args):
        raise AssertionError("Validation must not invoke the gateway")

    with pytest.raises(ValidationError):
        read_tool(name, forbidden).function_schema.validator.validate_python(wire)


def test_root_model_inspection_keeps_its_existing_explicit_wrapper():
    async def forbidden(*args):
        raise AssertionError("Validation must not invoke the gateway")

    tool = read_tool("player_inspect", forbidden)
    assert set(tool.function_schema.json_schema["properties"]) == {"arguments"}
    kwargs = tool.function_schema.validator.validate_python({"arguments": {"subject": "POLITICS"}})
    assert kwargs["request"].arguments.root.subject == "POLITICS"
    with pytest.raises(ValidationError):
        tool.function_schema.validator.validate_python({"subject": "POLITICS"})
