"""Current native SDK feedback stays scoped, distinct and observational."""

import asyncio
import json

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryFeedbackPart,
    RetryInfo,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai_harness.step_persistence import ToolEffectRecord

from civ_agent.tool_failures import failure_detail, failure_report
from tests.native_watch_fixtures import fixture, history, memory, persistent_files, task
from tests.test_native_tool_failures import recorded


def save_feedback(root, response, *, arguments=None, run_id="sdk", tool="read_file"):
    request = task(root)
    messages = [
        ModelResponse(
            [
                ToolCallPart(
                    tool, arguments if arguments is not None else {"filename": "missing.md"}, tool_call_id="call"
                )
            ],
            run_id=run_id,
        ),
        ModelRequest([response], run_id=run_id),
    ]
    asyncio.run(history(root, request, run_id=run_id, messages=messages))
    return request


@pytest.mark.parametrize(
    ("cause", "code", "category"),
    [
        ("validation_error", "SDK_TOOL_VALIDATION_RETRY", "arguments"),
        ("model_retry", "SDK_TOOL_MODEL_RETRY", "tool"),
        ("unknown_tool", "SDK_UNKNOWN_TOOL", "tool"),
        ("unavailable_tool", "SDK_UNAVAILABLE_TOOL", "tool"),
    ],
)
def test_native_retry_cause_is_not_inferred_from_feedback_text(tmp_path, cause, code, category):
    fixture(tmp_path)
    save_feedback(
        tmp_path,
        ToolReturnPart(
            "read_file",
            "PRIVATE correction from native tool",
            tool_call_id="call",
            outcome="retried",
            retry=RetryInfo(cause=cause, attempt=1, max_retries=1),
        ),
        arguments="{invalid PRIVATE JSON",
    )
    before = persistent_files(tmp_path)
    report = failure_report([tmp_path])
    assert not report["unavailable"] and len(report["failures"]) == 1
    failure = report["failures"][0]
    assert (failure["code"], failure["category"]) == (code, category)
    assert failure["evidence"]["operationId"] is None
    assert "PRIVATE" not in json.dumps(report)
    detail = failure_detail(report, failure["id"])
    assert detail["arguments"] == "{invalid PRIVATE JSON"
    assert detail["result"]["retry"] == {"cause": cause, "attempt": 1, "max_retries": 1}
    assert before == persistent_files(tmp_path)


@pytest.mark.parametrize(
    ("outcome", "code", "pending"),
    [("failed", "SDK_TOOL_FAILED", 0), ("denied", "SDK_TOOL_DENIED", 0), ("interrupted", None, 1)],
)
def test_native_outcomes_do_not_turn_interruption_into_failure(tmp_path, outcome, code, pending):
    fixture(tmp_path)
    save_feedback(
        tmp_path,
        ToolReturnPart("read_file", "PRIVATE result", tool_call_id="call", outcome=outcome),
    )
    report = failure_report([tmp_path])
    assert report["games"][0]["pendingCalls"] == pending
    assert [row["code"] for row in report["failures"]] == ([code] if code else [])
    assert "PRIVATE" not in json.dumps(report)


def test_native_output_feedback_is_separate_from_tool_operations_and_billing(tmp_path):
    fixture(tmp_path)
    request = task(tmp_path)
    asyncio.run(
        history(
            tmp_path,
            request,
            run_id="output",
            messages=[
                ModelRequest(
                    [
                        RetryFeedbackPart(
                            "PRIVATE output validation",
                            retry=RetryInfo(cause="validation_error", attempt=1, max_retries=2),
                        )
                    ],
                    run_id="output",
                )
            ],
        )
    )
    report = failure_report([tmp_path])
    counts = report["games"][0]
    assert counts["modelFeedback"] == 1 and counts["completedCalls"] == counts["pendingCalls"] == 0
    failure = report["failures"][0]
    assert failure["code"] == "SDK_OUTPUT_RETRY" and failure["tool"] == "model_output"
    assert failure["evidence"]["operationId"] is None
    assert report["nativeUsage"][0]["modelRequests"] == 64
    assert "PRIVATE" not in json.dumps(report)
    assert "PRIVATE output validation" in failure_detail(report, failure["id"])["result"]["feedback"]


