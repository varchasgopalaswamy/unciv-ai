"""Canonical role controls use the SDK's native single-model argument schema."""

import asyncio

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from civ_agent.orchestration.contracts import GatewayDependencies, GatewayReceipt, Scope
from civ_agent.orchestration.gateway_tools import gateway_tools


@pytest.mark.parametrize("wrapped", [False, True], ids=["flat", "wrapped"])
@pytest.mark.parametrize(
    "purpose,name,arguments,kind",
    [
        ("gameplay", "finish_role", {"deferChoices": True, "deferReason": "Reconsider next turn."}, "finish"),
        (
            "economy",
            "finish_role",
            {
                "requests": [{"id": "r1", "status": "deferred", "reason": "Save production.", "reviewTurn": 2}],
                "workerSkips": [{"targets": ["unit1"], "reason": "Wait for a safe route."}],
            },
            "finish",
        ),
        (
            "military",
            "finish_role",
            {"decisions": [{"targets": ["unit2"], "reason": "Hold position."}]},
            "finish",
        ),
        ("gameplay", "economy_wait", {"requestIds": ["r1"]}, "wait"),
    ],
)
def test_actual_agent_accepts_native_flat_and_wrapped_controls_without_changing_dispatch(
    purpose, name, arguments, kind, wrapped
):
    async def run():
        deps = GatewayDependencies(
            scope=Scope(game="game", player="p0", epoch="e1", activation="turn0"),
            job="role-job",
            purpose=purpose,
            model_step_offset=7,
        )
        calls, generated = [], []

        async def invoke(actual, call_id, step, decision):
            assert actual == deps
            calls.append((call_id, step, decision))
            return GatewayReceipt(result={"receipts": [{"status": "confirmed", "result": {"roleFinished": True}}]})

        tool = gateway_tools(purpose, [{"name": name}], invoke)[0]
        assert tool.name == name and tool.max_retries == 0 and tool.sequential
        schema = tool.function_schema.json_schema
        assert "arguments" not in schema["properties"] and "request" not in schema["properties"]
        assert set(arguments) <= schema["properties"].keys()
        wire = {"arguments": arguments} if wrapped else arguments
        validated = tool.function_schema.validator.validate_python(wire)
        assert validated["arguments"].model_dump(exclude_unset=True) == arguments

        def respond(messages, info):
            generated.append(messages)
            if len(generated) == 1:
                assert info.function_tools[0].parameters_json_schema == schema
                return ModelResponse(parts=[ToolCallPart(name, wire, "control1")])
            returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
            assert len(returns) == 1 and returns[0].outcome == "success"
            return ModelResponse(parts=[TextPart("Role control acknowledged.")])

        agent = Agent(FunctionModel(respond), deps_type=GatewayDependencies, tools=[tool], retries=0)
        result = await agent.run("Complete this role decision.", deps=deps)
        assert result.output == "Role control acknowledged."
        assert result.usage.requests == len(generated) == 2
        assert calls == [("control1", 8, {"kind": kind, "arguments": arguments})]

    asyncio.run(run())


@pytest.mark.parametrize("wrapped", [False, True], ids=["flat", "wrapped"])
def test_native_control_validator_rejects_malformed_values_before_dispatch_without_tool_retries(wrapped):
    async def run():
        calls = []

        async def invoke(*args):
            calls.append(args)
            raise AssertionError("Malformed control reached the trusted gateway")

        tool = gateway_tools("economy", [{"name": "finish_role"}], invoke)[0]
        arguments = {"requests": [], "workerSkips": [{"targets": [], "reason": "Wait."}]}
        wire = {"arguments": arguments} if wrapped else arguments
        with pytest.raises(ValidationError):
            tool.function_schema.validator.validate_python(wire)

        def respond(messages, info):
            return ModelResponse(parts=[ToolCallPart("finish_role", wire, "bad-control")])

        deps = GatewayDependencies(
            scope=Scope(game="game", player="p0", epoch="e1", activation="turn0"),
            job="economy-job",
            purpose="economy",
        )
        agent = Agent(FunctionModel(respond), deps_type=GatewayDependencies, tools=[tool], retries=0)
        with pytest.raises(UnexpectedModelBehavior):
            await agent.run("Complete this role decision.", deps=deps)
        assert not calls

    asyncio.run(run())
