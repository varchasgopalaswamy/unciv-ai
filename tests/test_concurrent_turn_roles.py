"""Event-driven role overlap, late work, reopening, and joined cancellation."""

import asyncio
from collections import Counter
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from civ_agent.orchestration.contracts import TurnTicket
from civ_agent.orchestration.job_contracts import NativeJob
from civ_agent.runtime.player import AttemptContext, LocalPlayer, Permit
from tests.test_local_agents import request


def scheduler(*, pending=("gameplay", "economy", "military")):
    roles = SimpleNamespace(
        ready={role: role not in pending for role in ("gameplay", "economy", "military")},
        changed=asyncio.Event(),
    )
    player: Any = object.__new__(LocalPlayer)
    player.root = SimpleNamespace(prepare_role=AsyncMock(), context=lambda: ({}, roles))
    player.economy_lock = asyncio.Lock()
    req = request()
    permit = Permit(NativeJob(scope=req.scope, job=req.job, purpose="gameplay"), "test")
    context = AttemptContext(TurnTicket(scope=req.scope, turn=0, handoff=0, index=0), permit)
    return player, roles, context


def test_all_three_roles_overlap_and_join_after_their_runs_settle():
    async def run():
        player, roles, context = scheduler()
        started, release, settling = asyncio.Event(), asyncio.Event(), asyncio.Event()
        active = set()

        async def role(purpose, _context):
            active.add(purpose)
            if len(active) == 3:
                started.set()
            await release.wait()
            roles.ready[purpose] = True
            roles.changed.set()
            if purpose == "military":
                await settling.wait()
            active.remove(purpose)

        player.run_role = role
        task = asyncio.create_task(player.run_roles(context))
        await asyncio.wait_for(started.wait(), 1)
        assert active == {"gameplay", "economy", "military"}
        player.root.prepare_role.assert_awaited_once()
        release.set()
        await asyncio.sleep(0)
        assert not task.done()
        settling.set()
        await asyncio.wait_for(task, 1)
        assert not active

    asyncio.run(run())


def test_sleeping_economy_wakes_twice_while_main_is_still_running():
    async def run():
        player, roles, context = scheduler(pending=("gameplay",))
        main_started, main_done = asyncio.Event(), asyncio.Event()
        economy_started, economy_done = asyncio.Queue(), asyncio.Queue()
        release_economy = asyncio.Queue()
        calls = Counter()

        async def role(purpose, _context):
            calls[purpose] += 1
            if purpose == "gameplay":
                main_started.set()
                await main_done.wait()
            else:
                assert purpose == "economy"
                economy_started.put_nowait(calls[purpose])
                await release_economy.get()
            roles.ready[purpose] = True
            if purpose == "economy":
                economy_done.put_nowait(calls[purpose])

        player.run_role = role
        task = asyncio.create_task(player.run_roles(context))
        await asyncio.wait_for(main_started.wait(), 1)
        assert calls == {"gameplay": 1}
        for number in (1, 2):
            roles.ready["economy"] = False
            roles.changed.set()
            assert await asyncio.wait_for(economy_started.get(), 1) == number
            assert not main_done.is_set()
            release_economy.put_nowait(None)
            assert await asyncio.wait_for(economy_done.get(), 1) == number
        main_done.set()
        await asyncio.wait_for(task, 1)
        assert calls == {"gameplay": 1, "economy": 2}

    asyncio.run(run())


def test_reopened_main_waits_for_its_old_pass_to_settle_while_military_continues():
    async def run():
        player, roles, context = scheduler(pending=("gameplay", "military"))
        main_finished, reopened, settle_main, followup = (asyncio.Event() for _ in range(4))
        calls = Counter()

        async def role(purpose, _context):
            calls[purpose] += 1
            if purpose == "gameplay" and calls[purpose] == 1:
                roles.ready[purpose] = True
                main_finished.set()
                await settle_main.wait()
                return
            if purpose == "military":
                await main_finished.wait()
                roles.ready["gameplay"] = False
                roles.changed.set()
                reopened.set()
                await followup.wait()
            else:
                assert not roles.ready["military"]
                followup.set()
            roles.ready[purpose] = True

        player.run_role = role
        task = asyncio.create_task(player.run_roles(context))
        await asyncio.wait_for(reopened.wait(), 1)
        await asyncio.sleep(0)
        assert calls["gameplay"] == 1 and not followup.is_set()
        settle_main.set()
        await asyncio.wait_for(task, 1)
        assert calls == {"gameplay": 2, "military": 1}

    asyncio.run(run())


def test_inline_economy_wait_joins_existing_economy_instead_of_starting_another_run():
    async def run():
        player, roles, context = scheduler(pending=("gameplay", "economy"))
        economy_started, main_waiting = asyncio.Event(), asyncio.Event()
        roles.jobs = {"gameplay": object()}
        roles.wait_for_economy = AsyncMock(return_value="resolved")
        roles.finish_economy_wait = AsyncMock(return_value={"state": "resolved"})
        player._compute = AsyncMock()
        calls = Counter()
        waits = SimpleNamespace(approvals={}, calls=[SimpleNamespace(tool_name="economy_wait", tool_call_id="wait")])

        async def role(purpose, _context):
            if roles.ready[purpose]:
                return
            calls[purpose] += 1
            if purpose == "economy":
                economy_started.set()
                await main_waiting.wait()
            else:
                await economy_started.wait()
                main_waiting.set()
                result = await player._wait(request(), waits, context.parent, context)
                assert result.calls["wait"] == {"state": "resolved"}
            roles.ready[purpose] = True

        player._role = role
        await asyncio.wait_for(player.run_roles(context), 1)
        assert calls == {"gameplay": 1, "economy": 1}
        assert not player.economy_lock.locked()
        assert player._compute.await_count == 2

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["role_error", "operator_stop"])
def test_failure_or_stop_cancels_and_joins_every_active_role(failure):
    async def run():
        player, _roles, context = scheduler()
        started, fail = asyncio.Event(), asyncio.Event()
        active, joined = set(), set()

        async def role(purpose, _context):
            active.add(purpose)
            if len(active) == 3:
                started.set()
            try:
                if purpose == "military" and failure == "role_error":
                    await fail.wait()
                    raise RuntimeError("INJECTED_ROLE_FAILURE")
                await asyncio.Event().wait()
            finally:
                joined.add(purpose)

        player.run_role = role
        task = asyncio.create_task(player.run_roles(context))
        await asyncio.wait_for(started.wait(), 1)
        if failure == "role_error":
            fail.set()
            with pytest.raises(ExceptionGroup) as raised:
                await task
            assert "INJECTED_ROLE_FAILURE" in repr(raised.value)
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert joined == active == {"gameplay", "economy", "military"}

    asyncio.run(run())
