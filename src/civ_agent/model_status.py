"""Input-free model connection notices, scoped to one requested Codex turn.

Some Codex retries are only logged on stderr. Parse that narrow event, never
forward raw logs: provider errors can contain headers, prompts, and credentials.
"""

import re
import time
from collections.abc import Callable

from civ_agent.protocol import JsonObject
from civ_agent.terminal_output import visible_text

QUIET_SECONDS = 60
RECOVERABLE_MODEL_FAILURES = frozenset({"serverOverloaded", "internalServerError", "rateLimitExceeded"})
MAX_LOG_LINE = 16 * 1024
RETRY = re.compile(r"retrying sampling request \((\d{1,4})/(\d{1,4}) in (\d{1,9}(?:\.\d{1,6})?)(ms|s)\)")
TURN = re.compile(r"\bturn_id=([A-Za-z0-9-]{1,128})\b")
REASONS = {
    "idle_timeout": "Model connection idle timeout",
    "timeout": "Model request timed out",
    "responseStreamDisconnected": "Model connection disconnected",
    "responseStreamConnectionFailed": "Model connection failed",
    "httpConnectionFailed": "Model HTTP request failed",
    "rateLimitExceeded": "Model service rate limit",
    "usageLimitExceeded": "Model usage limit reached",
    "serverOverloaded": "Model service overloaded",
    "internalServerError": "Model service error",
    "unauthorized": "Model authentication failed",
    "contextWindowExceeded": "Model context limit exceeded",
    "sessionBudgetExceeded": "Model session budget exceeded",
    "responseTooManyFailedAttempts": "Model retry limit reached",
    "badRequest": "Model request rejected",
    "invalidPrompt": "Model prompt rejected by provider",
    "promptPolicyRejected": "Model prompt rejected by provider policy filter",
    "codeModeDisabled": "Codex Code Mode is disabled",
    "modelMetadataMissing": "Codex model metadata is unavailable; using fallback settings",
    "runtimeWarning": "Codex runtime warning",
    "other": "Model service reported an issue",
}
TOOL_ITEMS = {"mcpToolCall", "dynamicToolCall", "commandExecution", "fileChange"}
PROGRESS_METHODS = {
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    # Count activity without inspecting or retaining raw reasoning.
    "item/reasoning/textDelta",
}


def error_fields(error: JsonObject) -> JsonObject:
    """Select a known category and numeric status; never retain free-form text."""
    info = error.get("codexErrorInfo")
    code = info if isinstance(info, str) else next(iter(info), "other") if isinstance(info, dict) else "other"
    reason = {"invalid_prompt": "invalidPrompt"}.get(code, code)
    fields: JsonObject = {"reason": reason if reason in REASONS else "other"}
    if isinstance(info, dict) and isinstance(info.get(code), dict):
        status = info[code].get("httpStatusCode")
        if type(status) is int and 100 <= status <= 599:
            fields["httpStatus"] = status
    message = str(error.get("message", ""))[:MAX_LOG_LINE].lower()
    if "invalid prompt: your prompt was flagged as potentially violating our usage policy" in message:
        fields["reason"] = "promptPolicyRejected"
    elif fields["reason"] == "usageLimitExceeded" or any(
        phrase in message
        for phrase in (
            "insufficient_quota",
            "quota exhausted",
            "quota exceeded",
            "exceeded your current quota",
            "usage limit",
            "insufficient credits",
            "credit balance is too low",
        )
    ):
        fields["reason"] = "usageLimitExceeded"
    elif "idle timeout" in message:
        fields["reason"] = "idle_timeout"
    elif "timed out" in message or "timeout" in message:
        fields["reason"] = "timeout"
    elif fields.get("httpStatus") == 429 or "rate limit" in message:
        fields["reason"] = "rateLimitExceeded"
    elif fields["reason"] == "other" and "stream disconnected" in message:
        fields["reason"] = "responseStreamDisconnected"
    return fields


def retry_log(line: str) -> JsonObject | None:
    line = visible_text(line)
    if "codex_core::responses_retry:" not in line:
        return None
    retry, turn = RETRY.search(line), TURN.search(line)
    if retry is None or turn is None:
        return None
    attempt, maximum = int(retry[1]), int(retry[2])
    if not 1 <= attempt <= maximum:
        return None
    return {
        **error_fields({"message": line}),
        "turnId": turn[1],
        "attempt": attempt,
        "maxAttempts": maximum,
        "retryDelayMs": round(float(retry[3]) * (1000 if retry[4] == "s" else 1)),
    }


def status_text(data: JsonObject) -> str:
    message = str(data.get("message", "Model status updated."))
    if data.get("state") in {"waiting", "retrying"}:
        message += f" No model progress for {data.get('elapsedSeconds', 0)}s."
    return message


def quota_failure(record: JsonObject, notice: JsonObject) -> JsonObject | None:
    """Retain only the job identity needed to recover a terminal quota failure."""
    if notice.get("reason") != "usageLimitExceeded" or not (
        notice.get("state") == "failed" or notice.get("rpcMethod") in {"turn/start", "thread/start", "thread/resume"}
    ):
        return None
    return {
        "reason": "usageLimitExceeded",
        "epoch": record["epoch"],
        "activationId": record["activationId"],
        "purpose": record.get("purpose", "gameplay"),
    }


