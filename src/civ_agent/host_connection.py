"""Authenticated MCP client with one pooled HTTPS transport per connection owner."""

import asyncio
import json
import logging
from pathlib import Path

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from civ_agent.civilization_state import StateConflict
from civ_agent.protocol import JsonObject
from civ_agent.tls import client_ssl_context, https_url


class _ProtocolLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
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
    def __init__(self, url: str, token: str, ca_file: Path | None):
        self.url = https_url(url, ("/mcp",))
        self._token = token
        self._ssl = client_ssl_context(ca_file)
        self._client: Client | None = None
        self._owner: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._closed = False

    async def _maintain(self) -> None:
        # SDK task-group contexts must enter and exit in the same task. HTTP
        # handlers and the mailbox can issue concurrent requests through its session.
        try:
            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + self._token},
                verify=self._ssl,
                trust_env=False,
                follow_redirects=False,
                timeout=35,
                event_hooks={"response": [_reject_redirect]},
            ) as http:
                transport = streamable_http_client(self.url, http_client=http)
                # The Kotlin host implements the initialize-handshake protocol.
                async with Client(transport, mode="legacy", cache=None, read_timeout_seconds=35) as client:
                    self._client = client
                    self._ready.set()
                    await self._stop.wait()
        except Exception:
            # SDK/HTTP errors may include headers, response bodies or nested
            # validation inputs. The public boundary reports only a fixed code.
            pass
        finally:
            self._client = None
            self._ready.set()

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
        try:
            async with asyncio.timeout(35):
                client = await self._connected()
                tools, cursor, seen = [], None, set()
                while True:
                    page = await client.list_tools(cursor=cursor)
                    tools.extend(tool.model_dump(mode="json", by_alias=True, exclude_unset=True) for tool in page.tools)
                    cursor = page.next_cursor
                    if cursor is None:
                        return tools
                    if cursor in seen:
                        raise StateConflict("HOST_PROTOCOL_ERROR")
                    seen.add(cursor)
        except StateConflict:
            raise
        except Exception:
            self._stop.set()
            raise StateConflict("HOST_PROTOCOL_ERROR") from None

    async def tool(self, name: str, arguments: JsonObject) -> JsonObject:
        try:
            async with asyncio.timeout(35):
                client = await self._connected()
                # One call, with no elicitation/retry loop. The gateway owns
                # application request IDs and reconciliation of uncertain receipts.
                wire_arguments = {"request": arguments} if name == "player_communication" else arguments
                result = await client.session.call_tool(name, wire_arguments)
                value = result.structured_content
                if value is None:
                    value = json.loads(next(item.text for item in result.content if isinstance(item, TextContent)))
                if not isinstance(value, dict):
                    raise StateConflict("HOST_PROTOCOL_ERROR")
                return value
        except StateConflict:
            raise
        except Exception:
            self._stop.set()
            raise StateConflict("HOST_PROTOCOL_ERROR") from None

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