def test_same_model_call_id_in_distinct_sdk_segments_is_not_deduplicated(tmp_path):
    fixture(tmp_path)
    for run in ("segment-1", "segment-2"):
        save_feedback(
            tmp_path,
            ToolReturnPart("read_file", {"error": "OBSERVATION_REQUIRED"}, tool_call_id="call"),
            run_id=run,
        )
    report = failure_report([tmp_path])
    assert len(report["failures"]) == report["games"][0]["completedCalls"] == 2
    assert len({row["id"] for row in report["failures"]}) == 2
    assert {row["evidence"]["runId"] for row in report["failures"]} == {"segment-1", "segment-2"}


def test_native_response_tool_binding_is_verified_before_reporting(tmp_path):
    fixture(tmp_path)
    save_feedback(
        tmp_path,
        ToolReturnPart("foreign_tool", {"error": "OBSERVATION_REQUIRED"}, tool_call_id="call"),
    )
    report = failure_report([tmp_path])
    assert report["failures"] == report["games"] == []
    assert report["unavailable"][0]["exceptionType"] == "ValueError"
    assert "foreign_tool" not in json.dumps(report)


def test_changed_receipt_for_same_original_native_operation_fails_closed(tmp_path):
    request = recorded(tmp_path)

    async def write():
        store = await history(
            tmp_path,
            request,
            run_id="changed",
            messages=[
                ModelResponse(
                    [ToolCallPart("player_command", {"kind": "WAIT"}, tool_call_id="changed")], run_id="changed"
                ),
                ModelRequest(
                    [ToolReturnPart("player_command", {"applied": True}, tool_call_id="changed")], run_id="changed"
                ),
            ],
        )
        await store.record_tool_effect(
            ToolEffectRecord(
                run_id="changed",
                tool_call_id="changed",
                tool_name="player_command",
                status="completed",
                idempotency_key="native:original-command:0",
            )
        )

    asyncio.run(write())
    report = failure_report([tmp_path])
    assert report["failures"] == [] and report["unavailable"]


def test_gateway_domain_feedback_preserves_accepted_action_and_rejected_changes(tmp_path):
    fixture(tmp_path)
    request = task(tmp_path)
    state = memory(tmp_path)
    state.start_job(request.job, "gameplay", {}, 2)
    result = {
        "applied": True,
        "changes": [{"accepted": False, "error": {"code": "MEMORY_CONFLICT"}, "content": "PRIVATE future plan"}],
        "stateUpdate": {"kind": "unavailable", "reason": "PRIVATE refresh failure"},
    }
    state.call(request.job, "original-action", "player_command", {"command": {"kind": "MOVE_UNIT"}}, result)
    state.engine.dispose()
    before = persistent_files(tmp_path)
    report = failure_report([tmp_path])
    assert report["games"][0]["completedCalls"] == 1
    assert report["games"][0]["rejectedEdits"] == report["games"][0]["refreshFailures"] == 1
    assert {row["kind"] for row in report["failures"]} == {"rejected_edit", "refresh"}
    assert all(row["executionStatus"] == "returned" for row in report["failures"])
    assert "PRIVATE" not in json.dumps(report)
    detail = failure_detail(report, report["failures"][0]["id"])
    assert detail["result"]["applied"] is True
    assert before == persistent_files(tmp_path)


def test_committed_gateway_receipt_remains_visible_before_sdk_tool_completion(tmp_path):
    recorded(tmp_path, settled_pending=True)
    report = failure_report([tmp_path])
    assert report["games"][0]["pendingCalls"] == 0 and report["games"][0]["completedCalls"] == 4
    assert len([row for row in report["failures"] if row["code"] == "CONVERSATION_UPDATED"]) == 1