class LogLines:
    """Drain even oversized/unrelated log lines without retaining their contents."""

    def __init__(self):
        self.buffer = b""
        self.discarding = False

    def feed(self, chunk: bytes) -> list[JsonObject]:
        result = []
        for part in chunk.splitlines(keepends=True):
            complete = part.endswith(b"\n")
            if not self.discarding:
                if len(self.buffer) + len(part) > MAX_LOG_LINE:
                    self.discarding = True
                    self.buffer = b""
                else:
                    self.buffer += part
            if complete:
                if not self.discarding:
                    notice = retry_log(self.buffer.decode("utf-8", errors="replace"))
                    if notice is not None:
                        result.append(notice)
                self.buffer = b""
                self.discarding = False
        return result


class ModelStatus:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.thread: str | None = None
        self.turn: str | None = None
        self.last_progress = self.clock()
        self.last_notice = self.last_progress
        self.tools: set[str] = set()
        self.issue: JsonObject | None = None
        self.last_retry: tuple[float, str, int | None] | None = None

    def begin(self, thread: str, turn: str):
        self.thread, self.turn = thread, turn
        self.last_progress = self.last_notice = self.clock()
        self.tools.clear()
        self.issue = None
        self.last_retry = None

    def event(self, state: str, message: str, **fields) -> JsonObject:
        return {
            "threadId": self.thread,
            "turnId": self.turn,
            "state": state,
            "message": message,
            "elapsedSeconds": max(0, int(self.clock() - self.last_progress)),
            **fields,
        }

    def retry(self, fields: JsonObject, source: str) -> JsonObject | None:
        if self.turn is None or fields.get("turnId", self.turn) != self.turn:
            return None
        now = self.clock()
        attempt = fields.get("attempt")
        # The same retry can arrive both as a protocol error and on stderr.
        if self.last_retry is not None:
            when, prior_source, prior_attempt = self.last_retry
            if now - when < 2 and source != prior_source and (attempt is None or prior_attempt in {None, attempt}):
                return None
        self.last_retry = now, source, attempt
        detail = {key: value for key, value in fields.items() if key != "turnId"}
        message = REASONS[detail["reason"]] + "; retrying"
        if attempt is not None:
            message += f" ({attempt}/{detail['maxAttempts']}, next attempt in {detail['retryDelayMs'] / 1000:g}s)"
        self.issue = self.event("retrying", message + ".", source=source, **detail)
        self.last_notice = now
        return self.issue

    def observe(self, method: str, params: JsonObject) -> JsonObject | None:
        if method == "warning" and self.turn is not None and params.get("threadId") in {None, self.thread}:
            fields = error_fields(params)
            if fields["reason"] == "other":
                message = str(params.get("message", ""))[:MAX_LOG_LINE]
                fields["reason"] = (
                    "codeModeDisabled"
                    if message.startswith("Code Mode is unavailable because code-mode host is disabled.")
                    else "modelMetadataMissing"
                    if message.startswith("Model metadata for `")
                    and "not found. Defaulting to fallback metadata" in message
                    else "runtimeWarning"
                )
                # Configuration/runtime warnings are not connection failures.
                # Keep them visible without manufacturing a later recovery or
                # overwriting a real pending retry/timeout.
                return self.event("warning", REASONS[fields["reason"]] + ".", source="protocol", **fields)
            self.issue = self.event("warning", REASONS[fields["reason"]] + ".", source="protocol", **fields)
            return self.issue
        if self.turn is None or params.get("threadId") != self.thread:
            return None
        turn = params.get("turn", {}) if method == "turn/completed" else {}
        if (turn.get("id") if turn else params.get("turnId")) != self.turn:
            return None
        if method == "error":
            fields = error_fields(params.get("error", {}))
            if params.get("willRetry") is True:
                return self.retry(fields, "protocol")
            self.issue = self.event("failed", REASONS[fields["reason"]] + ".", source="protocol", **fields)
            return self.issue
        if method == "turn/completed":
            result = None
            error = turn.get("error")
            if isinstance(error, dict):
                fields = error_fields(error)
                result = self.event("failed", REASONS[fields["reason"]] + ".", source="protocol", **fields)
            elif self.issue is not None:
                status = turn.get("status") if turn.get("status") in {"completed", "failed"} else "interrupted"
                result = (
                    {**self.issue, "state": status}
                    if status == "failed"
                    else self.event(status, f"Model turn {status}.", source="protocol")
                )
            self.turn = None
            self.issue = None
            return result
        item = params.get("item", {})
        tool = item.get("type") in TOOL_ITEMS and isinstance(item.get("id"), str)
        progress = method in PROGRESS_METHODS or method in {"item/started", "item/completed"}
        if not progress:
            return None
        result = None
        # Tool completion starts a new wait for the model; it isn't model output.
        if self.issue is not None and not (tool and method == "item/completed"):
            elapsed = max(0, int(self.clock() - self.last_progress))
            result = self.event(
                "responding", f"Model response resumed after {elapsed}s without progress.", source="activity"
            )
            self.issue = None
        if tool:
            if method == "item/started":
                self.tools.add(item["id"])
            else:
                self.tools.discard(item["id"])
        self.last_progress = self.clock()
        return result

    def poll(self) -> JsonObject | None:
        now = self.clock()
        if (
            self.turn is None
            or self.tools
            or (self.issue or {}).get("state") == "failed"
            or now - self.last_progress < QUIET_SECONDS
            or now - self.last_notice < QUIET_SECONDS
        ):
            return None
        elapsed = max(0, int(now - self.last_progress))
        if self.issue is not None and self.issue["state"] == "retrying":
            self.issue = {**self.issue, "elapsedSeconds": elapsed}
        else:
            self.issue = self.event("waiting", "Waiting for a model response; no progress received.", source="activity")
        self.last_notice = now
        return self.issue
