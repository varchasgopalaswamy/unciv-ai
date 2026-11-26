"""Operator diagnostics: bounded files, background writes, and input-free failures."""

import json
import logging
import os
import queue
import re
import stat
import sys
import traceback
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path

from pydantic import ValidationError

from civ_agent.operator_connection import OperatorError
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
        if isinstance(current, OperatorError):
            if isinstance(current.code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", current.code):
                detail["operatorCode"] = current.code
            if type(current.status) is int and 100 <= current.status <= 599:
                detail["httpStatus"] = current.status
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
