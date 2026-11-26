"""Code Mode guidance reaches real SDK runs and its examples execute offline."""

import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.execution_policy import CodeModePolicy, ExecutionPolicy
from civ_agent.orchestration.contracts import GatewayReceipt
from civ_agent.orchestration.review import delivered_reads, read_tool
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.code_mode_instructions import NATIVE_READ_EXAMPLE, REVIEW_READ_EXAMPLE
from civ_agent.runtime.preload import COMMON_INSTRUCTIONS
from civ_agent.schemas import BoundaryModel
from tests.domain_fixtures import run_async
from tests.test_local_agents import call, request, returns
from tests.test_local_review_services import review_setup as review_setup


class ReadSummary(BoundaryModel):
    summary: str


def execution(enabled=True):
    return ExecutionPolicy(code_mode=CodeModePolicy(enabled=enabled))


def assert_instructions(info, *, enabled, report):
    instructions = info.instructions or ""
    assert instructions.startswith(COMMON_INSTRUCTIONS)
    assert ("# Code Mode execution" in instructions) is enabled
    assert ("# Code Mode report completion" in instructions) is (enabled and report)
    assert ("# Code Mode role completion" in instructions) is (enabled and not report)


@pytest.mark.parametrize("enabled", [False, True])
@run_async
async def test_role_guidance_is_policy_gated_without_requiring_extra_calls(tmp_path, enabled):
    async def invoke(*_args):
        return GatewayReceipt(result={"roleFinished": True})

    def model(_messages, info):
        assert_instructions(info, enabled=enabled, report=False)
        return call(info, "finish_role", {"summary": "Native completion already available."}, "finish")

    result = await LocalAgentRunner(
        LocalAgentServices(invoke=invoke, step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"))
    ).run_role(
        request().model_copy(update={"execution_policy": execution(enabled)}),
        startup={"user": "Finish the role.", "instructions": "Use permitted evidence."},
        catalogue=[{"name": "finish_role", "inputSchema": {}}],
        model=FunctionModel(model),
    )
    assert result.usage.requests == 1


@run_async
async def test_native_example_groups_reads_filters_results_and_reuses_repl_values(tmp_path):
    dispatched = []

    async def invoke(_deps, identifier, _step, decision):
        dispatched.append(decision)
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        tool = decision["operations"][0]["tool"]
        data = (
            {"turn": 7, "pending": [{"kind": "Research", "supported": True}, {"kind": "Other", "supported": False}]}
            if tool == "player_turn"
            else {"tools": ["player_turn"]}
        )
        return GatewayReceipt(
            result={
                "decisionId": identifier,
                "receipts": [{"operationId": identifier, "tool": tool, "status": "confirmed", "result": data}],
                "unexecuted": [],
            }
        )

    def model(messages, info):
        assert_instructions(info, enabled=True, report=False)
        history = returns(messages)
        if not history:
            return call(info, "run_code", {"code": NATIVE_READ_EXAMPLE}, "read-group")
        if len(history) == 1:
            assert history[-1].content == {"turn": 7, "supported_pending": [{"kind": "Research", "supported": True}]}
            return call(info, "run_code", {"code": "{'cached_pending_count': len(pending)}"}, "reuse")
        assert history[-1].content == {"cached_pending_count": 2}
        return call(info, "finish_role", {"summary": "Read and assessed current pending choices."}, "finish")

    await LocalAgentRunner(
        LocalAgentServices(invoke=invoke, step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"))
    ).run_role(
        request().model_copy(update={"execution_policy": execution()}),
        startup={"user": "Inspect the available evidence.", "instructions": "Use permitted evidence."},
        catalogue=[{"name": name, "inputSchema": {}} for name in ("finish_role", "player_turn", "player_capabilities")],
        model=FunctionModel(model),
    )
    assert [decision["operations"][0]["tool"] for decision in dispatched if decision["kind"] == "inspect"] == [
        "player_turn",
        "player_capabilities",
    ]


@pytest.mark.parametrize("enabled", [False, True])
@run_async
async def test_report_guidance_is_policy_gated(review_setup, tmp_path, enabled):
    _worker, original = review_setup

    async def unused(*_args):
        raise AssertionError("Reports cannot dispatch native mutations")

    def model(_messages, info):
        assert_instructions(info, enabled=enabled, report=True)
        return call(info, "submit_report", {"summary": "No new evidence needed."}, "report")

    await LocalAgentRunner(
        LocalAgentServices(invoke=unused, step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"))
    ).run_report(
        original.model_copy(update={"execution_policy": execution(enabled)}),
        startup={"user": "Review existing evidence.", "instructions": "Cite delivered facts."},
        model=FunctionModel(model),
        output_type=ReadSummary,
        name="strategic_review_v1",
    )


@run_async
async def test_review_example_keeps_individual_sources_with_filtered_data(review_setup, tmp_path):
    worker, original = review_setup
    selected = original.model_copy(update={"execution_policy": execution()})

    async def unused(*_args):
        raise AssertionError("Reports cannot dispatch native mutations")

    def model(messages, info):
        assert_instructions(info, enabled=True, report=True)
        if not returns(messages):
            return call(info, "run_code", {"code": REVIEW_READ_EXAMPLE}, "read-group")
        data = returns(messages)[-1].content
        assert isinstance(data, list) and len(data) == 2
        first, second = data
        assert isinstance(first, dict) and isinstance(second, dict)
        assert first["source"] != second["source"]
        assert first["data"] == {"turn": 5} and second["data"] == {"victories": []}
        assert delivered_reads(messages) == [
            {"name": "player_turn", "arguments": {}},
            {"name": "victory_progress", "arguments": {}},
        ]
        return call(info, "submit_report", {"summary": "Separate delivered sources retained."}, "report")

    await LocalAgentRunner(
        LocalAgentServices(invoke=unused, step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"))
    ).run_report(
        selected,
        startup={"user": "Review frozen evidence.", "instructions": "Cite delivered facts."},
        model=FunctionModel(model),
        output_type=ReadSummary,
        name="strategic_review_v1",
        tools=[read_tool("player_turn", worker=worker), read_tool("victory_progress", worker=worker)],
    )
