"""Connection observability without paid requests or real-time timeout waits."""

import json

import pytest

from civ_agent.model_status import MAX_LOG_LINE, LogLines, ModelStatus, error_fields, retry_log
from civ_agent.protocol import JsonObject

RETRY_LINE = (
    "2026-09-22T14:26:36Z WARN codex_core::responses_retry: "
    "stream disconnected - retrying sampling request (1/5 in 184ms)... "
    "turn_id=turn-1 retries=1 max_retries=5 "
    "sampling_error=stream disconnected before completion: idle timeout waiting for websocket"
)


def notification(method, **params):
    return method, {"threadId": "thread-1", "turnId": "turn-1", **params}


def required(value: JsonObject | None) -> JsonObject:
    assert value is not None
    return value


def test_rome_timeout_timeline_ignores_usage_updates_and_recovers_once():
    now = [0.0]
    status = ModelStatus(lambda: now[0])
    status.begin("thread-1", "turn-1")
    now[0] = 59
    assert status.poll() is None
    now[0] = 60
    assert required(status.poll())["state"] == "waiting"
    assert status.poll() is None
    now[0] = 120
    assert required(status.poll())["elapsedSeconds"] == 120
    now[0] = 301
    assert status.observe(*notification("thread/tokenUsage/updated", tokenUsage={"total": {}})) is None
    fields = retry_log(RETRY_LINE)
    assert fields is not None
    retry = status.retry(fields, "stderr")
    assert retry is not None
    assert retry["state"] == "retrying" and retry["elapsedSeconds"] == 301
    assert retry["attempt"] == 1 and retry["maxAttempts"] == 5
    # A duplicate structured event must not report a second retry or recovery.
    assert status.observe(*notification("error", willRetry=True, error={"message": "idle timeout"})) is None
    now[0] = 315
    recovered = status.observe(*notification("item/agentMessage/delta", delta="New move"))
    assert recovered is not None
    assert recovered["state"] == "responding" and recovered["elapsedSeconds"] == 315
    assert status.observe(*notification("item/agentMessage/delta", delta="continued")) is None
    assert status.poll() is None


def test_terminal_completion_retains_quota_category_when_error_was_reported_separately():
    status = ModelStatus()
    status.begin("thread-1", "turn-1")
    status.observe(*notification("error", willRetry=False, error={"codexErrorInfo": "usageLimitExceeded"}))
    terminal = required(status.observe(*notification("turn/completed", turn={"id": "turn-1", "status": "failed"})))
    assert terminal["state"] == "failed" and terminal["reason"] == "usageLimitExceeded"
    assert "usage limit" in terminal["message"]


def test_pending_parallel_tools_are_not_reported_as_model_stalls():
    now = [0.0]
    status = ModelStatus(lambda: now[0])
    status.begin("thread-1", "turn-1")
    for id in ("trade", "map"):
        status.observe(*notification("item/started", item={"type": "mcpToolCall", "id": id}))
    now[0] = 500
    assert status.poll() is None
    status.observe(*notification("item/completed", item={"type": "mcpToolCall", "id": "map"}))
    now[0] = 800
    assert status.poll() is None
    status.observe(*notification("item/completed", item={"type": "mcpToolCall", "id": "trade"}))
    now[0] = 859
    assert status.poll() is None
    now[0] = 860
    assert required(status.poll())["elapsedSeconds"] == 60
    status.observe(*notification("item/reasoning/textDelta", delta="RAW_SECRET"))
    assert "RAW_SECRET" not in json.dumps(status.issue)
    assert status.poll() is None


@pytest.mark.parametrize("terminal", ["completed", "failed", "interrupted"])
def test_notices_cannot_leak_across_threads_turns_or_shutdown(terminal):
    now = [0.0]
    status = ModelStatus(lambda: now[0])
    assert status.retry(required(retry_log(RETRY_LINE)), "stderr") is None
    status.begin("thread-1", "turn-1")
    now[0] = 90
    assert status.observe(*notification("item/started", threadId="foreign", item={})) is None
    assert status.observe(*notification("item/started", turnId="old-turn", item={})) is None
    assert status.retry(required(retry_log(RETRY_LINE.replace("turn-1", "old-turn"))), "stderr") is None
    assert required(status.poll())["elapsedSeconds"] == 90
    result = status.observe(*notification("turn/completed", turn={"id": "turn-1", "status": terminal}))
    assert result is not None
    assert result["state"] == terminal
    now[0] = 1000
    assert status.poll() is None
    assert status.retry(required(retry_log(RETRY_LINE)), "stderr") is None
    status.begin("thread-1", "turn-2")
    assert status.poll() is None


