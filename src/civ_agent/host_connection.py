"""Authenticated MCP client with one pooled HTTPS transport per connection owner."""

import asyncio
import json
import logging
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from civ_agent.civilization_state import StateConflict
from civ_agent.diagnostics import Diagnostics
from civ_agent.protocol import JsonObject
from civ_agent.tls import client_ssl_context, https_url

_active_connection: ContextVar["HostConnection | None"] = ContextVar("host_diagnostics", default=None)


class _ProtocolLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        connection = _active_connection.get()
        if connection is not None:
            failure = record.exc_info[1] if record.exc_info else None
            if failure is None and isinstance(record.args, tuple):
                failure = next((value for value in record.args if isinstance(value, BaseException)), None)
            connection.record(
                "mcp_event", error=failure, logger=record.name, function=record.funcName, severity=record.levelname
            )
        # SDK diagnostics can embed whole responses, including validation inputs.
        # Keep the operation and severity without wire contents or tracebacks.
        record.msg, record.args = "MCP protocol event in %s", (record.funcName,)
        record.exc_info = record.exc_text = record.stack_info = None
        return True


for _logger in ("mcp.client.streamable_http", "mcp.shared.jsonrpc_dispatcher"):
    logging.getLogger(_logger).addFilter(_ProtocolLog())


async def _reject_redirect(response: httpx2.Response) -> None:
    # The SDK follows some redirects itself, regardless of follow_redirects=False.
    # Reject them before that handling can forward a player credential or command.
    if 300 <= response.status_code < 400:
        raise StateConflict("HOST_REDIRECT_REJECTED")


