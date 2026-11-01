"""Codex control errors identify the stalled operation without exposing inputs."""

import asyncio
import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from civ_agent.agent_history import HISTORY_METHODS, HistoryStore
from civ_agent.agent_service import AgentService, CodexProcess, ProcessDrainError, ServiceError
from civ_agent.diagnostics import Diagnostics


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


@pytest.mark.parametrize(
    "status,reason,failed_turn,fatal",
    [
        ("completed", "PROVIDER_FORWARD_FAILED", "turn-1", False),
        ("completed", "PROVIDER_DECISION_REQUEST_LIMIT", "turn-1", True),
        ("interrupted", "PROVIDER_FORWARD_FAILED", "turn-1", True),
        ("completed", "PROVIDER_FORWARD_FAILED", "turn-2", True),
    ],
)
def test_late_provider_failure_is_fenced_to_its_completed_turn(tmp_path, status, reason, failed_turn, fatal):
    async def run():
        seen = []
        arrived, release = asyncio.Event(), asyncio.Event()

        async def notification(method, params):
            if method == "turn/completed":
                arrived.set()
                await release.wait()
            seen.append((method, params))

        process = CodexProcess("codex", tmp_path, "", None, notification)
        socket = Socket()
        process.socket = cast(aiohttp.ClientWebSocketResponse, socket)
        process.reader_task = asyncio.create_task(process._read())
        try:
            socket.feed(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": status}},
                }
            )
            await asyncio.wait_for(arrived.wait(), 1)
            # Socket receipt fences the result even while durable notification
            # delivery is blocked. The provider error cannot win that race.
            await process._provider_notice(
                "service/providerRequestBlocked",
                {"reason": reason, "threadId": "thread-1", "turnId": failed_turn, "stage": "upstream_read"},
            )
            assert (process.transport_error == reason) == fatal
        finally:
            release.set()
            await asyncio.wait_for(process.close(), 1)
        failures = [p for m, p in seen if m == "service/requestFailed"]
        assert bool(failures) == fatal
        if not fatal:
            assert any(p.get("afterCompletedTurn") for m, p in seen if m == "service/transportStatus")

    asyncio.run(run())


def test_rpc_error_metadata_survives_pending_request_cleanup_and_shutdown(tmp_path):
    async def run():
        seen = []
        blocked, release = asyncio.Event(), asyncio.Event()

        async def notification(method, params):
            if method == "service/requestFailed":
                blocked.set()
                await release.wait()
            seen.append((method, params))

        process = CodexProcess("codex", tmp_path, "", None, notification)
        socket = Socket()
        process.socket = cast(aiohttp.ClientWebSocketResponse, socket)
        process.reader_task = asyncio.create_task(process._read())
        request = asyncio.create_task(
            process.request("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1", "token": "PRIVATE_TOKEN"})
        )
        command = await asyncio.wait_for(socket.outgoing.get(), 1)
        socket.feed(
            {
                "id": command["id"],
                "error": {
                    "code": -32000,
                    "message": "No active turn PRIVATE_TOKEN",
                    "data": {"prompt": "PRIVATE_PROMPT"},
                },
            }
        )
        with pytest.raises(ServiceError, match="^CODEX_REQUEST_FAILED$") as caught:
            await asyncio.wait_for(request, 1)
        # The request owner can persist the reason even while notification delivery is blocked.
        detail = caught.value.request_failure
        assert detail is not None
        assert detail["reason"] == "turnNotActive"
        assert detail["rpcMethod"] == "turn/interrupt"
        assert "PRIVATE" not in json.dumps(detail)
        await asyncio.wait_for(blocked.wait(), 1)
        assert not process.pending and not process.requests
        # Diagnostics must not block the socket reader or independent control replies.
        other = asyncio.create_task(process.request("thread/read", {}))
        next_command = await asyncio.wait_for(socket.outgoing.get(), 1)
        socket.feed({"id": next_command["id"], "result": {"thread": {"id": "thread-1"}}})
        assert await asyncio.wait_for(other, 1) == {"thread": {"id": "thread-1"}}
        close = asyncio.create_task(process.close())
        release.set()
        await asyncio.wait_for(close, 1)
        failures = [params for method, params in seen if method == "service/requestFailed"]
        assert len(failures) == 1
        assert {k: failures[0][k] for k in ["rpcMethod", "rpcId", "rpcCode", "reason", "threadId", "turnId"]} == {
            "rpcMethod": "turn/interrupt",
            "rpcId": command["id"],
            "rpcCode": -32000,
            "reason": "turnNotActive",
            "threadId": "thread-1",
            "turnId": "turn-1",
        }
        assert "PRIVATE" not in json.dumps(seen)

    asyncio.run(run())


