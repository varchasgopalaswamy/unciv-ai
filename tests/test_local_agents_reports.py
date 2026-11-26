"""Plain SDK report runs preserve typed strategic and peace consultation contracts."""

import pytest
from pydantic_ai import ModelRetry, Tool
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.orchestration.contracts import ModelSelection, PeacePhase, PeaceRequest, Reference, ReviewRequest, Scope
from civ_agent.orchestration.scripted_model import scripted_peace_model, scripted_review_model
from civ_agent.peace_contracts import PeaceApproval, TacticalOverview
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.strategic_review import StrategicAdvice
from tests.domain_fixtures import run_async


def scope():
    return Scope(game="game", player="player", epoch="epoch", activation="activation")


async def unused(*_):
    raise AssertionError("A report cannot execute an ordinary native action")


@run_async
async def test_report_reads_continue_past_legacy_request_limit(tmp_path):
    calls = 0
    reads = 0

    def model(_messages, _info):
        nonlocal calls
        calls += 1
        if calls <= 30:
            return ModelResponse(parts=[ToolCallPart("read_terms", {}, f"read-{calls}")])
        return ModelResponse(
            parts=[ToolCallPart("submit_report", {"approve": True, "reason": "Reviewed terms."}, "done")]
        )

    async def read_terms():
        nonlocal reads
        reads += 1
        return {"source": "terms", "data": {"acceptable": True}}

    request = ReviewRequest(
        scope=scope(), inputs=Reference(sha256="a" * 64), selection=ModelSelection(model="test"), request_limit=2
    )
    runner = LocalAgentRunner(
        LocalAgentServices(invoke=unused, step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"))
    )
    result = await runner.run_report(
        request,
        startup={"user": "Terms.", "instructions": "Read and decide."},
        model=FunctionModel(model),
        output_type=PeaceApproval,
        name="peace_main_v1",
        tools=[Tool(read_terms)],
    )
    assert result.report.approve and reads == 30
    assert result.usage.requests == calls == 31


@pytest.mark.parametrize("phase_name", ["military", "gameplay"])
@run_async
async def test_typed_peace_report_runs_without_workflow_or_global_services(tmp_path, phase_name):
    request = PeaceRequest(
        scope=scope(),
        basis="a" * 64,
        inputs=Reference(sha256="b" * 64),
        selection=ModelSelection(model="scripted-v1"),
    )
    phase = PeacePhase(request=request, phase=phase_name)
    store = SqliteStepStore(database=tmp_path / "steps.sqlite3")
    runner = LocalAgentRunner(LocalAgentServices(invoke=unused, step_store=store))
    expected = TacticalOverview if phase_name == "military" else PeaceApproval
    result = await runner.run_report(
        phase,
        startup={"user": "Exact bound peace terms.", "instructions": "Evaluate only the supplied terms."},
        model=scripted_peace_model(phase),
        output_type=expected,
        name="peace_" + phase_name + "_v1",
    )
    assert isinstance(result.report, expected) and result.usage.requests == 1
    assert await store.latest_snapshot(run_id=result.run_id) is not None


@run_async
async def test_strategic_report_validation_retries_inside_sdk_before_publication(tmp_path):
    request = ReviewRequest(
        scope=scope(), inputs=Reference(sha256="a" * 64), selection=ModelSelection(model="scripted-v1")
    )
    model = scripted_review_model(request)
    seen = []

    async def validate(ctx, report):
        assert isinstance(report, StrategicAdvice)
        seen.append((ctx.run_id, report))
        if len(seen) == 1:
            raise ModelRetry("Cite the exact frozen evidence for this report.")
        return report

    store = SqliteStepStore(database=tmp_path / "steps.sqlite3")
    runner = LocalAgentRunner(LocalAgentServices(invoke=unused, step_store=store))
    result = await runner.run_report(
        request,
        startup={"user": "Frozen permitted evidence.", "instructions": "Do not claim unseen evidence."},
        model=model,
        output_type=StrategicAdvice,
        name="strategic_review_v1",
        validate=validate,
    )
    assert len(seen) == 2 and seen[0][0] == seen[1][0] == result.run_id
    assert result.usage.requests == 2 and result.report == seen[-1][1]


@run_async
async def test_malformed_typed_report_uses_sdk_correction_without_running_domain_actions(tmp_path):
    attempts = []

    def model(_messages, _info):
        attempts.append(True)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "submit_report",
                    {"approve": True, "reason": "" if len(attempts) == 1 else "Exact terms accepted."},
                    "report" + str(len(attempts)),
                )
            ]
        )

    request = ReviewRequest(scope=scope(), inputs=Reference(sha256="a" * 64), selection=ModelSelection(model="test"))
    runner = LocalAgentRunner(
        LocalAgentServices(invoke=unused, step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"))
    )
    result = await runner.run_report(
        request,
        startup={"user": "Terms.", "instructions": "Decide."},
        model=FunctionModel(model),
        output_type=PeaceApproval,
        name="peace_main_v1",
    )
    assert result.report.reason == "Exact terms accepted." and result.usage.requests == 2


@run_async
async def test_review_output_corrections_keep_the_configured_attempt_limit(tmp_path):
    request = ReviewRequest(
        scope=scope(), inputs=Reference(sha256="a" * 64), selection=ModelSelection(model="scripted-v1"), attempt_limit=2
    )
    attempts = []

    async def reject(ctx, _report):
        attempts.append(ctx.run_id)
        raise ModelRetry("Report must cite permitted evidence.")

    store = SqliteStepStore(database=tmp_path / "steps.sqlite3")
    runner = LocalAgentRunner(LocalAgentServices(invoke=unused, step_store=store))
    with pytest.raises(UnexpectedModelBehavior):
        await runner.run_report(
            request,
            startup={"instructions": "Review the evidence.", "user": "Frozen evidence."},
            model=scripted_review_model(request),
            output_type=StrategicAdvice,
            name="strategic_review_v1",
            validate=reject,
        )
    assert len(attempts) == request.attempt_limit and len(set(attempts)) == 1
