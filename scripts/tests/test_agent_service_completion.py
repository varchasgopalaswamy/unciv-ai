"""Correlate native notifications with the acknowledged model turn and its own counters."""

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_service import AgentService
from test_agent_service import FakeCodex, FakeFactory


class ReplayCodex(FakeCodex):
    async def request(self, method, payload):
        if method == "turn/start" and self.factory.replay_before_response:
            # Resuming can produce asynchronous notifications from a preceding turn.
            # Its completed status must not satisfy this new turn/start request.
            await self.notification("turn/started", {"threadId": "thread-one", "turn": {"id": "old-turn", "status": "inProgress"}})
            await self.notification("turn/completed", {"threadId": "thread-one", "turn": {"id": "old-turn", "status": "completed"}})
        result = await super().request(method, payload)
        if method == "turn/start" and self.factory.burst:
            turn_id = result["turn"]["id"]
            await self.notification("thread/tokenUsage/updated", {
                "threadId": "thread-one", "turnId": turn_id, "tokenUsage": {
                    "total": {"inputTokens": 10 * self.factory.count + 25, "cachedInputTokens": 0, "outputTokens": 0, "reasoningOutputTokens": 0},
                    "last": {"inputTokens": 25, "cachedInputTokens": 0, "outputTokens": 0, "reasoningOutputTokens": 0}}})
            await self.notification("turn/completed", {"threadId": "thread-one", "turn": {"id": turn_id, "status": "completed"}})
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
        service = AgentService("Rome", self.root / "workspace", self.root / "service", "S" * 43,
                               "https://localhost:8765/mcp", None, process_factory=self.factory)
        self.factory.service = service
        return service

    async def asyncTearDown(self):
        await self.service.close()
        self.temporary.cleanup()

    def payload(self, activation="first", thread=None):
        return {"activationId": activation, "epoch": "epoch-one", "civilization": "Rome", "threadId": thread,
                "mcpUrl": "https://localhost:8765/mcp", "playerToken": "P" * 43, "prompt": "Play current host turn", "model": None,
                "timeoutSeconds": 20, "leaseSeconds": 10}

    async def wait_running(self):
        for _ in range(100):
            if self.service.status()["phase"] != "STARTING":
                break
            await asyncio.sleep(0.001)
        self.assertEqual("RUNNING", self.service.status()["phase"])

    async def complete(self, turn):
        await self.service._notification("turn/completed", {"threadId": "thread-one", "turn": {"id": turn, "status": "completed"}})
        await asyncio.wait_for(asyncio.shield(self.service.active_task), 2)
        self.assertEqual("COMPLETED", self.service.status()["phase"])

    async def test_replayed_completion_before_rpc_response_cannot_complete_new_turn(self):
        self.factory.complete = False
        self.factory.replay_before_response = True
        await self.service.start(self.payload())
        await self.wait_running()
        self.assertEqual("turn-1", self.service.turn_id)
        self.assertFalse(self.service.active_task.done())
        await self.complete("turn-1")
        self.assertEqual("turn-1", self.service.status()["turnId"])

    async def test_replayed_started_after_rpc_response_cannot_replace_bound_turn(self):
        self.factory.complete = False
        await self.service.start(self.payload())
        await self.wait_running()
        await self.service._notification("turn/started", {"threadId": "thread-one", "turn": {"id": "old-turn", "status": "inProgress"}})
        await self.service._notification("turn/completed", {"threadId": "thread-one", "turn": {"id": "old-turn", "status": "completed"}})
        await asyncio.sleep(0)
        self.assertEqual("turn-1", self.service.turn_id)
        self.assertEqual("RUNNING", self.service.status()["phase"])
        await self.complete("turn-1")

    async def test_reset_and_cumulative_native_counters_both_count_each_resumed_activation(self):
        expected_total = 0
        for index, (cumulative, amount) in enumerate(((False, 10), (False, 10), (True, 10), (False, 120))):
            self.factory.cumulative_totals = cumulative
            self.factory.input_tokens = amount
            await self.service.start(self.payload(str(index), None if index == 0 else "thread-one"))
            await asyncio.wait_for(asyncio.shield(self.service.active_task), 2)
            expected_total += amount
            self.assertEqual(amount, self.service.status()["usage"]["input_tokens"])
            self.assertEqual(expected_total, self.service.records[str(index)]["threadTotals"]["input_tokens"])
            await self.service.close()
            self.service = self.make_service()

    async def test_first_and_latest_early_updates_preserve_the_entire_new_turn_usage(self):
        self.factory.count = 10
        self.factory.cumulative_totals = True
        self.factory.complete = False
        self.factory.burst = True
        await self.service.start(self.payload())
        await asyncio.wait_for(asyncio.shield(self.service.active_task), 2)
        self.assertEqual("COMPLETED", self.service.status()["phase"])
        self.assertEqual(35, self.service.status()["usage"]["input_tokens"])

    async def test_foreign_turn_usage_is_excluded_and_current_multi_request_usage_accumulates(self):
        self.factory.complete = False
        await self.service.start(self.payload())
        await self.wait_running()
        def usage(turn, total, last):
            return {"threadId": "thread-one", "turnId": turn, "tokenUsage": {
                "total": {"inputTokens": total, "cachedInputTokens": 0, "outputTokens": 0, "reasoningOutputTokens": 0},
                "last": {"inputTokens": last, "cachedInputTokens": 0, "outputTokens": 0, "reasoningOutputTokens": 0}}}
        await self.service._notification("thread/tokenUsage/updated", usage("old-turn", 99999, 1))
        await self.service._notification("thread/tokenUsage/updated", usage("turn-1", 35, 25))
        self.assertEqual(35, self.service.status()["usage"]["input_tokens"])
        await self.complete("turn-1")
        self.assertEqual(35, self.service.records["first"]["threadTotals"]["input_tokens"])


if __name__ == "__main__":
    unittest.main()
