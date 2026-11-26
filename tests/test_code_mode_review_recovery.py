"""Frozen query mistakes are recoverable; trusted-state failures still fence Code Mode."""

import json

import pytest
from pydantic import ValidationError
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.execution_policy import CodeModePolicy, ExecutionPolicy
from civ_agent.observations import DetailResult
from civ_agent.orchestration import review
from civ_agent.orchestration.review import delivered_reads, read_tool, validate_report
from civ_agent.orchestration.store import JournalConflict
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.strategic_review import FrozenReadArgumentError, StrategicAdvice
from tests.domain_fixtures import run_async
from tests.test_local_agents import call
from tests.test_local_review_services import review_setup as review_setup
from tests.test_strategic_review import assessment


async def unused(*_args):
    raise AssertionError("Frozen reviews cannot execute native actions")


def frozen_map_request(worker, request):
    inputs = worker.inputs(request)
    inputs["observation"]["map"]["tiles"] = [{"position": {"x": 0, "y": 0}, "terrain": "Grassland", "visible": True}]
    return request.model_copy(
        update={
            "inputs": worker.store.put(request.scope, inputs),
            "execution_policy": ExecutionPolicy(code_mode=CodeModePolicy(enabled=True)),
        }
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {"tool": "map_region", "arguments": {"radius": 3}},
        {"tool": "map_region", "arguments": {"center": {"x": 0, "y": 0}, "radius": 5}},
        {"tool": "city_details", "arguments": {"target": ""}},
    ],
)
def test_entire_frozen_bundle_is_validated_before_any_projection(review_setup, monkeypatch, invalid):
    from civ_agent import observations

    worker, request = review_setup
    projected = []

    def projection(*args):
        projected.append(args)
        raise AssertionError("Malformed bundles must not project earlier queries")

    monkeypatch.setattr(observations, "project_detail", projection)
    supplied = read_tool("inspect_bundle").function_schema.validator.validate_python(
        {"queries": [{"tool": "player_details", "arguments": {"section": "economy"}}, invalid]}
    )
    with pytest.raises(ModelRetry) as rejected:
        worker.read(request, "inspect_bundle", supplied["arguments"])
    assert isinstance(rejected.value.__cause__, FrozenReadArgumentError)
    assert projected == []


def test_frozen_argument_feedback_identifies_missing_target_without_raw_input(review_setup):
    worker, request = review_setup
    supplied = read_tool("inspect_bundle").function_schema.validator.validate_python(
        {"queries": [{"tool": "construction_details", "arguments": {"names": ["PRIVATE_INPUT_VALUE"]}}]}
    )
    with pytest.raises(ModelRetry) as rejected:
        worker.read(request, "inspect_bundle", supplied["arguments"])
    message = rejected.value.message
    assert all(fragment in message for fragment in ("construction_details", '"target"', '"missing"', "Field required"))
    assert "PRIVATE_INPUT_VALUE" not in message and '"input"' not in message and "https://" not in message


def test_frozen_argument_feedback_caps_diagnostics_and_preserves_schema_fields(review_setup):
    worker, request = review_setup
    supplied = read_tool("inspect_bundle").function_schema.validator.validate_python(
        {
            "queries": [
                {
                    "tool": "map_region",
                    "arguments": {f"unknown_{index}_" + "x" * 300: "PRIVATE_INPUT_VALUE" for index in range(40)},
                }
            ]
        }
    )
    with pytest.raises(ModelRetry) as rejected:
        worker.read(request, "inspect_bundle", supplied["arguments"])
    error = rejected.value.__cause__
    assert isinstance(error, FrozenReadArgumentError)
    assert 0 < len(error.diagnostics) <= 1000
    details = json.loads(error.diagnostics)
    assert 1 <= len(details) <= 4 and set(details[0]) == {"loc", "type", "msg"}
    assert "PRIVATE_INPUT_VALUE" not in rejected.value.message and len(rejected.value.message) < 1300


