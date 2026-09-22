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
        ("unknown_private_value", "arbitrary secret", "other"),
    ],
)
def test_errors_keep_only_known_categories_and_numeric_status(info, message, reason):
    fields = error_fields({"codexErrorInfo": info, "message": message, "additionalDetails": "SECRET_HEADERS"})
    assert fields["reason"] == reason
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
