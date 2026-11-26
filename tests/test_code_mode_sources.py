"""Nested command attribution uses the causal private child frontier."""

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness import CodeMode

from civ_agent.orchestration.authored_plan_tools import authored_plan_capability
from civ_agent.orchestration.authored_sources import authored_sources
from civ_agent.orchestration.contracts import GatewayDependencies
from civ_agent.orchestration.gateway_execution import native_call_id
from civ_agent.orchestration.store import WorkflowStore
from civ_agent.orchestration.transcripts import PlayerPersistence, backend
from civ_agent.runtime.code_mode import NativeFailureFence
from tests.domain_fixtures import run_async
from tests.test_authored_memory import operation, setup
from tests.test_authored_sources import command_call, strategy
from tests.test_local_agents import call, returns


@pytest.mark.parametrize("long_id", [False, True])
@run_async
async def test_nested_command_attributes_only_prior_settled_authored_reads(tmp_path, long_id):
    async with setup(tmp_path / "gateway") as fixture:
        workflow = WorkflowStore(tmp_path / "workflow", player="p0")
        workflow.bind_epoch("game", "g1")
        try:
            await fixture.proxy.write(
                fixture.prefix + "strategy.md", strategy(), expected_version=None, operation=operation("seed")
            )
            steps = backend(workflow, create=True)
            captures = []

            def respond(messages, info):
                if not returns(messages):
                    return call(
                        info,
                        "run_code",
                        {
                            "code": (
                                "before = execute_commands()\n"
                                "observed = await read_strategy()\n"
                                "after = execute_commands()\nNone"
                            )
                        },
                        "p" * 128 if long_id else "program",
                    )
                return ModelResponse(parts=[TextPart("done")])

            agent = Agent(
                FunctionModel(respond),
                name="player_role_v1",
                deps_type=GatewayDependencies,
                capabilities=[
                    authored_plan_capability(fixture.client, enabled=lambda ctx: True),
                    PlayerPersistence(store=steps, capture_frontier=True),
                    NativeFailureFence(),
                    CodeMode(dynamic_catalog=False),
                ],
            )

            @agent.tool(sequential=True)
            async def execute_commands(ctx: RunContext[GatewayDependencies]) -> str:
                assert ctx.run_id is not None and ctx.tool_call_id is not None
                captures.append(
                    await authored_sources(
                        workflow,
                        fixture.identity,
                        ctx.run_id,
                        command_call(call_id=native_call_id(ctx.tool_call_id), model_step=ctx.run_step),
                        fixture.service.store,
                    )
                )
                return "accepted"

            with agent.parallel_tool_call_execution_mode("sequential"):
                result = await agent.run(
                    "Read authored strategy before attribution.",
                    deps=GatewayDependencies(**fixture.identity.model_dump()),
                )
            assert result.output == "done" and captures[0] == {}
            source = captures[1]["strategy.md"]
            assert source["version"] == "1" and source["source"]["toolName"] == "read_strategy"
            assert source["source"]["toolCallId"].endswith("__2")
            assert source["body"]["_decisionBaselines"]["capital"]["id"]
        finally:
            workflow.close()