@run_async
async def test_code_mode_corrects_malformed_bundle_then_submits_grounded_report(review_setup, tmp_path):
    worker, original = review_setup
    request = frozen_map_request(worker, original)
    requested, validated = [], []
    steps = SqliteStepStore(database=tmp_path / "steps.sqlite3")

    def respond(messages, info):
        requested.append(messages)
        if len(requested) == 1:
            return call(
                info,
                "run_code",
                {
                    "code": (
                        "inspect_bundle(queries=["
                        "{'tool': 'player_details', 'arguments': {'section': 'economy'}},"
                        "{'tool': 'construction_details', 'arguments': {'names': ['Granary']}}])\n"
                        "player_turn()"
                    )
                },
                "malformed",
            )
        if len(requested) == 2:
            feedback = [
                part
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart) and part.tool_name == "run_code"
            ]
            assert feedback[-1].outcome == "retried"
            detail = str(feedback[-1].content).lower()
            assert "target" in detail and ("required" in detail or "missing" in detail), detail
            return call(
                info,
                "run_code",
                {"code": "map_region(center={'x': 0, 'y': 0}, radius=0, format='full')"},
                "corrected",
            )
        returned = next(
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_call_id == "corrected"
        )
        envelope = returned.metadata["tool_returns"]["corrected__1"].content
        assert envelope["data"]["available"]
        assert envelope["data"]["data"]["tiles"][0]["terrain"] == "Grassland"
        report = assessment()
        report["review"]["materialAssumptions"] = [
            {
                "assessment": "Grassland is observed at the inspected position.",
                "status": "inferred",
                "sources": [envelope["source"]],
            }
        ]
        return call(info, "submit_report", report, "report")

    async def validate(ctx, report):
        validated.append(validate_report(request, report, delivered_reads(ctx.messages), worker=worker))

    result = await LocalAgentRunner(LocalAgentServices(invoke=unused, step_store=steps)).run_report(
        request,
        startup={"user": "Review the frozen map with source provenance.", "instructions": "Advise."},
        model=FunctionModel(respond),
        output_type=StrategicAdvice,
        name="strategic_review_v1",
        tools=[read_tool(name, worker=worker) for name in ("inspect_bundle", "map_region", "player_turn")],
        validate=validate,
    )
    assert len(requested) == 3 and validated[0]["_evidence"]
    assert delivered_reads(result.messages) == [
        {"name": "map_region", "arguments": {"center": {"x": 0, "y": 0}, "radius": 0, "format": "full"}}
    ]
    events = await steps.list_events(run_id=result.run_id)
    assert not any(event.tool_name == "player_turn" for event in events)
    completed = [event for event in events if "nested_result" in event.metadata]
    assert [(event.tool_name, event.tool_call_id) for event in completed] == [("map_region", "corrected__1")]


@pytest.mark.parametrize("failure", ["store", "epoch", "evidence", "receipt", "response", "internal_selector"])
@run_async
async def test_fatal_frozen_read_failures_cannot_be_caught_to_resume_dispatch(
    review_setup, tmp_path, monkeypatch, failure
):
    from civ_agent import observations

    worker, original = review_setup
    request = frozen_map_request(worker, original)
    requested, dispatched = [], []
    original_read = worker.read

    def read(*args):
        dispatched.append(args[1])
        return original_read(*args)

    monkeypatch.setattr(worker, "read", read)
    if failure == "response":
        with pytest.raises(ValidationError) as invalid:
            DetailResult.model_validate({"available": "invalid trusted response"})
        fatal = invalid.value
    elif failure == "internal_selector":
        fatal = ValueError("INVALID_ARGUMENT")
    else:
        fatal = JournalConflict("FROZEN_" + failure.upper() + "_CONFLICT")

    def broken(*_args, **_kwargs):
        raise fatal

    if failure == "epoch":
        worker.store.bind_epoch(request.scope.game, "e2", previous=request.scope.epoch)
    elif failure == "store":
        monkeypatch.setattr(worker, "inputs", broken)
    elif failure == "evidence":
        monkeypatch.setattr(review.StableEvidence, "deliver", broken)
    else:
        monkeypatch.setattr(observations, "project_detail", broken)

    def respond(_messages, info):
        requested.append(True)
        return call(
            info,
            "run_code",
            {
                "code": (
                    "try:\n"
                    "    map_region(center={'x': 0, 'y': 0}, format='full')\n"
                    "except Exception:\n"
                    "    pass\n"
                    "player_turn()"
                )
            },
            "fatal",
        )

    expected = JournalConflict if failure in {"epoch", "response"} else type(fatal)
    with pytest.raises(expected) as raised:
        await LocalAgentRunner(
            LocalAgentServices(invoke=unused, step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"))
        ).run_report(
            request,
            startup={"user": "Review the frozen map.", "instructions": "Advise."},
            model=FunctionModel(respond),
            output_type=StrategicAdvice,
            name="strategic_review_v1",
            tools=[read_tool(name, worker=worker) for name in ("map_region", "player_turn")],
        )
    if failure == "epoch":
        assert "EPOCH_CONFLICT" in str(raised.value)
    elif failure == "response":
        assert str(raised.value) == "REVIEW_INTERNAL_VALIDATION_FAILED" and raised.value.__cause__ is fatal
    else:
        assert raised.value is fatal
    assert requested == [True] and dispatched == ["map_region"]
