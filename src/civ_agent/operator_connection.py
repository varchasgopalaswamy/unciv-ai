"""Authenticated HTTPS control transport shared by durable and legacy coordinators."""

import errno
import json
import os
import re
import socket
import ssl
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from civ_agent.decisions_policy import MAX_DECISIONS_TIMEOUT_SECONDS


class RunnerError(Exception):
    pass


class OperatorError(RunnerError):
    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code, self.status = code, status


def operator_token(path):
    try:
        with path.open("rb") as source:
            mode = os.fstat(source.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & ~0o600:
                raise RunnerError(
                    "Operator credentials must be a regular file with owner-only permissions (chmod 600)."
                )
            content = source.read(65537)
        value = json.loads(content)
        if len(content) > 65536 or not isinstance(value, dict) or set(value) != {"token"}:
            raise ValueError()
        token = value["token"]
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise ValueError()
        return token
    except FileNotFoundError:
        raise RunnerError(
            "Operator credentials are missing. Restart civ-agent host start with operator support first."
        ) from None
    except (ValueError, UnicodeError, TypeError):
        raise RunnerError("Invalid operator credentials file.") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OperatorError("Authenticated control endpoints must not redirect.")


def secure_url(value):
    if not isinstance(value, str):
        raise RunnerError("Control endpoints must be HTTPS URL strings.")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RunnerError("Control endpoints must be HTTPS URLs without credentials, queries or fragments.")
    return value.rstrip("/")


class JsonEndpoint:
    endpoint_role = "control endpoint"

    def __init__(self, url, token, ca_file, timeout=15):
        self.url, self.token, self.timeout = secure_url(url), token, timeout
        self.context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
        # Direct connections keep operator/service credentials out of HTTP proxies.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=self.context),
            NoRedirect(),
        )

    def call(self, operation, *, _timeout=None, **fields):
        operation_name = (
            operation
            if isinstance(operation, str) and re.fullmatch(r"[a-z_]{1,40}", operation)
            else "unknown operation"
        )
        diagnostic = f"{self.endpoint_role} {operation_name}"
        request = urllib.request.Request(
            self.url,
            data=json.dumps(dict(op=operation, **fields)).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            maximum = (
                32 * 1024 * 1024
                if operation in {"archive", "results", "snapshot", "pending_requests", "resolve_request"}
                else 300 * 1024
                if operation == "events"
                else 65536
            )
            timeout = (
                max(self.timeout, 60)
                if operation
                in {
                    "connect",
                    "finalize_terminal",
                    "interrupt",
                    "archive",
                    "snapshot",
                    "pending_requests",
                    "verify_snapshot",
                    "restore",
                    "cancel_jobs",
                }
                or operation == "review_phase"
                and fields.get("stage") == "start"
                else self.timeout
            )
            if operation == "review_triage":
                # Triage may reconcile an earlier broker receipt, then submit
                # when it was never received. Each has its own Decisions bound.
                # TurnCoordinator renews the host lease throughout this wait.
                timeout = max(timeout, 2 * MAX_DECISIONS_TIMEOUT_SECONDS + 30)
            if _timeout is not None:
                timeout = min(timeout, _timeout)
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read(maximum + 1)
            if len(body) > maximum:
                raise OperatorError(
                    f"HTTPS {diagnostic} response exceeds the {maximum}-byte limit; stopped for inspection.",
                    "RESPONSE_TOO_LARGE",
                )
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except urllib.error.HTTPError as failure:
            code = "REQUEST_REJECTED"
            try:
                detail = json.loads(failure.read(4096)).get("error")
                if isinstance(detail, str) and re.fullmatch(r"[A-Z_]{1,80}", detail):
                    code = detail
            except (ValueError, AttributeError):
                pass
            finally:
                failure.close()
            raise OperatorError(
                f"HTTPS {diagnostic} rejected: HTTP {failure.code} {code}.", code, failure.code
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError, EOFError) as failure:
            cause = (
                failure.reason
                if isinstance(failure, urllib.error.URLError) and isinstance(failure.reason, BaseException)
                else failure
            )
            # Only positive transport failures can authorize an automatic
            # infrastructure rejoin. TLS/verification and unknown OS failures
            # retain their inspection boundary.
            code = None
            if not isinstance(cause, ssl.SSLError):
                if isinstance(cause, TimeoutError):
                    code = "TRANSPORT_TIMEOUT"
                elif isinstance(cause, (ConnectionError, socket.gaierror, EOFError)) or (
                    isinstance(cause, OSError)
                    and cause.errno in {errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPIPE}
                ):
                    code = "TRANSPORT_UNAVAILABLE"
            raise OperatorError(
                f"Cannot reach or verify HTTPS {diagnostic} ({type(cause).__name__}); stopped for inspection.",
                code,
            ) from None
        except (ValueError, KeyError, TypeError) as failure:
            raise OperatorError(
                f"Invalid HTTPS {diagnostic} response ({type(failure).__name__}); stopped for inspection."
            ) from None


class Operator(JsonEndpoint):
    endpoint_role = "host operator"

    def __init__(self, host_url, token, ca_file, timeout=15):
        self.host_url = secure_url(host_url)
        super().__init__(self.host_url + "/operator", token, ca_file, timeout)
        self._lease_lock = threading.Lock()
        self._lease_deadline = None
        self._server_instance = None

    def call(self, operation, **fields):
        began = time.monotonic()
        with self._lease_lock:
            lease_deadline = self._lease_deadline
        deadline = min(began + 2 * self.timeout, lease_deadline - 5) if lease_deadline else began + 2 * self.timeout
        value = None
        for attempt in range(2):
            try:
                value = super().call(
                    operation, _timeout=None if attempt == 0 else deadline - time.monotonic(), **fields
                )
                break
            except OperatorError as error:
                if (
                    attempt
                    or operation not in {"status", "heartbeat"}
                    or error.code != "TRANSPORT_TIMEOUT"
                    or deadline - time.monotonic() < 0.1
                ):
                    raise
        try:
            if not isinstance(value, dict):
                raise ValueError()
            status = value["status"]
            if not isinstance(status, dict):
                raise ValueError()
            for key in ("epoch", "serverInstance", "phase"):
                if not isinstance(status.get(key), str):
                    raise ValueError()
            if status["phase"] not in {"ACTIVE", "PAUSED", "FINISHED", "HALTED"}:
                raise ValueError()
            if any(type(status.get(key)) is not int or status[key] < 0 for key in ("turn", "handoff")):
                raise ValueError()
            war_turn = status.get("warDeclarationTurn")
            if war_turn is not None and (type(war_turn) is not int or war_turn < 0):
                raise ValueError()
            with self._lease_lock:
                if self._server_instance is not None and self._server_instance != status["serverInstance"]:
                    self._lease_deadline = None
                self._server_instance = status["serverInstance"]
                remaining = status.get("leaseExpiresInMs")
                if operation in {"acquire", "heartbeat"} and status.get("leaseId") == fields.get("leaseId"):
                    if type(remaining) is int and remaining > 0:
                        self._lease_deadline = began + remaining / 1000
                elif not status.get("leaseId"):
                    self._lease_deadline = None
            return value
        except (ValueError, KeyError, TypeError):
            raise OperatorError("Invalid operator response; stopped for inspection.") from None
