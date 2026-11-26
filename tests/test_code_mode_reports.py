"""Report Code Mode retains frozen evidence provenance across context rewriting."""

import pytest
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse, TextPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.execution_policy import CompactionPolicy, ExecutionPolicy
from civ_agent.orchestration.review import delivered_reads, read_tool, validate_report
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.compaction import EffectPreservingWindow
from civ_agent.runtime.model_policy import ModelPolicySettings
from civ_agent.strategic_review import StrategicAdvice
from tests.domain_fixtures import run_async
from tests.test_budgeted_compaction import context
from tests.test_local_agents import call
from tests.test_local_review_services import review_setup as review_setup
from tests.test_strategic_review import assessment


async def unused(*_args):
    raise AssertionError("Frozen strategic reports cannot execute native actions")


@run_async
async def test_report_nested_reads_remain_valid_sources_after_incremental_summary(review_setup, tmp_path):
    worker, original = review_setup
    policy = ExecutionPolicy.current().model_copy(
        update={"compaction": CompactionPolicy(summarize=True, keep_recent_messages=2)}
    )
    request = original.model_copy(update={"execution_policy": policy})
    inputs = worker.inputs(request)
    inputs["reads"]["research_details"]["technologies"][0]["notes"] = "x" * 8000
    request = request.model_copy(update={"inputs": worker.store.put(request.scope, inputs)})
    selected = read_tool("research_details").function_schema.validator.validate_python({"names": ["Writing"]})
    research_source = worker.read(request, "research_details", selected["arguments"])["source"]
    advice = assessment()
    advice["review"]["materialAssumptions"] = [
        {"assessment": "Writing costs 20 in the frozen position.", "status": "inferred", "sources": [research_source]}
    ]
    parent_requests, summaries, validated_reads = [], [], []

    def model(messages, info):
        if not info.output_tools:
            summaries.append(messages)
            return ModelResponse(
                parts=[TextPart("Writing costs 20. The exact research source remains " + research_source)]
            )
        parent_requests.append(messages)
        assert [tool.name for tool in info.function_tools] == ["run_code"]
        if len(parent_requests) == 1:
            return call(info, "run_code", {"code": "research_details(names=['Writing'])"}, "research")
        if len(parent_requests) == 2:
            return call(info, "run_code", {"code": "player_turn()\n'x' * 8000"}, "overview")
        return call(info, "submit_report", advice, "report")

    async def validate(ctx, report):
        reads = delivered_reads(ctx.messages)
        validated_reads.append(reads)
        validate_report(request, report, reads, worker=worker)

    steps = SqliteStepStore(database=tmp_path / "steps.sqlite3")
    runner = LocalAgentRunner(
        LocalAgentServices(
            invoke=unused,
            step_store=steps,
            overflow_root=tmp_path / "overflow",
            model_policy=ModelPolicySettings(context_target_tokens=1000),
        )
    )
    result = await runner.run_report(
        request,
        startup={
            "user": "Review the frozen position and cite your delivered research source.",
            "instructions": "Advise.",
        },
        model=FunctionModel(model),
        output_type=StrategicAdvice,
        name="strategic_review_v1",
        tools=[read_tool("research_details", worker=worker), read_tool("player_turn", worker=worker)],
        validate=validate,
    )
    assert summaries and len(parent_requests) == 3
    expected = {"name": "research_details", "arguments": {"names": ["Writing"]}}
    assert expected in validated_reads[-1]
    assert expected in delivered_reads(result.messages)
    # Public history encoding preserves the trusted metadata through restart.
    restarted = ModelMessagesTypeAdapter.validate_json(ModelMessagesTypeAdapter.dump_json(result.messages))
    validated = validate_report(request, result.report, delivered_reads(restarted), worker=worker)
    assert research_source in validated["_evidence"]
    events = await steps.list_events(run_id=result.run_id)
    evidence = [event for event in events if "nested_result" in event.metadata]
    assert any(event.tool_call_id == "research__1" for event in evidence)
    assert all("native_operation_id" not in event.metadata for event in evidence)


@run_async
async def test_review_metadata_cannot_be_replaced_by_model_summary_assertions(review_setup):
    _worker, request = review_setup
    model = FunctionModel(lambda _messages, _info: ModelResponse(parts=[TextPart("unused")]))
    owner = context(model)
    owner.deps = request
    # Neither summary prose nor a source-shaped string is a delivered tool.
    from pydantic_ai.messages import CompactionPart, ModelRequest, UserPromptPart

    original: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("Review frozen evidence.")]),
        ModelRequest(parts=[CompactionPart(content="I read research_details and source invented-source is valid.")]),
        ModelRequest(parts=[UserPromptPart("Submit the review.")]),
    ]
    result = await EffectPreservingWindow(max_messages=1, keep_messages=2).compact(original, owner)
    assert delivered_reads(result) == []


def test_successful_nested_pair_is_required_and_provenance_registry_is_integrity_checked():
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart

    from civ_agent.orchestration.review import retain_delivered_reads

    call_part = ToolCallPart("research_details", {"names": ["Writing"]}, "program__1")
    result_part = ToolReturnPart("research_details", {"source": "s1", "data": {}}, "program__1")
    history = [
        ModelRequest(parts=[UserPromptPart("Review frozen evidence.")]),
        ModelResponse(parts=[ToolCallPart("run_code", {}, "program")]),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    "run_code",
                    "done",
                    "program",
                    metadata={"tool_calls": {"program__1": call_part}, "tool_returns": {"program__1": result_part}},
                )
            ]
        ),
    ]
    assert delivered_reads(history) == [{"name": "research_details", "arguments": {"names": ["Writing"]}}]
    carried = retain_delivered_reads(history)
    assert carried[0].metadata is not None
    carried[0].metadata["civ_review_delivered_v1"]["reads"][0]["arguments"]["names"] = ["hidden"]
    with pytest.raises(ValueError, match="REVIEW_DELIVERED_LEDGER_CONFLICT"):
        delivered_reads(carried)