@pytest.mark.parametrize("purpose", ["gameplay", "economy", "military"])
def test_rpc_failure_before_thread_creation_is_logged_and_saved_by_owning_role(tmp_path, purpose):
    async def run():
        record = {"activationId": "job-1", "purpose": purpose, "threadId": None}
        diagnostics = Diagnostics(tmp_path, "Rome")
        service = SimpleNamespace(
            active_id="job-1", records={"job-1": record}, diagnostics=diagnostics, _save=AsyncMock()
        )
        detail = {"rpcMethod": "account/login/start", "rpcId": 2, "rpcCode": -32000, "reason": "unauthorized"}
        try:
            await AgentService._notification(cast(AgentService, service), "service/requestFailed", detail)
            assert record["requestFailure"] == detail
            service._save.assert_awaited_once_with(record)
        finally:
            diagnostics.close()
        entries = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
        errors = [entry for entry in entries if entry["event"] == "codex_request_failed"]
        assert len(errors) == 1
        assert errors[0]["activationId"] == "job-1" and errors[0]["purpose"] == purpose
        assert all(errors[0][key] == value for key, value in detail.items())

    asyncio.run(run())


def test_thousands_of_tiny_deltas_are_coalesced_without_losing_text_or_event_order(tmp_path):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def notification(method, params):
            if method == "first":
                started.set()
                await release.wait()
            seen.append((method, params))

        process = CodexProcess("codex", tmp_path, "", None, notification)
        process._queue_notification("first", {})
        await asyncio.wait_for(started.wait(), 1)
        common = {"threadId": "thread-1", "turnId": "turn-1", "itemId": "report"}
        chunks = ["{", *[" " * (index % 13 + 1) for index in range(6725)], "}"]
        for chunk in chunks:
            assert process._queue_notification("item/agentMessage/delta", {**common, "delta": chunk})
        assert process._queue_notification("thread/tokenUsage/updated", {"tokenUsage": {"total": {"outputTokens": 99}}})
        assert process._queue_notification("item/agentMessage/delta", {**common, "delta": "after usage"})
        assert process._queue_notification("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}})
        process._end_notifications()
        assert process.notification_peak_count < 25
        release.set()
        await asyncio.wait_for(process.close(), 1)
        text = [params["delta"] for method, params in seen if method == "item/agentMessage/delta"]
        assert "".join(text[:-1]) == "".join(chunks)
        assert text[-1] == "after usage"
        methods = [method for method, _ in seen]
        usage_index = methods.index("thread/tokenUsage/updated")
        assert methods[usage_index + 1 : usage_index + 3] == ["item/agentMessage/delta", "turn/completed"]
        assert not process.transport_error
        assert process.notification_count == process.notification_bytes == 0

    asyncio.run(run())


def test_deltas_do_not_merge_across_items_turns_or_changed_metadata(tmp_path):
    async def run():
        seen = []

        async def notification(method, params):
            seen.append((method, params))

        process = CodexProcess("codex", tmp_path, "", None, notification)
        values = [
            {"itemId": "one", "turnId": "one", "delta": "a"},
            {"itemId": "two", "turnId": "one", "delta": "b"},
            {"itemId": "two", "turnId": "two", "delta": "c"},
            {"itemId": "two", "turnId": "two", "delta": "d", "contentIndex": 1},
        ]
        for value in values:
            assert process._queue_notification("item/agentMessage/delta", value)
        process._end_notifications()
        await process.close()
        assert [params for method, params in seen if method == "item/agentMessage/delta"] == values

    asyncio.run(run())


def test_coalesced_history_redacts_secrets_across_batch_boundaries_and_omits_raw_reasoning(tmp_path):
    async def run():
        credential = "s" * 43
        record = {"activationId": "review", "threadId": "thread-1", "phase": "RUNNING"}
        store = HistoryStore(tmp_path / "history", "other-secret")
        store.begin(record, None, credential)
        store.next_conversation("review", "strategic_review")
        store.bind_turn("review", "turn-1")

        async def notification(method, params):
            if method in HISTORY_METHODS:
                store.notify("review", "thread-1", method, params)

        process = CodexProcess("codex", tmp_path, "", None, notification)
        common = {"threadId": "thread-1", "turnId": "turn-1", "itemId": "report"}
        for text in ["A visible report. ", credential[:20]]:
            process._queue_notification("item/agentMessage/delta", {**common, "delta": text})
        process._queue_notification("thread/tokenUsage/updated", {})
        for text in [credential[20:], " Its ending."]:
            process._queue_notification("item/agentMessage/delta", {**common, "delta": text})
        process._queue_notification("item/reasoning/textDelta", {**common, "delta": "RAW_REASONING_SENTINEL"})
        process._queue_notification(
            "item/completed",
            {
                **common,
                "item": {
                    "id": "report",
                    "type": "agentMessage",
                    "text": "A visible report. " + credential + " Its ending.",
                    "phase": "final_answer",
                },
            },
        )
        await process.close()
        text = store.logs["review"].path.read_text()
        assert credential not in text and credential[:20] not in text
        assert "RAW_REASONING_SENTINEL" not in text
        assert "[REDACTED]" in text and "Its ending." in text
        assert process.notification_coalesced == 2

    asyncio.run(run())


def test_review_size_guard_wakes_failure_even_with_blocked_history(tmp_path):
    async def run():
        started, release, failed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = []

        async def notification(method, params):
            if method == "service/transportFailure":
                assert params["reason"] == "REVIEW_OUTPUT_LIMIT"
                failed.set()
                return
            if method == "item/started":
                started.set()
                await release.wait()
            seen.append((method, params))

        process = CodexProcess("codex", tmp_path, "", None, notification)
        socket = Socket()
        process.socket = cast(aiohttp.ClientWebSocketResponse, socket)
        process.reader_task = asyncio.create_task(process._read())
        request = asyncio.create_task(
            process.request("turn/start", {"threadId": "thread-1", "outputSchema": {"type": "object"}})
        )
        command = await asyncio.wait_for(socket.outgoing.get(), 1)
        socket.feed({"id": command["id"], "result": {"turn": {"id": "turn-1"}}})
        await asyncio.wait_for(request, 1)
        common = {"threadId": "thread-1", "turnId": "turn-1"}
        socket.feed(
            {
                "method": "item/started",
                "params": {**common, "item": {"type": "agentMessage", "id": "report", "phase": "final_answer"}},
            }
        )
        await asyncio.wait_for(started.wait(), 1)
        socket.feed(
            {
                "method": "item/agentMessage/delta",
                "params": {**common, "itemId": "report", "delta": '{"answer":"' + "x" * (128 * 1024)},
            }
        )
        for _ in range(6000):
            socket.feed(
                {"method": "item/agentMessage/delta", "params": {**common, "itemId": "report", "delta": " " * 10}}
            )
        await asyncio.wait_for(failed.wait(), 1)
        assert not seen
        assert process.notification_peak_count < 10
        release.set()
        await process.close()
        assert process.notification_drained

    asyncio.run(run())


def test_flush_timeout_reports_confirmed_process_exit_separately(tmp_path, monkeypatch):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def notification(method, params):
            if method == "first":
                started.set()
                await release.wait()

        process = CodexProcess("codex", tmp_path, "", None, notification)
        child = SimpleNamespace(pid=12345, returncode=None, wait=AsyncMock())
        process.process = cast(asyncio.subprocess.Process, child)

        async def wait():
            child.returncode = -15

        child.wait.side_effect = wait
        monkeypatch.setattr("civ_agent.agent_service.os.killpg", Mock())
        monkeypatch.setattr("civ_agent.agent_service.CODEX_NOTIFICATION_FLUSH_SECONDS", 0.01)
        process._queue_notification("first", {})
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(ProcessDrainError, match="^CODEX_NOTIFICATION_FLUSH_TIMEOUT$") as failed:
            await process.close()
        assert failed.value.process_exited
        assert process.process_exited and not process.notification_drained
        release.set()
        await process.close()
        assert process.notification_drained

    asyncio.run(run())


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
