"""Native SDK and gateway receipt triage retains exact scope and uncertainty."""

import asyncio
import copy
import json
from datetime import UTC, datetime

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, RetryInfo, ToolCallPart, ToolReturnPart
from pydantic_ai_harness.step_persistence import ToolEffectRecord

from civ_agent.commands import failures as command
from civ_agent.tool_failures import failure_detail, failure_report
from tests.native_watch_fixtures import fixture, history, install_test_epoch, memory, persistent_files, task


def recorded(root, *, settled_pending=False):
    fixture(root)
    request = task(root)
    operation = "native:original-command"
    rejected = {"error": "STALE_REVISION", "applied": False, "instruction": "PRIVATE observed facts"}
    arguments = {"commands": [{"kind": "MOVE_UNIT", "unit": "PRIVATE unit"}, {"kind": "FOUND_CITY"}, {"kind": "WAIT"}]}
    messages = [
        ModelResponse(
            [
                ToolCallPart("execute_commands", arguments, tool_call_id="commands"),
                ToolCallPart("read_file", {"filename": "missing.md"}, tool_call_id="retry"),
                ToolCallPart("chat_read", {}, tool_call_id="pending"),
            ],
            run_id="sdk",
        ),
        ModelRequest(
            [
                ToolReturnPart(
                    "execute_commands",
                    {
                        "receipts": [
                            {
                                "operationId": operation + ":0",
                                "tool": "player_command",
                                "status": "rejected",
                                "result": rejected,
                            },
                            {
                                "operationId": operation + ":1",
                                "tool": "player_command",
                                "status": "confirmed",
                                "result": {"applied": True},
                            },
                            {"operationId": operation + ":2", "tool": "player_command", "status": "unexecuted"},
                        ]
                    },
                    tool_call_id="commands",
                ),
                ToolReturnPart(
                    "read_file",
                    "PRIVATE missing-file correction",
                    tool_call_id="retry",
                    outcome="retried",
                    retry=RetryInfo(cause="validation_error", attempt=1, max_retries=1),
                ),
            ],
            run_id="sdk",
        ),
    ]

    async def write():
        store = await history(root, request, run_id="sdk", messages=messages)
        for identifier, name, status, key in (
            ("commands", "execute_commands", "completed", operation),
            ("retry", "read_file", "completed", "native:read-file"),
            ("pending", "chat_read", "started", "native:pending"),
        ):
            await store.record_tool_effect(
                ToolEffectRecord(
                    run_id="sdk",
                    tool_call_id=identifier,
                    tool_name=name,
                    status=status,
                    idempotency_key=key,
                    started_at=datetime(2026, 11, 5, 12, tzinfo=UTC),
                )
            )

    asyncio.run(write())
    state = memory(root)
    state.start_job(request.job, "gameplay", {}, 2)
    state.call(request.job, operation + ":0", "player_command", {"command": arguments["commands"][0]}, rejected)
    state.call(request.job, "startup:private-read", "player_turn", {}, {"error": "OBSERVATION_REQUIRED"})
    if settled_pending:
        state.call(request.job, "native:pending", "chat_read", {}, {"error": "CONVERSATION_UPDATED"})
    state.engine.dispose()
    return request


def test_sdk_gateway_receipts_deduplicate_and_pending_effect_is_not_failed(tmp_path):
    recorded(tmp_path)
    before = persistent_files(tmp_path)
    report = failure_report([tmp_path])
    assert not report["unavailable"]
    assert report["games"][0]["completedCalls"] == 3 and report["games"][0]["pendingCalls"] == 1
    assert report["games"][0]["unexecutedCalls"] == 1
    assert [row["code"] for row in report["failures"]] == ["STALE_REVISION", "SDK_TOOL_VALIDATION_RETRY"]
    assert report["nativeUsage"][0]["usage"]["reasoning_output_tokens"] == 128
    assert "PRIVATE" not in json.dumps(report)
    assert before == persistent_files(tmp_path)
    assert len(failure_report([tmp_path], include_internal=True)["failures"]) == 3


def test_detail_revalidates_source_and_returns_only_exact_current_sdk_receipt(tmp_path):
    recorded(tmp_path)
    report = failure_report([tmp_path], tool="player_command", code="STALE_REVISION", since_turn=2)
    failure = report["failures"][0]
    detail = failure_detail(report, failure["id"])
    assert detail["result"]["instruction"] == "PRIVATE observed facts"
    assert detail["arguments"]["kind"] == "MOVE_UNIT" and detail["toolEffect"]["status"] == "completed"
    changed = copy.deepcopy(report)
    changed["failures"][0]["civilization"] = "Greece"
    with pytest.raises(ValueError, match="NATIVE_FAILURE_DETAIL_SCOPE"):
        failure_detail(changed, failure["id"])
    install_test_epoch(tmp_path, "target")
    with pytest.raises(ValueError):
        failure_detail(report, failure["id"])
    assert failure_report([tmp_path])["failures"] == []


def test_feedback_detail_is_actual_native_model_correction_not_inferred_exception(tmp_path):
    recorded(tmp_path)
    report = failure_report([tmp_path], code="SDK_TOOL_VALIDATION_RETRY")
    detail = failure_detail(report, report["failures"][0]["id"])
    assert "PRIVATE missing-file correction" in detail["result"]["feedback"]
    assert failure_report([tmp_path], role="military")["failures"] == []


def test_native_failure_cli_summary_and_explicit_detail(tmp_path, capsys):
    recorded(tmp_path)
    args = command.parser().parse_args(["--state-dir", str(tmp_path), "--json"])
    assert command.run(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert "PRIVATE" not in json.dumps(report)
    args.id = report["failures"][0]["id"]
    assert command.run(args) == 0
    assert json.loads(capsys.readouterr().out)["result"]["error"] == "STALE_REVISION"
