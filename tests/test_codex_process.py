"""Codex control errors identify the stalled operation without exposing inputs."""

import asyncio
import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from civ_agent.agent_service import CodexProcess, ServiceError


def test_request_timeout_identifies_method_and_clears_pending_state(tmp_path, monkeypatch):
    async def run():
        process = CodexProcess("codex", tmp_path, "", None, AsyncMock())
        process.socket = Mock()
        process.socket.send_json = AsyncMock()

        async def timeout(future, seconds):
            assert seconds == 30 and list(process.pending.values()) == [future]
            future.cancel()
            raise TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", timeout)
        with pytest.raises(TimeoutError, match="^Codex thread/start did not respond within 30 seconds$"):
            await process.request("thread/start", {"developerInstructions": "PRIVATE_PROMPT"})
        assert not process.pending and not process.requests

    asyncio.run(run())


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(message))

    def feed(self, message):
        self.incoming.put_nowait(message)

    async def send_json(self, message):
        self.outgoing.put_nowait(message)

    async def close(self):
        self.closed = True


def test_slow_callback_does_not_block_control_responses_or_lose_ordered_events(tmp_path):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def notification(method, params):
            if method == "item/started":
                started.set()
                await release.wait()
            seen.append((method, params))

        process = CodexProcess("codex", tmp_path, "", None, notification)
        socket = Socket()
        process.socket = cast(aiohttp.ClientWebSocketResponse, socket)
        process.reader_task = asyncio.create_task(process._read())
        socket.feed({"method": "item/started", "params": {"item": {"id": "tool-1", "type": "mcpToolCall"}}})
        await asyncio.wait_for(started.wait(), 1)
        request = asyncio.create_task(process.request("turn/interrupt", {"threadId": "thread-1"}))
        command = await asyncio.wait_for(socket.outgoing.get(), 1)
        events = [
            ("thread/tokenUsage/updated", {"tokenUsage": {"total": {"inputTokens": 123}}}),
            ("item/completed", {"item": {"id": "tool-1", "type": "mcpToolCall", "status": "completed"}}),
            ("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}),
        ]
        for method, params in events:
            socket.feed({"method": method, "params": params})
        socket.feed({"id": command["id"], "result": {"interrupted": True}})
        socket.feed({"id": 100, "method": "item/commandExecution/requestApproval", "params": {}})
        assert await asyncio.wait_for(request, 1) == {"interrupted": True}
        refusal = await asyncio.wait_for(socket.outgoing.get(), 1)
        assert refusal["id"] == 100 and refusal["error"]["code"] == -32000
        assert not seen
        socket.feed(None)
        await asyncio.wait_for(process.reader_task, 1)
        release.set()
        assert process.notification_task is not None
        await asyncio.wait_for(process.notification_task, 1)
        assert seen[1:4] == events
        assert [method for method, _ in seen] == [
            "item/started",
            *[method for method, _ in events],
            "service/approvalRequired",
            "service/transportStatus",
            "service/disconnected",
        ]
        assert seen[-2][1]["deliveredNotifications"] == 5
        assert process.notification_bytes == process.notification_count == 0
        await process.close()

    asyncio.run(run())


@pytest.mark.parametrize("bound", ["count", "bytes"])
def test_notification_overflow_fails_explicitly_and_flushes_accepted_events(tmp_path, monkeypatch, bound):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        seen = []
        failed = asyncio.Event()

        async def notification(method, params):
            if method == "service/transportFailure":
                assert params == {"reason": "CODEX_NOTIFICATION_OVERFLOW"}
                failed.set()
                return
            if method == "first":
                started.set()
                await release.wait()
            seen.append((method, params))

        process = CodexProcess("codex", tmp_path, "", None, notification)
        socket = Socket()
        process.socket = cast(aiohttp.ClientWebSocketResponse, socket)
        process.reader_task = asyncio.create_task(process._read())
        socket.feed({"method": "first", "params": {}})
        await asyncio.wait_for(started.wait(), 1)
        if bound == "count":
            monkeypatch.setattr("civ_agent.agent_service.CODEX_NOTIFICATION_COUNT", 2)
        else:
            monkeypatch.setattr("civ_agent.agent_service.CODEX_NOTIFICATION_BYTES", process.notification_bytes + 72)
        request = asyncio.create_task(process.request("thread/read", {}))
        await asyncio.wait_for(socket.outgoing.get(), 1)
        socket.feed({"method": "second", "params": {}})
        socket.feed({"method": "overflow", "params": {}})
        await asyncio.wait_for(process.reader_task, 1)
        with pytest.raises(ServiceError, match="^CODEX_NOTIFICATION_OVERFLOW$"):
            await request
        await asyncio.wait_for(failed.wait(), 1)
        assert process.notification_count == 2
        release.set()
        assert process.notification_task is not None
        await asyncio.wait_for(process.notification_task, 1)
        assert [method for method, _ in seen] == ["first", "second", "service/transportStatus", "service/disconnected"]
        assert seen[-1][1] == {"reason": "CODEX_NOTIFICATION_OVERFLOW"}
        assert process.notification_bytes == process.notification_count == 0
        await process.close()

    asyncio.run(run())


def test_close_flushes_queued_usage_before_closing_socket(tmp_path):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def notification(method, params):
            if method == "first":
                started.set()
                await release.wait()
            assert not socket.closed
            seen.append(method)

        process = CodexProcess("codex", tmp_path, "", None, notification)
        socket = Socket()
        process.socket = cast(aiohttp.ClientWebSocketResponse, socket)
        process.reader_task = asyncio.create_task(process._read())
        socket.feed({"method": "first", "params": {}})
        socket.feed({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {"inputTokens": 42}}}})
        await asyncio.wait_for(started.wait(), 1)
        close = asyncio.create_task(process.close())
        await asyncio.gather(process.reader_task, return_exceptions=True)
        assert not close.done()
        release.set()
        await asyncio.wait_for(close, 1)
        assert seen == ["first", "thread/tokenUsage/updated", "service/transportStatus", "service/disconnected"]
        assert socket.closed
        await process.close()

    asyncio.run(run())


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_interrupted_close_keeps_flush_owned_and_can_finish_later(tmp_path, monkeypatch, stop):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        delivered = []

        async def notification(method, params):
            if method == "first":
                started.set()
                await release.wait()
            delivered.append(method)

        process = CodexProcess("codex", tmp_path, "", None, notification)
        socket = Socket()
        process.socket = cast(aiohttp.ClientWebSocketResponse, socket)
        process.reader_task = asyncio.create_task(process._read())
        socket.feed({"method": "first", "params": {}})
        socket.feed({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}})
        await asyncio.wait_for(started.wait(), 1)
        if stop == "timeout":
            monkeypatch.setattr("civ_agent.agent_service.CODEX_NOTIFICATION_FLUSH_SECONDS", 0.01)
        close = asyncio.create_task(process.close())
        await asyncio.gather(process.reader_task, return_exceptions=True)
        if stop == "cancel":
            close.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close
        else:
            with pytest.raises(ServiceError, match="^CODEX_NOTIFICATION_FLUSH_TIMEOUT$"):
                await close
        assert process.notification_task is not None
        assert not process.notification_task.done()
        release.set()
        await asyncio.wait_for(process.close(), 1)
        assert delivered == ["first", "turn/completed", "service/transportStatus", "service/disconnected"]
        assert socket.closed

    asyncio.run(run())


