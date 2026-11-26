"""Economy waiting stops inference without completing Main or holding shared locks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from civ_agent.coordination_context import working_context
from civ_agent.economy_roles import TurnRoles
from tests.domain_fixtures import run_async
from tests.test_economy_roles import coordinator


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


def archive_messages():
    return [
        {
            "id": f"old{i}",
            "author": "military",
            "to": "gameplay",
            "text": "Historical notice " + "x" * 600,
            "createdTurn": 0,
        }
        for i in range(100)
    ]


@run_async
async def test_omitted_archived_notices_do_not_prevent_a_real_economy_wait():
    roles, jobs = waiting_roles()
    main = jobs["gameplay"]
    roles.war_state["messages"] = archive_messages()
    main.economy_seen = working_context(roles.context(), "gameplay")
    assert main.economy_seen["history"]["omitted"]["messages"] > 90
    result = await roles.economy_wait(main, {"requestIds": ["r1"]}, "wait")
    assert result["economyWait"]["state"] == "waiting"
    roles.arm_economy_wait(main, result)
    assert main.role_wait_pending.is_set()
    assert not any(m.get("acknowledged") for m in roles.war_state["messages"])


@run_async
async def test_new_question_displaces_archived_notices_and_attention_is_not_an_endless_gate():
    roles, jobs = waiting_roles()
    main = jobs["gameplay"]
    roles.war_state["messages"] = archive_messages()
    main.economy_seen = working_context(roles.context(), "gameplay")
    question = {
        "id": "new",
        "author": "economy",
        "to": "gameplay",
        "text": "Which city should receive the reinforcement?",
        "createdTurn": 4,
    }
    roles.war_state["messages"].append(question)
    result = await roles.economy_wait(main, {}, "first")
    assert result["economyWait"]["state"] == "attention"
    assert "did not suspend" in result["instruction"]
    assert not main.role_wait_pending.is_set()
    delivered = working_context(roles.context(), "gameplay")
    assert delivered["warState"]["messages"][0] == question
    main.economy_seen = delivered
    result = await roles.economy_wait(main, {}, "second")
    assert result["economyWait"]["state"] == "waiting"
    assert len(roles.war_state["messages"]) == 101