class HostConnection:
    def __init__(
        self,
        url: str,
        token: str,
        ca_file: Path | None,
        *,
        diagnostics: Diagnostics | None = None,
        context: JsonObject | None = None,
    ):
        self.url = https_url(url, ("/mcp",))
        self.diagnostics = diagnostics
        self.context = dict(context or {})
        self.connection_id = uuid.uuid4().hex[:12]
        self.session_attempt = 0
        self.call_sequence = 0
        self._token = token
        self._ssl = client_ssl_context(ca_file)
        self._client: Client | None = None
        self._owner: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._closed = False

    def record(self, event: str, *, error: BaseException | None = None, **fields) -> None:
        if self.diagnostics is not None:
            self.diagnostics.emit(
                event,
                error=error,
                **self.context,
                connectionId=self.connection_id,
                sessionAttempt=self.session_attempt,
                **fields,
            )

    @staticmethod
    def _request_metadata(request: httpx2.Request) -> JsonObject:
        metadata: JsonObject = {"httpMethod": request.method}
        if request.method != "POST":
            return metadata
        try:
            body = json.loads(request.content)
        except (ValueError, httpx2.RequestNotRead):
            return metadata
        if not isinstance(body, dict):
            return metadata
        metadata.update(rpcMethod=body.get("method"), rpcId=body.get("id"))
        params = body.get("params", {})
        if isinstance(params, dict):
            metadata["tool"] = params.get("name")
            arguments = params.get("arguments", {})
            if isinstance(arguments, dict):
                if params.get("name") == "player_communication":
                    arguments = arguments.get("request", {})
                if isinstance(arguments, dict):
                    metadata.update(
                        requestId=arguments.get("requestId"),
                        commandKind=arguments.get("kind"),
                        communicationOp=arguments.get("op"),
                    )
        return {key: value for key, value in metadata.items() if value is not None}

    async def _request_sent(self, request: httpx2.Request) -> None:
        request.extensions["civ_started"] = time.monotonic()
        self.record("host_http_request", **self._request_metadata(request))

    async def _response_received(self, response: httpx2.Response) -> None:
        request = response.request
        started = request.extensions.get("civ_started", time.monotonic())
        length = response.headers.get("content-length", "")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        self.record(
            "host_http_response",
            **self._request_metadata(request),
            httpStatus=response.status_code,
            elapsedMs=round((time.monotonic() - started) * 1000, 3),
            contentLength=int(length) if length.isdecimal() and len(length) < 16 else None,
            contentType=content_type
            if content_type in {"application/json", "text/event-stream", "text/html", "text/plain"}
            else "other",
        )
        await _reject_redirect(response)

    async def _maintain(self) -> None:
        # SDK task-group contexts must enter and exit in the same task. HTTP
        # handlers and the mailbox can issue concurrent requests through its session.
        scope = _active_connection.set(self)
        self.session_attempt += 1
        stage = "connect"
        self.record("host_session_start")
        try:
            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + self._token},
                verify=self._ssl,
                trust_env=False,
                follow_redirects=False,
                timeout=35,
                event_hooks={"request": [self._request_sent], "response": [self._response_received]},
            ) as http:
                transport = streamable_http_client(self.url, http_client=http)
                # The Kotlin host implements the initialize-handshake protocol.
                async with Client(transport, mode="legacy", cache=None, read_timeout_seconds=35) as client:
                    self._client = client
                    stage = "connected"
                    self.record("host_session_ready")
                    self._ready.set()
                    await self._stop.wait()
        except Exception as error:
            self.record("host_session_failed", error=error, stage=stage)
        finally:
            self._client = None
            self._ready.set()
            self.record("host_session_closed", closeRequested=self._closed or self._stop.is_set())
            _active_connection.reset(scope)

    async def _connected(self) -> Client:
        if self._closed:
            raise StateConflict("HOST_CONNECTION_CLOSED")
        if self._stop.is_set() and self._owner is not None:
            # A failed transport may still be unwinding when its caller receives
            # the error. A later receipt read must wait for that teardown first.
            await asyncio.shield(self._owner)
        if self._closed:
            raise StateConflict("HOST_CONNECTION_CLOSED")
        if self._owner is None or self._owner.done():
            self._ready = asyncio.Event()
            self._stop = asyncio.Event()
            self._owner = asyncio.create_task(self._maintain())
        await self._ready.wait()
        if self._client is None or self._closed:
            raise StateConflict("HOST_UNAVAILABLE")
        return self._client

    async def list_tools(self) -> list[JsonObject]:
        self.call_sequence += 1
        call_id = self.call_sequence
        started = time.monotonic()
        stage = "connect"
        scope = _active_connection.set(self)
        self.record("host_call_started", callId=call_id, tool="tools/list")
        try:
            async with asyncio.timeout(35):
                client = await self._connected()
                stage = "list_tools"
                tools, cursor, seen = [], None, set()
                while True:
                    page = await client.list_tools(cursor=cursor)
                    tools.extend(tool.model_dump(mode="json", by_alias=True, exclude_unset=True) for tool in page.tools)
                    cursor = page.next_cursor
                    if cursor is None:
                        self.record(
                            "host_call_completed",
                            callId=call_id,
                            tool="tools/list",
                            toolCount=len(tools),
                            elapsedMs=round((time.monotonic() - started) * 1000, 3),
                        )
                        return tools
                    if cursor in seen:
                        raise StateConflict("HOST_PROTOCOL_ERROR")
                    seen.add(cursor)
        except StateConflict as error:
            self.record("host_call_failed", callId=call_id, tool="tools/list", stage=stage, error=error)
            raise
        except Exception as error:
            self.record("host_call_failed", callId=call_id, tool="tools/list", stage=stage, error=error)
            self._stop.set()
            raise StateConflict("HOST_PROTOCOL_ERROR") from None
        finally:
            _active_connection.reset(scope)

    async def tool(self, name: str, arguments: JsonObject) -> JsonObject:
        self.call_sequence += 1
        metadata = {
            "callId": self.call_sequence,
            "tool": name,
            "requestId": arguments.get("requestId"),
            "commandKind": arguments.get("kind"),
            "communicationOp": arguments.get("op"),
        }
        started = time.monotonic()
        stage = "connect"
        scope = _active_connection.set(self)
        self.record("host_call_started", **metadata)
        try:
            async with asyncio.timeout(35):
                client = await self._connected()
                # One call, with no elicitation/retry loop. The gateway owns
                # application request IDs and reconciliation of uncertain receipts.
                wire_arguments = {"request": arguments} if name == "player_communication" else arguments
                stage = "call_tool"
                result = await client.session.call_tool(name, wire_arguments)
                stage = "decode_result"
                value = result.structured_content
                if value is None:
                    value = json.loads(next(item.text for item in result.content if isinstance(item, TextContent)))
                if not isinstance(value, dict):
                    raise StateConflict("HOST_PROTOCOL_ERROR")
                rejection = value.get("error")
                code = rejection.get("code") if isinstance(rejection, dict) else rejection
                self.record(
                    "host_call_completed",
                    **metadata,
                    elapsedMs=round((time.monotonic() - started) * 1000, 3),
                    outcome="rejected" if "error" in value else "success",
                    errorCode=code
                    if isinstance(code, str)
                    and 0 < len(code) <= 64
                    and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_" for c in code)
                    else None,
                )
                return value
        except asyncio.CancelledError:
            self.record(
                "host_call_cancelled", **metadata, stage=stage, elapsedMs=round((time.monotonic() - started) * 1000, 3)
            )
            raise
        except StateConflict as error:
            self.record(
                "host_call_failed",
                **metadata,
                stage=stage,
                error=error,
                elapsedMs=round((time.monotonic() - started) * 1000, 3),
            )
            raise
        except Exception as error:
            self.record(
                "host_call_failed",
                **metadata,
                stage=stage,
                error=error,
                elapsedMs=round((time.monotonic() - started) * 1000, 3),
            )
            self._stop.set()
            raise StateConflict("HOST_PROTOCOL_ERROR") from None
        finally:
            _active_connection.reset(scope)

    async def close(self) -> None:
        self._closed = True
        self._stop.set()
        if self._owner is not None:
            cancelled = False
            while not self._owner.done():
                try:
                    await asyncio.shield(self._owner)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError()

    async def __aenter__(self):
        # Connect on the first request; merely preparing a model job costs no I/O.
        return self

    async def __aexit__(self, *_):
        await self.close()
