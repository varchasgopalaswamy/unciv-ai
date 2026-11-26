"""Exercise the actual Pydantic AI tool boundary over the guarded domain executor."""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from civ_agent.decision_contracts import DecisionContract
from civ_agent.game_commands import command_schema
from civ_agent.orchestration.contracts import GatewayDependencies, GatewayJob, Scope
from civ_agent.orchestration.gateway_registry import GatewayRegistry
from civ_agent.orchestration.gateway_tools import command_type, gateway_tools
from civ_agent.proposal_gateway import READ_TOOLS
from civ_agent.schemas import BoundaryModel
from tests.domain_fixtures import managed, run_async
from tests.test_gateway_execution import executor


def dependencies(job):
    return GatewayDependencies(
        scope=Scope(game="game", player="p0", epoch=job.epoch, activation="turn1"), job=job.id, purpose=job.purpose
    )


@run_async
async def test_all_role_tools_match_permitted_catalogues_and_hide_private_controls(tmp_path):
    async with managed(tmp_path) as m:
        m.host.list_tools = AsyncMock(
            return_value=[{"name": name, "description": name} for name in READ_TOOLS | {"player_command"}]
        )
        military = await m.gateway.open("military", "military", "g1", m.host, {}, 0)
        m.roles.register(military)
        communication = await m.gateway.open("communication", "communication", "g1", m.host, {}, 0)
        for job in (m.main, m.economy, military, communication):
            job.catalogue = None
            catalogue = await m.gateway.tools(job)
            contract = DecisionContract(job.purpose, catalogue)
            invoke = AsyncMock()
            tools = gateway_tools(job.purpose, catalogue, invoke)
            names = {tool.name for tool in tools}
            reads = contract.names & (READ_TOOLS | {"war_goals"})
            assert reads <= names
            assert names <= contract.names | {"act", "finish_role", "economy_wait"}
            for tool in tools:
                schema = tool.function_schema.json_schema
                variants = schema.get("anyOf", [schema])
                for variant in variants:
                    assert "scope" not in variant["properties"] and "job" not in variant["properties"]
                assert "gatewayState" not in json.dumps(schema)
                if tool.name == "act":
                    command = next((entry for entry in catalogue if entry["name"] == "player_command"), None)
                    if command:
                        command_shape = schema["$defs"]["NativePlayerCommandArguments"]["properties"]["command"]
                        kinds = set(command_shape["discriminator"]["mapping"])
                        assert kinds == set(command["inputSchema"]["discriminator"]["mapping"])
                        assert "END_TURN" not in kinds and "AUTOPLAY" not in kinds
                elif tool.name in contract.recoverable:
                    for variant in variants:
                        assert "delivery" not in variant["properties"] and "full" not in variant["properties"]
                        assert "recoverBaseline" in variant["properties"]
            assert invoke.await_count == 0


def test_narrow_commands_keep_canonical_confirmations_and_reject_hidden_fields():
    command = TypeAdapter(command_type(command_schema({"DECLARE_WAR", "SET_CITY_FOCUS"})))
    assert command.validate_python({"kind": "DECLARE_WAR", "target": "Greece", "confirmWar": True})
    for value in (
        {"kind": "DECLARE_WAR", "target": "Greece", "confirmWar": 1},
        {"kind": "SET_CITY_FOCUS", "target": "city1", "selection": "FoodFocus", "reviewAfter": True},
        {"kind": "END_TURN"},
    ):
        with pytest.raises(ValidationError):
            command.validate_python(value)


@run_async
async def test_native_agent_inspects_acts_and_only_receives_public_receipts(tmp_path):
    async with managed(tmp_path) as m:
        _, state = await executor(m)
        calls = []
        generated = []

        async def resolve(record):
            return m.economy

        owner = GatewayRegistry(m.gateway, game="game", player="p0", resolve=resolve)
        deps = dependencies(m.economy)
        binding = GatewayJob(scope=deps.scope, job=deps.job, purpose=deps.purpose)
        await owner.register(binding, state, await m.gateway.tools(m.economy))

        async def invoke(deps, call_id, step, decision):
            assert deps == dependencies(m.economy)
            calls.append((call_id, step, decision))
            return await owner(deps, call_id, step, decision)

        async def respond(messages, info):
            generated.append(messages)
            if len(generated) == 1:
                return ModelResponse(parts=[ToolCallPart("city_details", {"arguments": {"target": "city2"}}, "read1")])
            returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
            for part in returns:
                assert isinstance(part.content, dict)
                assert "state" not in part.content and "gatewayState" not in json.dumps(part.content)
            if len(generated) == 2:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "act",
                            {
                                "operations": [
                                    {
                                        "tool": "player_command",
                                        "arguments": {
                                            "command": {
                                                "kind": "SET_CITY_FOCUS",
                                                "target": "city2",
                                                "selection": "FoodFocus",
                                            }
                                        },
                                    }
                                ]
                            },
                            "act1",
                        )
                    ]
                )
            content = returns[-1].content
            assert isinstance(content, dict) and content["receipts"][0]["status"] == "confirmed"
            return ModelResponse(parts=[ToolCallPart("done", {}, "done1")])

        agent = Agent(
            FunctionModel(respond),
            deps_type=GatewayDependencies,
            tools=gateway_tools("economy", await m.gateway.tools(m.economy), invoke),
            output_type=ToolOutput(BoundaryModel, name="done"),
            retries=0,
        )
        try:
            with agent.parallel_tool_call_execution_mode("sequential"):
                await agent.run("Choose the next city focus.", deps=deps)
            assert [call[0] for call in calls] == ["read1", "act1"]
            assert calls[0][1] < calls[1][1]
            assert owner.load(binding).state.delivery_step == calls[-1][1]
            assert len(m.host.executed) == 1
        finally:
            await owner.close()


@run_async
async def test_invalid_batch_is_rejected_by_pydantic_before_gateway_invocation(tmp_path):
    async with managed(tmp_path) as m:
        invoke = AsyncMock()

        async def respond(messages, info):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "act",
                        {
                            "operations": [
                                {"tool": "city_override", "arguments": {"city": "city1", "reason": "Emergency"}},
                                {"tool": "not_a_tool", "arguments": {}},
                            ]
                        },
                        "bad1",
                    )
                ]
            )

        agent = Agent(
            FunctionModel(respond), tools=gateway_tools("gameplay", await m.gateway.tools(m.main), invoke), retries=0
        )
        with pytest.raises(UnexpectedModelBehavior):
            await agent.run("Choose actions.", deps=dependencies(m.main))
        assert invoke.await_count == 0
        assert not m.host.executed and not m.roles.overrides
