"""Bound actual inference requests before forwarding them to the selected provider.

Codex still owns authentication and conversation state. This local transport does
not log headers, prompts, responses, or reasoning. Every attempt, including a
failed connection, spends its reservation; uncertain requests are never refunded.
"""

import asyncio
import copy
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

from civ_agent.model_status import error_fields
from civ_agent.protocol import JsonObject, Notification

MAX_REJECTION_BYTES = 16 * 1024

HOP_HEADERS = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "content-length",
        "upgrade",
        "proxy-authorization",
        "proxy-authenticate",
        "te",
        "trailer",
    }
)


class RequestBudget:
    """A shared, persistent ceiling across roles, processes, retries and restarts."""

    def __init__(self, path: Path, limit: int):
        if limit < 1:
            raise ValueError("MODEL_REQUEST_LIMIT_INVALID")
        self.path, self.limit = path, limit
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY CHECK(id=1), ceiling INTEGER)")
            db.execute("INSERT OR IGNORE INTO budget VALUES (1, ?)", (limit,))
            if db.execute("SELECT ceiling FROM budget WHERE id=1").fetchone()[0] != limit:
                raise ValueError("MODEL_REQUEST_BUDGET_CHANGED")
            db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, kind TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS policy_retries (scope TEXT PRIMARY KEY, request_id TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, identity: str, kind: str, *, retry_scope: str | None = None) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                retry_scope is not None
                and db.execute("SELECT 1 FROM policy_retries WHERE scope=?", (retry_scope,)).fetchone()
            ):
                return False
            if db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= self.limit:
                return False
            db.execute("INSERT INTO requests VALUES (?, ?)", (identity, kind))
            if retry_scope is not None:
                db.execute("INSERT INTO policy_retries VALUES (?, ?)", (retry_scope, identity))
            return True

    def used(self) -> int:
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

    @classmethod
    def from_environment(cls):
        path, limit = os.environ.get("CIV_MODEL_REQUEST_BUDGET"), os.environ.get("CIV_MODEL_REQUEST_LIMIT")
        if path is None and limit is None:
            return None
        if not path or not limit or not limit.isdecimal() or not Path(path).is_absolute():
            raise ValueError("MODEL_REQUEST_BUDGET_INVALID")
        return cls(Path(path), int(limit))


@dataclass
class RequestWindow:
    thread: str
    schema: JsonObject
    turn: str | None = None
    attempts: set[str] = field(default_factory=set)


