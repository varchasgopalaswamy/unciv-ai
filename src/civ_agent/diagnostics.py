"""Operator diagnostics: bounded files, background writes, and input-free failures."""

import hashlib
import json
import logging
import os
import queue
import stat
import sys
import traceback
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path

from pydantic import ValidationError

from civ_agent.model_status import MAX_LOG_LINE, error_fields
from civ_agent.protocol import JsonObject
from civ_agent.tls import private_directory


def exception_details(error: BaseException) -> JsonObject:
    """Keep causes and code locations without messages, source lines or locals.

    SDK exception messages can contain entire wire responses, validation inputs,
    headers and credentials. Extract useful typed fields instead of redacting an
    arbitrary string after it has already reached a logging handler.
    """
    seen: set[int] = set()

    def describe(current: BaseException, depth: int = 0) -> JsonObject:
        if id(current) in seen or depth >= 6 or len(seen) >= 24:
            return {"truncated": True}
        seen.add(id(current))
        detail: JsonObject = {
            "type": type(current).__module__ + "." + type(current).__qualname__,
            "stack": [
                {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(current.__traceback__)[-20:]
            ],
        }
        if isinstance(current, OSError) and current.errno is not None:
            detail["errno"] = current.errno
        if isinstance(current, json.JSONDecodeError):
            detail["jsonPosition"] = {"line": current.lineno, "column": current.colno, "offset": current.pos}
        if isinstance(current, ValidationError):
            # Locations may contain arbitrary mapping keys. Field *types* and
            # their depth suffice to identify the failed validation boundary.
            detail["validation"] = [
                {"type": item["type"], "depth": len(item["loc"])}
                for item in current.errors(include_input=False, include_context=False, include_url=False)[:20]
            ]
        for attribute, name in (("error", "rpcCode"), ("response", "httpStatus")):
            source = getattr(current, attribute, None)
            value = getattr(source, "code" if attribute == "error" else "status_code", None)
            if type(value) is int:
                detail[name] = value
        verification = getattr(current, "verify_code", None)
        if type(verification) is int:
            detail["tlsVerifyCode"] = verification
        if isinstance(current, BaseExceptionGroup):
            detail["exceptions"] = [describe(child, depth + 1) for child in current.exceptions[:12]]
        cause = current.__cause__ or current.__context__
        if cause is not None:
            detail["cause"] = describe(cause, depth + 1)
        return detail

    return describe(error)


def codex_request_error(method: str, request_id: int, error: object) -> JsonObject:
    """Retain control-error metadata without copying provider text or payloads."""
    fields: JsonObject = {"rpcMethod": method, "rpcId": request_id}
    if not isinstance(error, dict):
        fields["reason"] = "malformedRpcError"
        return fields
    code = error.get("code")
    if type(code) is int:
        fields["rpcCode"] = code
    candidates = [error]
    data = error.get("data")
    if isinstance(data, dict):
        candidates.append(data)
        if isinstance(data.get("error"), dict):
            candidates.append(data["error"])
    fields["reason"] = "other"
    for candidate in candidates:
        detail = error_fields(candidate)
        if detail["reason"] != "other" and fields["reason"] != "usageLimitExceeded":
            fields["reason"] = detail["reason"]
        if "httpStatus" in detail:
            fields["httpStatus"] = detail["httpStatus"]
        for name in ("httpStatusCode", "httpStatus"):
            status = candidate.get(name)
            if type(status) is int and 100 <= status <= 599:
                fields["httpStatus"] = status
    message = error.get("message")
    if isinstance(message, str):
        fields["messageCharacters"] = len(message)
        fields["messageSha256"] = hashlib.sha256(message.encode("utf-8", errors="replace")).hexdigest()
        text = message[:MAX_LOG_LINE].lower()
        if fields["reason"] == "other":
            for reason, phrases in (
                (
                    "turnNotActive",
                    ("no active turn", "turn is not active", "turn not active", "turn already completed"),
                ),
                ("threadNotFound", ("thread not found", "unknown thread")),
                ("unsupportedReasoningEffort", ("unsupported reasoning effort", "reasoning effort is not supported")),
            ):
                if any(phrase in text for phrase in phrases):
                    fields["reason"] = reason
                    break
    if fields["reason"] == "other" and type(code) is int:
        fields["reason"] = {
            -32700: "parseError",
            -32600: "invalidRequest",
            -32601: "methodNotFound",
            -32602: "invalidParams",
            -32603: "internalError",
        }.get(code, "other")
    return fields


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        descriptor = os.open(
            self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & ~0o600:
                raise OSError("Diagnostic log must be a private regular file")
            return os.fdopen(descriptor, "a", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise

    def handleError(self, record):
        # Logging failure must not change whether an action is executed.
        print("Cannot write service diagnostics; check disk space and log permissions.", file=sys.stderr)


class Diagnostics:
    def __init__(self, directory: Path, civilization: str, *, max_bytes: int = 5 * 1024 * 1024):
        self.path = private_directory(directory) / "diagnostics.jsonl"
        self.context = {"civilization": civilization, "serviceInstance": uuid.uuid4().hex, "pid": os.getpid()}
        self.file = _PrivateRotatingFileHandler(self.path, maxBytes=max_bytes, backupCount=3, encoding="utf-8")
        self.file.setFormatter(logging.Formatter("%(message)s"))
        self.queue = queue.SimpleQueue()
        self.handler = QueueHandler(self.queue)
        self.listener = QueueListener(self.queue, self.file)
        self.closed = False
        self.listener.start()
        self.emit(
            "service_started",
            python=sys.version.split()[0],
            packages={name: version(name) for name in ("mcp", "httpx2", "pydantic")},
        )

    def emit(self, event: str, *, error: BaseException | None = None, **fields) -> None:
        """Fields must be explicitly selected operational metadata, never payloads."""
        if self.closed:
            return
        value = {"time": datetime.now(UTC).isoformat(), "event": event, **self.context, **fields}
        if error is not None:
            value["exception"] = exception_details(error)
        self.handler.handle(
            logging.LogRecord("civ_agent.diagnostics", logging.INFO, "", 0, json.dumps(value), (), None)
        )

    def close(self) -> None:
        if self.closed:
            return
        self.emit("service_stopped")
        self.closed = True
        self.listener.stop()
        self.handler.close()
        self.file.close()
