"""Exercise real gateway calls with deterministic app-server lifecycle events."""

import asyncio
import json
from unittest.mock import patch

from civ_agent.agent_service import ServiceError
from tests.test_agent_service import AgentServiceTestCase, FakeCodex, FakeFactory, StartupHost
from tests.test_trade_gateway import HoldingHost


class NegotiationHost(StartupHost, HoldingHost):
    pass


class NegotiationCodex(FakeCodex):
    async def request(self, method, payload):
        if method == "turn/interrupt":
            self.factory.calls.append((method, payload))
            if self.factory.complete_during_interrupt:
                await self.notification(
                    "turn/completed",
                    {"threadId": self.thread_id, "turn": {"id": payload["turnId"], "status": "completed"}},
                )
                raise ServiceError("CODEX_REQUEST_FAILED")
            if self.factory.interrupt_error:
                raise ServiceError("TEST_INTERRUPT_FAILED")
            if self.factory.acknowledge_interrupt:
                await self.notification(
                    "turn/completed",
                    {"threadId": self.thread_id, "turn": {"id": payload["turnId"], "status": "interrupted"}},
                )
            return {}
        if method == "turn/start":
            # Thread totals increase across continuations, not independent jobs.
            self.factory.input_tokens = 10 * (self.factory.count + 1)
        return await super().request(method, payload)


class NegotiationFactory(FakeFactory):
    def __init__(self):
        super().__init__()
        self.complete = False
        self.acknowledge_interrupt = True
        self.interrupt_error = False
        self.complete_during_interrupt = False
        self.host = NegotiationHost(self)

    def __call__(self, executable, workspace, mcp_url, ca_file, notification):
        if "economy" in workspace.name:
            return super().__call__(executable, workspace, mcp_url, ca_file, notification)
        process = NegotiationCodex(self, workspace, notification)
        self.instances.append(process)
        return process

    def connection(self, *args, **kwargs):
        return self.host