@pytest.mark.parametrize(
    ("info", "message", "reason"),
    [
        ({"httpConnectionFailed": {"httpStatusCode": 429}}, "", "rateLimitExceeded"),
        ("unauthorized", "secret error body", "unauthorized"),
        (
            {"responseStreamDisconnected": {"httpStatusCode": None}},
            "idle timeout waiting for websocket",
            "idle_timeout",
        ),
        ("contextWindowExceeded", "secret prompt", "contextWindowExceeded"),
        ("usageLimitExceeded", "rate limit exceeded: SECRET", "usageLimitExceeded"),
        ({"usageLimitExceeded": {"httpStatusCode": 429}}, "SECRET", "usageLimitExceeded"),
        ({"httpConnectionFailed": {"httpStatusCode": 429}}, "insufficient_quota SECRET", "usageLimitExceeded"),
        ("other", "You've hit your usage limit. SECRET", "usageLimitExceeded"),
        ("unknown_private_value", "arbitrary secret", "other"),
        ("invalid_prompt", "Invalid prompt", "invalidPrompt"),
        ({"invalid_prompt": {"httpStatusCode": 400}}, "Invalid prompt", "invalidPrompt"),
        (
            "other",
            "Invalid prompt: your prompt was flagged as potentially violating our usage policy. SECRET",
            "promptPolicyRejected",
        ),
    ],
)
def test_errors_keep_only_known_categories_and_numeric_status(info, message, reason):
    fields = error_fields({"codexErrorInfo": info, "message": message, "additionalDetails": "SECRET_HEADERS"})
    assert fields["reason"] == reason
    if info == {"invalid_prompt": {"httpStatusCode": 400}}:
        assert fields["httpStatus"] == 400
    assert "secret" not in json.dumps(fields).lower()
    status = ModelStatus()
    status.begin("thread-1", "turn-1")
    result = status.observe(*notification("error", willRetry=False, error={"codexErrorInfo": info, "message": message}))
    assert result is not None
    assert result["state"] == "failed" and result["reason"] == reason
    assert status.poll() is None


def test_stderr_is_bounded_sanitized_and_drained_after_oversized_lines():
    lines = LogLines()
    assert not lines.feed(b"irrelevant secret header\n")
    assert not lines.feed(b"x" * (MAX_LOG_LINE + 1))
    assert not lines.buffer
    assert not lines.feed(b"ignored end\n")
    raw = ("\x1b[33m" + RETRY_LINE + "\x1b[0m cookie=PRIVATE_COOKIE\n").encode()
    assert not lines.feed(raw[:65])
    entries = lines.feed(raw[65:])
    assert len(entries) == 1
    assert entries[0]["reason"] == "idle_timeout" and entries[0]["retryDelayMs"] == 184
    assert "PRIVATE" not in json.dumps(entries)
    assert retry_log(RETRY_LINE.replace("codex_core::responses_retry:", "other::target:")) is None
    assert retry_log(RETRY_LINE.replace("turn_id=", "other=")) is None
    assert retry_log(RETRY_LINE.replace("1/5", "6/5")) is None


def test_repeated_retries_and_nonfatal_warnings_remain_visible():
    now = [0.0]
    status = ModelStatus(lambda: now[0])
    status.begin("thread-1", "turn-1")
    assert required(status.retry(required(retry_log(RETRY_LINE)), "stderr"))["attempt"] == 1
    now[0] = 1
    assert required(status.retry(required(retry_log(RETRY_LINE.replace("1/5", "2/5"))), "stderr"))["attempt"] == 2
    now[0] = 61
    assert required(status.poll())["state"] == "retrying"
    warning = status.observe("warning", {"message": "rate limit exceeded", "threadId": "thread-1"})
    assert warning is not None
    assert warning["state"] == "warning" and warning["reason"] == "rateLimitExceeded"


@pytest.mark.parametrize(
    "message,reason",
    [
        ("Code Mode is unavailable because code-mode host is disabled. PRIVATE", "codeModeDisabled"),
        ("Model metadata for `PRIVATE` not found. Defaulting to fallback metadata", "modelMetadataMissing"),
        ("PRIVATE unknown runtime notice", "runtimeWarning"),
    ],
)
def test_runtime_warnings_do_not_invent_connection_recovery_or_erase_retries(message, reason):
    status = ModelStatus()
    status.begin("thread-1", "turn-1")
    notice = required(status.observe("warning", {"threadId": "thread-1", "message": message}))
    assert notice["state"] == "warning" and notice["reason"] == reason
    assert "PRIVATE" not in json.dumps(notice)
    assert status.issue is None
    assert status.observe(*notification("item/agentMessage/delta", delta="progress")) is None
    retry = status.retry(required(retry_log(RETRY_LINE)), "stderr")
    status.observe("warning", {"threadId": "thread-1", "message": message})
    assert status.issue == retry
    recovered = required(status.observe(*notification("item/agentMessage/delta", delta="progress")))
    assert recovered["state"] == "responding"