class ProviderGate:
    def __init__(
        self, notification: Notification, budget: RequestBudget | None = None, *, retry_scope: str | None = None
    ):
        self.notification, self.budget = notification, budget
        self.retry_scope = retry_scope
        self.route = "/" + secrets.token_urlsafe(32)
        self.runner: web.AppRunner | None = None
        self.session: aiohttp.ClientSession | None = None
        self.url: str | None = None
        self.upstream: str | None = None
        self.window: RequestWindow | None = None

    async def start(self):
        app = web.Application(client_max_size=32 * 1024 * 1024)
        app.router.add_route("*", self.route + "/{tail:.*}", self.forward)
        self.runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        address = self.runner.addresses[0]
        assert isinstance(address, tuple)
        self.url = f"http://127.0.0.1:{address[1]}{self.route}"
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300))

    def begin(self, thread: str, schema: JsonObject):
        self.window = RequestWindow(thread, copy.deepcopy(schema))

    def bind(self, turn: str):
        if self.window is not None:
            self.window.turn = turn

    async def deny(self, reason: str, window: RequestWindow | None, **details):
        await self.notification(
            "service/providerRequestBlocked",
            {"reason": reason, **({"threadId": window.thread, "turnId": window.turn} if window else {}), **details},
        )
        return web.json_response({"error": {"code": reason, "message": reason}}, status=400)

    async def forward(self, request: web.Request):
        window = self.window
        tail = request.match_info["tail"]
        if request.method == "GET" and tail == "responses":
            # Fail closed if a client ignores the HTTP-only provider setting.
            # WebSocket upgrades cannot bypass schema or request-budget checks.
            raise web.HTTPMethodNotAllowed("GET", ["POST"])
        # Model catalog reads are not inference. No other provider endpoints
        # (including tools or arbitrary destinations) are exposed by this gate.
        catalogue = request.method == "GET" and tail == "models"
        if self.upstream is None or not (
            catalogue or request.method == "POST" and tail in {"responses", "responses/compact"}
        ):
            return await self.deny("PROVIDER_ENDPOINT_NOT_PERMITTED", window)
        body = None
        identity = None
        if not catalogue:
            if window is None:
                return await self.deny("PROVIDER_DECISION_REQUIRED", window)
            kind = "compaction" if tail.endswith("/compact") else "generation"
            try:
                body = await request.json()
            except (ValueError, UnicodeError):
                return await self.deny("PROVIDER_REQUEST_INVALID", window)
            if not isinstance(body, dict):
                return await self.deny("PROVIDER_REQUEST_INVALID", window)
            if kind == "generation":
                formatting = body.get("text")
                shape = formatting.get("format") if isinstance(formatting, dict) else None
                if (
                    not isinstance(shape, dict)
                    or shape.get("type") != "json_schema"
                    or shape.get("strict") is not True
                    or shape.get("schema") != window.schema
                ):
                    return await self.deny("STRUCTURED_SCHEMA_REQUIRED", window)
                body.update(tools=[], tool_choice="none", parallel_tool_calls=False)
            if window is not self.window:
                return await self.deny("PROVIDER_DECISION_EXPIRED", window)
            if kind in window.attempts:
                return await self.deny("PROVIDER_DECISION_REQUEST_LIMIT", window)
            window.attempts.add(kind)  # No await between checking and claiming.
            identity = "provider:" + secrets.token_hex(16)
            if self.budget is not None:
                try:
                    reserved = await asyncio.to_thread(self.budget.reserve, identity, kind)
                except (sqlite3.Error, OSError):
                    return await self.deny("MODEL_REQUEST_BUDGET_UNAVAILABLE", window)
                if not reserved:
                    return await self.deny("MODEL_REQUEST_BUDGET_EXHAUSTED", window)
            await self.notification(
                "service/providerRequest",
                {"requestId": identity, "kind": kind, "threadId": window.thread, "turnId": window.turn},
            )
        assert self.session is not None
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in HOP_HEADERS | {"content-encoding", "accept-encoding"}
        }
        outgoing = None
        stage, forwarded_bytes = "upstream_connect", 0
        try:
            for attempt in range(2):
                if attempt and (
                    window is not self.window or request.transport is None or request.transport.is_closing()
                ):
                    return await self.deny("PROVIDER_DECISION_EXPIRED", window)
                stage = "upstream_connect"
                async with self.session.request(
                    request.method,
                    self.upstream.rstrip("/") + "/" + tail,
                    params=request.query,
                    headers=headers,
                    json=body,
                    allow_redirects=False,
                ) as response:
                    buffered = b""
                    if tail == "responses" and request.method == "POST" and response.status == 400:
                        stage = "upstream_read"
                        try:
                            buffered = await response.content.readexactly(MAX_REJECTION_BYTES + 1)
                        except asyncio.IncompleteReadError as error:
                            buffered = error.partial
                        if prompt_policy_rejection(buffered):
                            retry = await self.policy_retry(request, window, identity, attempt)
                            if retry is not None:
                                identity = retry
                                continue
                    outgoing = web.StreamResponse(
                        status=response.status,
                        headers={
                            key: value
                            for key, value in response.headers.items()
                            if key.lower() not in HOP_HEADERS | {"content-encoding"}
                        },
                    )
                    stage = "downstream_headers"
                    await outgoing.prepare(request)
                    stage = "downstream_body"
                    if buffered:
                        await outgoing.write(buffered)
                        forwarded_bytes += len(buffered)
                    stage = "upstream_read"
                    async for chunk in response.content.iter_any():
                        stage = "downstream_body"
                        await outgoing.write(chunk)
                        forwarded_bytes += len(chunk)
                        stage = "upstream_read"
                    stage = "downstream_eof"
                    await outgoing.write_eof()
                    stage = "upstream_close"
                    return outgoing
            raise AssertionError("Provider attempt limit exceeded")
        except (aiohttp.ClientError, OSError, TimeoutError) as error:
            # Keep only transport metadata, never exception text, URLs, headers,
            # prompts or response bodies. An interrupted attempt stays charged.
            details = {
                "stage": stage,
                "exceptionType": type(error).__name__,
                "requestId": identity,
                "forwardedBytes": forwarded_bytes,
                "upstreamStatus": outgoing.status if outgoing is not None else None,
            }
            if stage.startswith("downstream_"):
                # Codex can close its HTTP reader after the terminal SSE event,
                # before the proxy finishes writing the stream's trailing bytes.
                # Let its turn/completed or failed event determine the outcome;
                # a local socket close must not invalidate a completed decision.
                await self.notification(
                    "service/transportStatus",
                    {
                        "reason": "PROVIDER_CLIENT_DISCONNECTED",
                        **({"threadId": window.thread, "turnId": window.turn} if window else {}),
                        **details,
                    },
                )
                assert outgoing is not None
                outgoing.force_close()
                return outgoing
            failure = await self.deny("PROVIDER_FORWARD_FAILED", window, **details)
            if outgoing is not None and outgoing.prepared:
                # Headers have already gone out. A second HTTP response would
                # corrupt the stream and can leave the client waiting for EOF.
                outgoing.force_close()
                return outgoing
            return failure

    async def policy_retry(self, request, window, identity, attempt):
        """Retry only a terminal HTTP prompt-filter rejection, before forwarding bytes.

        Keep the complete request unchanged. A durable reservation limits this to
        one extra attempt per activation, even across process restarts. Model
        refusals, partial streams, other errors and uncertain outcomes never enter.
        """
        retry = None

        def current():
            return (
                window is not None
                and window is self.window
                and request.transport is not None
                and not request.transport.is_closing()
            )

        if attempt == 0 and current() and self.budget is not None:
            candidate = "provider:" + secrets.token_hex(16)
            try:
                reserved = await asyncio.to_thread(
                    self.budget.reserve,
                    candidate,
                    "generation",
                    retry_scope=self.retry_scope or window.thread,
                )
            except (sqlite3.Error, OSError):
                reserved = False
            if reserved and current():
                retry = candidate
        await self.notification(
            "service/providerRejection",
            {
                "threadId": window.thread if window else None,
                "turnId": window.turn if window else None,
                "requestId": identity,
                "reason": "promptPolicyRejected",
                "httpStatus": 400,
                "retryRequestId": retry,
            },
        )
        if retry is not None:
            await self.notification(
                "service/providerRequest",
                {"requestId": retry, "kind": "generation", "threadId": window.thread, "turnId": window.turn},
            )
        return retry

    async def close(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        if self.session is not None:
            await self.session.close()
            self.session = None


def prompt_policy_rejection(body: bytes) -> bool:
    """Recognize the provider's specific rejection without retaining its body."""
    if len(body) > MAX_REJECTION_BYTES:
        return False
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return False
    error = value.get("error") if isinstance(value, dict) else None
    return isinstance(error, dict) and error_fields(error)["reason"] == "promptPolicyRejected"


def upstream_url(provider: str, configuration: JsonObject, auth_type: str | None) -> str:
    from civ_agent.providers import OPENROUTER_URL

    if provider == "openrouter":
        return configuration.get("model_providers", {}).get(provider, {}).get("base_url") or OPENROUTER_URL
    return (
        configuration.get("openai_base_url")
        or configuration.get("model_providers", {}).get("openai", {}).get("base_url")
        or os.environ.get("OPENAI_BASE_URL")
        or ("https://api.openai.com/v1" if auth_type == "apiKey" else "https://chatgpt.com/backend-api/codex")
    )


def gate_configuration(provider: str, url: str) -> JsonObject:
    # Built-in OpenAI settings cannot be overridden. Use a transport-only alias
    # with the same Codex-managed authentication and model catalogue instead.
    gated_provider = "civ_openai" if provider == "openai" else provider
    prefix = "model_providers." + gated_provider + "."
    result: JsonObject = {
        "features.enable_request_compression": False,
        "model_provider": gated_provider,
        prefix + "base_url": url,
        prefix + "supports_websockets": False,
        prefix + "request_max_retries": 0,
        prefix + "stream_max_retries": 0,
    }
    if provider == "openai":
        result.update(
            {
                prefix + "name": "OpenAI (host transport)",
                prefix + "wire_api": "responses",
                prefix + "requires_openai_auth": True,
                prefix + "model_catalog_url": url + "/models",
            }
        )
    return result
