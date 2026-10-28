"""Economy waiting stops inference without completing Main or holding shared locks."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from civ_agent.economy_roles import TurnRoles
from tests.test_agent_service import AgentServiceTestCase, FakeCodex, FakeFactory
from tests.test_economy_roles import coordinator, run_async
from tests.test_negotiation_suspension import NegotiationFactory


def waiting_roles():
    roles, jobs = coordinator()
    roles.service.gateway = SimpleNamespace(check=Mock(), role_dispatch=asyncio.Lock())
    roles.service.failure = None
    for job in jobs.values():
        job.negotiation_pending = asyncio.Event()
    roles.requests = [{"id": "r1", "status": "pending", "priority": "routine"}]
    return roles, jobs


@run_async
async def test_registration_is_durable_before_arm_and_never_completes_main():
    roles, jobs = waiting_roles()
    main = jobs["gameplay"]
    result = await roles.economy_wait(main, {"requestIds": ["r1"]}, "wait1")
    assert result["economyWait"]["state"] == "waiting"
    assert not main.role_wait_pending.is_set()
    assert roles.record["roles"]["economyWait"]["id"] == "wait1"
    roles.arm_economy_wait(main, result)
    assert main.role_wait_pending.is_set() and not roles.ready["gameplay"]
    assert not main.role_finished.is_set()
    assert (await roles.tool(main, "finish_role", {}))["error"] == "ROLE_WAIT_ACTIVE"
    assert (await roles.economy_wait(main, {}, "wait2"))["error"] == "ROLE_WAIT_ACTIVE"


@pytest.mark.parametrize("status", ["completed", "deferred", "rejected"])
@run_async
async def test_resolved_requests_return_immediately_without_waking_economy(status):
    roles, jobs = waiting_roles()
    roles.requests[0]["status"] = status
    result = await roles.economy_wait(jobs["gameplay"], {"requestIds": ["r1"]}, "wait1")
    assert result["economyWait"]["state"] == "resolved"
    assert roles.economy_wait_record is None
    assert not jobs["gameplay"].role_wait_pending.is_set()


@run_async
async def test_idle_economy_does_not_promote_pending_routine_requests():
    roles, jobs = waiting_roles()
    roles.ready["economy"] = True
    assert (await roles.economy_wait(jobs["gameplay"], {}, "idle"))["economyWait"]["state"] == "idle"
    result = await roles.economy_wait(jobs["gameplay"], {"requestIds": ["r1"]}, "routine")
    assert result["economyWait"]["state"] == "not_running"
    assert roles.ready["economy"] and roles.requests[0]["priority"] == "routine"
    assert (await roles.economy_wait(jobs["gameplay"], {"requestIds": ["missing"]}, "bad"))[
        "error"
    ] == "UNKNOWN_ECONOMY_REQUEST"
    assert (await roles.economy_wait(jobs["economy"], {}, "other"))["error"] == "MAIN_PLAYER_DECISION_REQUIRED"


@run_async
async def test_completion_before_subscription_and_immediate_reopen_are_not_lost():
    roles, jobs = waiting_roles()
    main = jobs["gameplay"]
    result = await roles.economy_wait(main, {}, "wait1")
    await roles.tool(jobs["economy"], "finish_role", {})
    roles.resume("economy", "Another request")
    await roles.save()
    roles.arm_economy_wait(main, result)
    assert await roles.wait_for_economy(main) == "pass_finished"
    await roles.finish_economy_wait(main, "pass_finished")
    roles.arm_economy_wait(main, result, replayed=True)
    assert not main.role_wait_pending.is_set()
    assert not roles.ready["gameplay"]


@pytest.mark.parametrize("wake", ["attention", "resolved", "timeout", "negotiation", "turn_ended"])
@run_async
async def test_wait_releases_locks_and_wakes_for_required_conditions(wake):
    roles, jobs = waiting_roles()
    main = jobs["gameplay"]
    result = await roles.economy_wait(main, {"requestIds": ["r1"]}, "wait1")
    roles.arm_economy_wait(main, result)
    waiting = asyncio.create_task(roles.wait_for_economy(main))
    await asyncio.sleep(0)
    assert not roles.lock.locked() and not roles.service.gateway.role_dispatch.locked()
    if wake == "attention":
        roles.resume("gameplay", "Please clarify the request")
    elif wake == "resolved":
        roles.requests[0]["status"] = "deferred"
    elif wake == "timeout":
        roles.economy_wait_deadline = 0
    elif wake == "negotiation":
        main.negotiation_pending.set()
    else:
        main.gameplay_ended.set()
    await roles.save()
    assert await asyncio.wait_for(waiting, 1) == wake
    await roles.finish_economy_wait(main, wake)
    assert not main.role_wait_pending.is_set()
    assert not roles.ready["gameplay"]


@run_async
async def test_wait_registration_is_not_restored_on_new_activation():
    roles, jobs = waiting_roles()
    await roles.economy_wait(jobs["gameplay"], {}, "wait1")
    replacement = TurnRoles(roles.service, {**roles.record, "activationId": "new-attempt"})
    assert replacement.economy_wait_record is None
    assert replacement.ready["gameplay"] is False


@run_async
async def test_question_arriving_before_registration_is_delivered_without_suspending():
    roles, jobs = waiting_roles()
    message = {"id": "m1", "to": "gameplay", "text": "Which city needs the purchase?"}
    roles.war_state["messages"] = [message]
    result = await roles.economy_wait(jobs["gameplay"], {}, "wait1")
    assert result["economyWait"]["state"] == "attention" and roles.economy_wait_record is None
    jobs["gameplay"].economy_seen = {"warState": {"messages": [message]}}
    result = await roles.economy_wait(jobs["gameplay"], {}, "wait2")
    assert result["economyWait"]["state"] == "waiting"


class WorkingEconomy(FakeCodex):
    async def request(self, method, payload):
        if method == "turn/start":
            return {"turn": {"id": "working-economy"}}
        return await super().request(method, payload)


class WaitingFactory(NegotiationFactory):
    def __call__(self, executable, workspace, mcp_url, ca_file, notification):
        if "economy" in workspace.name:
            return WorkingEconomy(self, workspace, notification)
        return super().__call__(executable, workspace, mcp_url, ca_file, notification)


class EconomyWaitLifecycleTests(AgentServiceTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.models = WaitingFactory()
        self.factory: FakeFactory = self.models
        self.models.service = self.service
        self.service.process_factory = self.models
        self.service.connection_factory = self.models.connection

    async def until(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.001)

    async def start_waiting(self):
        await self.service.start(self.payload(model="game-model", reasoningEffort="high"))
        await self.until(lambda: self.service.status()["phase"] == "RUNNING")
        await self.until(
            lambda: self.service.economy_runtime and self.service.economy_runtime.turn_id == "working-economy"
        )
        main = self.service.gateway.jobs[self.models.tokens[0]]
        result = await self.service.gateway.invoke(main, "economy_wait", {}, "wait1")
        assert result["economyWait"]["state"] == "waiting"
        await self.until(lambda: self.service.status().get("economyWait") == {"state": "waiting"})
        return main, result

    async def test_suspends_and_resumes_same_thread_without_handoff_or_polling(self):
        main, receipt = await self.start_waiting()
        roles, gateway = self.service.turn_roles, self.service.gateway
        assert roles is not None
        assert not roles.ready["gameplay"] and not main.role_finished.is_set()
        assert self.models.count == 1
        assert not gateway.role_dispatch.locked() and gateway.calls_in_flight == 0
        assert self.service.records[main.id]["roleState"] == "waiting_economy"
        assert (await gateway.invoke(main, "player_turn", {}, "late"))["error"] == "GAMEPLAY_SUSPENDED"
        assert self.service.economy_runtime is not None
        await gateway.invoke(self.service.economy_runtime.job, "finish_role", {}, "economy-done")
        await self.until(lambda: self.models.count == 2 and self.service.turn_id == "turn-2")
        starts = [args for method, args in self.models.calls if method == "turn/start"]
        assert len(starts) == 2 and starts[0]["threadId"] == starts[1]["threadId"]
        assert starts[1]["model"] == "game-model" and starts[1]["effort"] == "high"
        resumed = json.loads(starts[1]["input"][0]["text"].split("\n", 1)[1])
        assert resumed["economyWait"]["state"] == "pass_finished"
        assert resumed["currentGame"]["economyCoordination"]["rolesFinished"]["economy"]
        assert not self.models.host.executed and not roles.ready["gameplay"]
        replay = await gateway.invoke(main, "economy_wait", {}, "wait1")
        assert replay == receipt and not main.role_wait_pending.is_set()
        assert self.service.status()["roleWaitSeconds"] > 0
        assert self.service.status()["negotiationWaitSeconds"] == 0
        await gateway.invoke(main, "finish_role", {}, "main-done")
        assert (await self.terminal())["phase"] == "COMPLETED"

    async def test_pause_cancels_wait_and_drains_calls_without_resuming(self):
        main, _ = await self.start_waiting()
        await self.service.interrupt(main.id)
        assert self.service.status()["phase"] == "INTERRUPTED"
        assert not main.role_wait_pending.is_set() and not main.role_wait_calls
        assert not main.pending_calls and self.models.count == 1
        assert not self.models.host.executed

    async def test_timeout_resumes_main_without_completing_economy(self):
        main, _ = await self.start_waiting()
        roles = self.service.turn_roles
        assert roles is not None
        roles.economy_wait_deadline = 0
        await roles.save()
        await self.until(lambda: self.models.count == 2)
        starts = [args for method, args in self.models.calls if method == "turn/start"]
        resumed = json.loads(starts[1]["input"][0]["text"].split("\n", 1)[1])
        assert resumed["economyWait"]["state"] == "timeout"
        assert not roles.ready["gameplay"] and not roles.ready["economy"]
        await self.service.interrupt(main.id)

    async def test_economy_question_wakes_main_and_does_not_leave_a_second_resume(self):
        main, _ = await self.start_waiting()
        roles, gateway = self.service.turn_roles, self.service.gateway
        assert roles is not None and self.service.economy_runtime is not None
        await gateway.invoke(
            self.service.economy_runtime.job,
            "role_message",
            {"to": "gameplay", "text": "Should this city's gold be reserved for defense?"},
            "question",
        )
        await self.until(lambda: self.models.count == 2 and self.service.turn_id == "turn-2")
        starts = [args for method, args in self.models.calls if method == "turn/start"]
        resumed = json.loads(starts[1]["input"][0]["text"].split("\n", 1)[1])
        assert resumed["economyWait"]["state"] == "attention"
        assert "reserved for defense" in json.dumps(resumed["currentGame"])
        assert not roles.resumes["gameplay"].is_set() and "gameplay" not in roles.messages
        assert not roles.ready["gameplay"] and not roles.ready["economy"]
        await self.service.interrupt(main.id)

    async def test_incoming_negotiation_settles_before_waiting_main_resumes(self):
        main, _ = await self.start_waiting()
        host = self.models.host
        host.active = True
        self.service.gateway.suspend_negotiation(main)
        await self.until(lambda: self.service.status().get("negotiationWait") == {"state": "waiting"})
        assert self.models.count == 1
        host.active = False
        host.settled.set()
        await self.until(lambda: self.models.count == 2 and self.service.turn_id == "turn-2")
        starts = [args for method, args in self.models.calls if method == "turn/start"]
        resumed = json.loads(starts[1]["input"][0]["text"].split("\n", 1)[1])
        assert resumed["economyWait"]["state"] == "negotiation"
        assert "negotiation" in resumed and "currentGame" in resumed
        assert not main.role_wait_pending.is_set() and not main.negotiation_pending.is_set()
        await self.service.interrupt(main.id)

    async def test_parallel_finish_is_rejected_while_durable_wait_is_registered(self):
        main, _ = await self.start_waiting()
        # An accepted code-mode sibling can already be queued before invoke is fenced.
        result = await self.service.gateway.call(main, "finish_role", {}, "queued-finish")
        assert result["error"] == "ROLE_WAIT_ACTIVE"
        assert self.service.turn_roles is not None and not self.service.turn_roles.ready["gameplay"]
        assert not self.models.host.executed
        await self.service.interrupt(main.id)

    async def test_terminal_economy_failure_stops_waiting_without_another_model_pass(self):
        main, _ = await self.start_waiting()
        assert self.service.turn_roles is not None
        self.service.failure = "ECONOMY_ROLE_FAILED"
        await self.service.turn_roles.save()
        status = await self.terminal()
        assert status["phase"] == "FAILED" and status["error"] == "ECONOMY_ROLE_FAILED"
        assert self.models.count == 1 and not main.role_wait_pending.is_set()

    async def test_restart_discards_wait_registration_and_does_not_count_downtime(self):
        main, _ = await self.start_waiting()
        await self.service.interrupt(main.id)
        record = self.service.records[main.id]
        record.update(
            phase="RUNNING",
            roleState="waiting_economy",
            economyWait={"state": "waiting"},
            roleWaitStartedAtMs=int((time.time() - 3600) * 1000),
            roleWaitSeconds=2,
        )
        await self.service._save(record)
        await self.service.close()
        self.service = self.make_service()
        restored = self.service.status(main.id)
        assert restored["phase"] == "FAILED" and restored["error"] == "SERVICE_RESTARTED"
        assert restored["roleWaitSeconds"] == 2 and restored["roleWaitIncomplete"]
        assert "economyWait" not in restored and "roleWaitStartedAtMs" not in restored
        assert not self.service.gateway.jobs and self.service.turn_roles is None