def test_callback_failure_stops_reader_and_reports_failure_after_remaining_events(tmp_path):
    async def run():
        seen = []

        async def notification(method, params):
            if method == "bad":
                raise OSError("PRIVATE_CALLBACK_DETAILS")
            if method == "service/transportFailure":
                assert params == {"reason": "CODEX_NOTIFICATION_FAILED"}
                return
            seen.append((method, params))

        process = CodexProcess("codex", tmp_path, "", None, notification)
        socket = Socket()
        process.socket = cast(aiohttp.ClientWebSocketResponse, socket)
        process.reader_task = asyncio.create_task(process._read())
        socket.feed({"method": "bad", "params": {}})
        socket.feed({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}})
        await asyncio.gather(process.reader_task, return_exceptions=True)
        assert process.notification_task is not None
        await asyncio.wait_for(process.notification_task, 1)
        assert [method for method, _ in seen] == ["turn/completed", "service/transportStatus", "service/disconnected"]
        assert seen[-2][1]["failedNotifications"] == 1
        assert seen[-1][1] == {"reason": "CODEX_NOTIFICATION_FAILED"}
        assert "PRIVATE_CALLBACK_DETAILS" not in repr(seen)
        await process.close()

    asyncio.run(run())


def test_slow_auth_refresh_does_not_block_other_control_responses(tmp_path):
    async def run():
        refreshing, release = asyncio.Event(), asyncio.Event()

        async def fetch(*, refresh):
            assert refresh is True
            refreshing.set()
            await release.wait()
            return {"chatgptAccountId": "account-1", "accessToken": "PRIVATE_TOKEN"}

        process = CodexProcess("codex", tmp_path, "", None, AsyncMock())
        process.auth_client = SimpleNamespace(fetch=fetch)
        socket = Socket()
        process.socket = cast(aiohttp.ClientWebSocketResponse, socket)
        process.reader_task = asyncio.create_task(process._read())
        socket.feed(
            {"id": 100, "method": "account/chatgptAuthTokens/refresh", "params": {"previousAccountId": "account-1"}}
        )
        await asyncio.wait_for(refreshing.wait(), 1)
        request = asyncio.create_task(process.request("turn/interrupt", {}))
        command = await asyncio.wait_for(socket.outgoing.get(), 1)
        socket.feed({"id": command["id"], "result": {"interrupted": True}})
        socket.feed({"id": 101, "method": "account/chatgptAuthTokens/refresh", "params": {}})
        assert await asyncio.wait_for(request, 1) == {"interrupted": True}
        refusal = await asyncio.wait_for(socket.outgoing.get(), 1)
        assert refusal["id"] == 101 and "error" in refusal
        release.set()
        refreshed = await asyncio.wait_for(socket.outgoing.get(), 1)
        assert refreshed == {"id": 100, "result": {"chatgptAccountId": "account-1", "accessToken": "PRIVATE_TOKEN"}}
        await process.close()

    asyncio.run(run())
