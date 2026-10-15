"""Correlate native notifications with the acknowledged model turn and its own counters."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from civ_agent.agent_service import AgentService, ServiceError
from tests.test_agent_service import FakeCodex, FakeFactory


class ReplayCodex(FakeCodex):
    async def request(self, method, payload):
        if method == "turn/start" and self.factory.replay_before_response:
            # Resuming can produce asynchronous notifications from a preceding turn.
            # Its completed status must not satisfy this new turn/start request.
            await self.notification(
                "turn/started", {"threadId": "thread-one", "turn": {"id": "old-turn", "status": "inProgress"}}
            )
            await self.notification(
                "turn/completed", {"threadId": "thread-one", "turn": {"id": "old-turn", "status": "completed"}}
            )
        result = await super().request(method, payload)
        if method == "turn/start" and self.factory.burst:
            turn_id = result["turn"]["id"]
            await self.notification(
                "thread/tokenUsage/updated",
                {
                    "threadId": "thread-one",
                    "turnId": turn_id,
                    "tokenUsage": {
                        "total": {
                            "inputTokens": 35,
                            "cachedInputTokens": 0,
                            "outputTokens": 0,
                            "reasoningOutputTokens": 0,
                        },
                        "last": {
                            "inputTokens": 25,
                            "cachedInputTokens": 0,
                            "outputTokens": 0,
                            "reasoningOutputTokens": 0,
                        },
                    },
                },
            )
            await self.notification(
                "turn/completed", {"threadId": "thread-one", "turn": {"id": turn_id, "status": "completed"}}
            )
        return result


class ReplayFactory(FakeFactory):
    def __init__(self):
        super().__init__()
        self.replay_before_response = False
        self.burst = False

    def __call__(self, executable, workspace, mcp_url, ca_file, notification):
        process = ReplayCodex(self, workspace, notification)
        self.instances.append(process)
        return process


class CompletionCorrelationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-correlation-test-")
        self.root = Path(self.temporary.name)
        self.factory = ReplayFactory()
        self.service = self.make_service()

    def make_service(self):
        service = AgentService(
            "Rome",
            self.root / "workspace",
            self.root / "service",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=self.factory,
            connection_factory=self.factory.connection,
        )
        self.factory.service = service
        return service

    async def asyncTearDown(self):
        await self.service.close()
        self.temporary.cleanup()

    def payload(self, activation="first", thread=None):
        return {
            "activationId": activation,
            "epoch": "epoch-one",
            "gameTurn": 0,
            "handoff": 0,
            "civilization": "Rome",
            "threadId": thread,
            "mcpUrl": "https://localhost:8765/mcp",
            "playerToken": "P" * 43,
            "prompt": "Play current host turn",
            "model": None,
            "timeoutSeconds": 20,
            "leaseSeconds": 10,
        }

    async def wait_running(self):
        for _ in range(100):
            if self.service.status()["phase"] != "STARTING":
                break
            await asyncio.sleep(0.001)
        self.assertEqual("RUNNING", self.service.status()["phase"])

    async def complete(self, turn):
        await self.service._notification(
            "turn/completed", {"threadId": "thread-one", "turn": {"id": turn, "status": "completed"}}
        )
        assert self.service.active_task is not None
        await asyncio.wait_for(asyncio.shield(self.service.active_task), 2)
        self.assertEqual("COMPLETED", self.service.status()["phase"])

    async def test_replayed_completion_before_rpc_response_cannot_complete_new_turn(self):
        self.factory.complete = False
        self.factory.replay_before_response = True
        await self.service.start(self.payload())
        await self.wait_running()
        self.assertEqual("turn-1", self.service.turn_id)
        assert self.service.active_task is not None
        self.assertFalse(self.service.active_task.done())
        await self.complete("turn-1")
        self.assertEqual("turn-1", self.service.status()["turnId"])

    async def test_replayed_started_after_rpc_response_cannot_replace_bound_turn(self):
        self.factory.complete = False
        await self.service.start(self.payload())
        await self.wait_running()
        await self.service._notification(
            "turn/started", {"threadId": "thread-one", "turn": {"id": "old-turn", "status": "inProgress"}}
        )
        await self.service._notification(
            "turn/completed", {"threadId": "thread-one", "turn": {"id": "old-turn", "status": "completed"}}
        )
        await asyncio.sleep(0)
        self.assertEqual("turn-1", self.service.turn_id)
        self.assertEqual("RUNNING", self.service.status()["phase"])
        await self.complete("turn-1")

    async def test_fresh_conversations_count_usage_independently_across_service_restarts(self):
        for index, amount in enumerate((10, 10, 10, 120)):
            self.factory.input_tokens = amount
            await self.service.start(self.payload(str(index)))
            assert self.service.active_task is not None
            await asyncio.wait_for(asyncio.shield(self.service.active_task), 2)
            self.assertEqual(amount, self.service.status()["usage"]["input_tokens"])
            await self.service.close()
            self.service = self.make_service()

    async def test_first_and_latest_early_updates_preserve_the_entire_new_turn_usage(self):
        self.factory.complete = False
        self.factory.burst = True
        await self.service.start(self.payload())
        assert self.service.active_task is not None
        await asyncio.wait_for(asyncio.shield(self.service.active_task), 2)
        self.assertEqual("COMPLETED", self.service.status()["phase"])
        self.assertEqual(35, self.service.status()["usage"]["input_tokens"])

    async def test_transport_failure_cannot_publish_completed_turn_as_success(self):
        self.factory.complete = False
        await self.service.start(self.payload())
        await self.wait_running()
        await self.service._notification(
            "turn/completed", {"threadId": "thread-one", "turn": {"id": "turn-1", "status": "completed"}}
        )
        await self.service._notification("service/transportFailure", {"reason": "CODEX_NOTIFICATION_OVERFLOW"})
        assert self.service.active_task is not None
        await asyncio.wait_for(asyncio.shield(self.service.active_task), 2)
        self.assertEqual("FAILED", self.service.status()["phase"])
        self.assertEqual("CODEX_NOTIFICATION_OVERFLOW", self.service.status()["error"])

    async def test_close_retries_retained_process_before_closing_notification_io(self):
        process = Mock()
        process.close = AsyncMock(side_effect=[ServiceError("CODEX_NOTIFICATION_FLUSH_TIMEOUT", 503), None])
        self.service.process = process
        self.service.records["failed"] = {"phase": "RUNNING", "error": "STOP_UNCONFIRMED"}
        self.service.active_task = asyncio.create_task(asyncio.sleep(0))
        await self.service.active_task
        with self.assertRaisesRegex(ServiceError, "^CODEX_NOTIFICATION_FLUSH_TIMEOUT$"):
            await self.service.close()
        self.assertIs(self.service.process, process)
        self.assertFalse(self.service.io.closed)
        self.assertFalse(self.service.diagnostics.closed)
        await self.service.close()
        self.assertIsNone(self.service.process)
        self.assertTrue(self.service.io.closed)
        self.assertEqual(2, process.close.await_count)
        self.assertEqual("STOP_UNCONFIRMED", self.service.records["failed"]["error"])

    async def test_foreign_turn_usage_is_excluded_and_current_multi_request_usage_accumulates(self):
        self.factory.complete = False
        await self.service.start(self.payload())
        await self.wait_running()

        def usage(turn, total, last):
            return {
                "threadId": "thread-one",
                "turnId": turn,
                "tokenUsage": {
                    "total": {
                        "inputTokens": total,
                        "cachedInputTokens": 0,
                        "outputTokens": 0,
                        "reasoningOutputTokens": 0,
                    },
                    "last": {
                        "inputTokens": last,
                        "cachedInputTokens": 0,
                        "outputTokens": 0,
                        "reasoningOutputTokens": 0,
                    },
                },
            }

        await self.service._notification("thread/tokenUsage/updated", usage("old-turn", 99999, 1))
        await self.service._notification("thread/tokenUsage/updated", usage("turn-1", 35, 25))
        self.assertEqual(35, self.service.status()["usage"]["input_tokens"])
        await self.complete("turn-1")
        self.assertEqual(35, self.service.records["first"]["usage"]["input_tokens"])