class NegotiationSuspensionTests(AgentServiceTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.models = NegotiationFactory()
        self.factory = self.models
        self.models.service = self.service
        self.service.process_factory = self.models
        self.service.connection_factory = self.models.connection

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.001)

    async def start_game(self):
        await self.service.start(self.payload(model="game-model", reasoningEffort="high"))
        await self.until(lambda: self.service.status()["phase"] == "RUNNING")
        return self.service.gateway.jobs[self.models.tokens[0]]

    async def offer(self, job):
        return await self.service.gateway.invoke(
            job,
            "trade_offer",
            {
                "partner": "Greece",
                "give": [{"type": "Gold", "name": "Gold", "amount": 1, "duration": -1}],
                "receive": [],
                "intent": {"reason": "Offer a goodwill gift"},
            },
            "offer",
        )

    async def suspended(self):
        await self.until(lambda: self.service.status().get("negotiationWait") == {"state": "waiting"})
        assert self.models.count == 1
        assert not self.models.instances[0].closed
        assert not self.service.gateway.dispatch.locked()
        assert self.service.status()["phase"] == "RUNNING"

    async def resumed(self):
        await self.until(lambda: self.models.count == 2 and self.service.turn_id == "turn-2")
        assert "negotiationWait" not in self.service.status()
        starts = [args for name, args in self.models.calls if name == "turn/start"]
        assert len(starts) == 2 and self.models.thread_count == 1
        assert starts[0]["threadId"] == starts[1]["threadId"]
        assert starts[1]["model"] == "game-model" and starts[1]["effort"] == "high"
        assert self.service.status()["usage"]["input_tokens"] == 20  # cumulative, not 10 + 20
        return json.loads(starts[1]["input"][0]["text"].split("\n", 1)[1])["toolResults"]

    async def complete_model(self):
        await self.models.instances[0].notification(
            "turn/completed",
            {"threadId": "thread-one", "turn": {"id": self.service.turn_id, "status": "completed"}},
        )

    async def test_counteroffer_resumes_same_conversation_without_resubmission(self):
        job = await self.start_game()
        pending = asyncio.create_task(self.offer(job))
        await self.suspended()
        usage = self.service.status()["usage"]
        # Cancelling the original HTTP waiter must not cancel the accepted offer.
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        assert job.pending_calls and not self.models.host.settled.is_set()
        assert self.service.status()["usage"] == usage
        assert self.models.count == 1
        # The communication agent remains free to inspect and accept counterterms.
        host, gateway = self.models.host, self.service.gateway
        host.trade.update(author="Greece", partner="Rome", version=2)
        chat = await gateway.open("chat", "communication", job.epoch, host, {})
        await gateway.invoke(chat, "trade_read", {"proposalId": "d1"}, "read")
        await gateway.invoke(
            chat,
            "trade_decide",
            {"proposalId": "d1", "decision": "accept", "intent": {"reason": "Accept the revised gift"}},
            "accept",
        )
        results = await self.resumed()
        assert len(results) == 1 and results[0]["tool"] == "trade_offer"
        result = results[0]["result"]
        assert result["trade"]["status"] == "ENACTED" and result["trade"]["version"] == 2
        assert result["outcome"]["action"] == "ENACT_TRADE" and "stateUpdate" in result
        assert result["stateUpdate"]["kind"] == "full"
        assert "mode" not in result["trade"]
        assert [row["op"] for row in host.writes] == ["trade_offer", "trade_decide"]
        await gateway.invoke(job, "player_command", {"kind": "END_TURN"}, "end")
        status = await self.terminal()
        assert status["phase"] == "COMPLETED" and status["completionReason"] == "native_handoff"
        assert self.service.completion == "interrupted" and status["negotiationWaitSeconds"] > 0
        assert [name for name, _ in self.models.calls].count("turn/interrupt") == 2
        assert not job.pending_calls and not job.negotiation_calls
        path = self.service.history.path(job.id) / "events.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines()]
        assert sum(row["data"].get("source") == "service_negotiation" for row in events) == 1

    async def closed_negotiation(self, outcome):
        job = await self.start_game()
        offer = asyncio.create_task(self.offer(job))
        # A second accepted call in the same parallel batch also needs its result.
        read = asyncio.create_task(self.service.gateway.invoke(job, "player_turn", {}, "parallel-read"))
        await self.suspended()
        rejected = await self.service.gateway.invoke(job, "player_command", {"kind": "END_TURN"}, "late")
        assert rejected == {"error": "GAMEPLAY_SUSPENDED", "applied": False}
        assert not self.models.host.executed
        self.models.host.trade.update(status=outcome)
        self.models.host.active = False
        self.models.host.settled.set()
        offer_result, read_result = await asyncio.gather(offer, read)
        results = await self.resumed()
        assert [(row["tool"], row["result"]) for row in results] == [
            ("trade_offer", offer_result),
            ("player_turn", read_result),
        ]
        assert offer_result["trade"]["status"] == outcome and not offer_result["outcome"]

    async def test_expiry_delivers_all_parallel_call_results(self):
        await self.closed_negotiation("EXPIRED")

    async def test_decline_delivers_all_parallel_call_results(self):
        await self.closed_negotiation("DECLINED")

    async def test_completion_racing_interrupt_is_a_confirmed_pause(self):
        self.models.complete_during_interrupt = True
        await self.closed_negotiation("EXPIRED")

    async def test_completed_sibling_action_and_its_state_are_not_lost(self):
        job = await self.start_game()
        gateway = self.service.gateway
        arguments = {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}}
        # This call could finish inside a JS batch before the offer starts.
        await gateway.invoke(job, "unit_details", {"target": "unit1"}, "inspect")
        moved = await gateway.invoke(job, "player_command", arguments, "move")
        assert "error" not in moved, moved
        assert job.completed_actions and not job.pending_calls
        # Give the host the new position; the continuation must include it even
        # though the gateway previously delivered an overview to the code cell.
        self.models.host.view["map"]["units"][0]["position"] = arguments["destination"]
        await gateway.invoke(job, "player_turn", {}, "position")
        pending = asyncio.create_task(self.offer(job))
        await self.suspended()
        self.models.host.trade["status"] = "EXPIRED"
        self.models.host.active = False
        self.models.host.settled.set()
        await pending
        results = await self.resumed()
        turn = [args for name, args in self.models.calls if name == "turn/start"][-1]
        continuation = json.loads(turn["input"][0]["text"].split("\n", 1)[1])
        assert len(continuation["completedActions"]) == 1
        action = continuation["completedActions"][0]
        assert action["tool"] == "player_command" and action["arguments"] == arguments
        assert action["result"]["kind"] == "MOVE_UNIT" and action["result"]["done"]
        assert "revision" not in action["result"] and "requestId" not in action["result"]
        overview = results[0]["result"]["stateUpdate"]["overview"]
        assert overview["units"][0]["position"] == arguments["destination"]
        assert len(self.models.host.executed) == 1 and not job.completed_actions

    async def test_command_blocked_by_incoming_negotiation_requires_a_new_decision(self):
        job = await self.start_game()
        self.models.host.active = True
        gateway = self.service.gateway
        pending = asyncio.create_task(gateway.invoke(job, "player_command", {"kind": "END_TURN"}, "old-end"))
        await self.suspended()
        self.models.host.active = False
        self.models.host.settled.set()
        assert (await pending)["error"] == "OBSERVATION_REQUIRED"
        results = await self.resumed()
        assert results[0]["result"]["applied"] is False and not self.models.host.executed
        await gateway.invoke(job, "player_turn", {}, "refresh")
        await gateway.invoke(job, "player_command", {"kind": "END_TURN"}, "new-end")
        await self.complete_model()
        assert (await self.terminal())["completionReason"] == "native_handoff"
        assert len(self.models.host.executed) == 1

    async def test_operator_stop_cancels_wait_and_does_not_resume(self):
        job = await self.start_game()
        pending = asyncio.create_task(self.offer(job))
        await self.suspended()
        status = await self.service.interrupt(job.id)
        with self.assertRaises(asyncio.CancelledError):
            await pending
        assert status["phase"] == "INTERRUPTED" and status["error"] == "INTERRUPTED"
        assert not job.pending_calls and not job.negotiation_calls and job.wait_started is None
        assert self.models.count == 1 and self.models.instances[0].closed
        assert len(self.models.host.writes) == 1

    async def test_incoming_negotiation_suspends_both_unfinished_roles(self):
        models = self.models

        class WorkingEconomy(FakeCodex):
            passes = 0

            async def request(self, method, payload):
                if method == "turn/start":
                    self.passes += 1
                    return {"turn": {"id": "economy-pass-" + str(self.passes)}}
                return await super().request(method, payload)

        def process_factory(executable, workspace, mcp_url, ca_file, notification):
            if "economy" in workspace.name:
                return WorkingEconomy(models, workspace, notification)
            return models(executable, workspace, mcp_url, ca_file, notification)

        self.service.process_factory = process_factory
        main = await self.start_game()
        runtime = self.service.economy_runtime
        assert runtime is not None
        await self.until(lambda: runtime.turn_id == "economy-pass-1")
        economy = runtime.job
        self.models.host.active = True
        await self.suspended()
        await self.until(lambda: self.service.records[economy.id].get("negotiationWait") == {"state": "waiting"})
        assert self.service.turn_roles is not None
        assert self.service.turn_roles.ready == {"gameplay": False, "economy": False}
        assert runtime.turn_id == "economy-pass-1" and not economy.model_stopped.is_set()
        self.models.host.active = False
        self.models.host.settled.set()
        await self.until(lambda: self.models.count == 2 and runtime.turn_id == "economy-pass-2")
        assert not main.negotiation_pending.is_set() and not economy.negotiation_pending.is_set()
        assert not self.models.host.executed
        await self.service.interrupt(main.id)
        assert main.model_stopped.is_set() and economy.model_stopped.is_set()

    async def test_lost_lease_during_suspension_is_a_failure(self):
        job = await self.start_game()
        pending = asyncio.create_task(self.offer(job))
        await self.suspended()
        self.service.last_heartbeat = 0
        status = await self.terminal()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        assert status["phase"] == "FAILED" and status["error"] == "HEARTBEAT_EXPIRED"
        assert self.models.count == 1 and not job.pending_calls

    async def test_disconnect_while_suspended_stops_without_waiting_for_trade_deadline(self):
        job = await self.start_game()
        pending = asyncio.create_task(self.offer(job))
        await self.suspended()
        await self.models.instances[0].notification("service/disconnected", {})
        status = await self.terminal()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        assert status["phase"] == "FAILED" and status["error"] == "CODEX_DISCONNECTED"
        assert self.models.count == 1

    async def test_missing_interrupt_acknowledgement_stops_the_job(self):
        self.models.acknowledge_interrupt = False
        job = await self.start_game()
        with patch("civ_agent.agent_service.MODEL_INTERRUPT_TIMEOUT", 0.02):
            pending = asyncio.create_task(self.offer(job))
            status = await self.terminal()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        assert status["phase"] == "FAILED" and status["error"] == "NEGOTIATION_SUSPEND_TIMEOUT"
        assert self.models.count == 1

    async def test_interrupt_rpc_failure_does_not_restart_or_duplicate_offer(self):
        self.models.interrupt_error = True
        job = await self.start_game()
        pending = asyncio.create_task(self.offer(job))
        status = await self.terminal()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        assert status["phase"] == "FAILED" and status["error"] == "TEST_INTERRUPT_FAILED"
        assert self.models.count == 1 and len(self.models.host.writes) == 1

    async def test_model_finishing_before_host_offer_reply_still_waits_for_outcome(self):
        job = await self.start_game()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.models.host.tool

        async def delayed(name, arguments):
            if name == "player_communication" and arguments["op"] == "trade_offer":
                entered.set()
                await release.wait()
            return await original(name, arguments)

        with patch.object(self.models.host, "tool", side_effect=delayed):
            pending = asyncio.create_task(self.offer(job))
            await entered.wait()
            await self.complete_model()
            assert self.service.active_task is not None and not self.service.active_task.done()
            release.set()
            await self.suspended()
            assert not any(name == "turn/interrupt" for name, _ in self.models.calls)
            self.models.host.trade.update(status="EXPIRED")
            self.models.host.active = False
            self.models.host.settled.set()
            await pending
            await self.resumed()

    async def test_native_ai_offer_does_not_suspend_gameplay(self):
        job = await self.start_game()
        self.models.host.trade["mode"] = "NATIVE_AI"
        result = await self.offer(job)
        assert result["trade"]["status"] == "PENDING"
        assert not job.negotiation_pending.is_set() and "negotiationWait" not in self.service.status()
        assert not any(name == "turn/interrupt" for name, _ in self.models.calls)
