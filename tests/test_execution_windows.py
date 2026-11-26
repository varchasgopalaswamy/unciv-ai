"""Bounded scheduling does not replace observation or native effect authority."""

import asyncio

import pytest
from pydantic_ai import ModelRetry

from civ_agent.execution_windows import ExecutionWindowScheduler, WindowBinding, native_effect_started, release_focus
from civ_agent.orchestration.contracts import GatewayJob, PlayerRole, Scope
from civ_agent.orchestration.store import JournalConflict
from tests.domain_fixtures import run_async


def identity(job: PlayerRole = "military"):
    return GatewayJob(scope=Scope(game="game", player="p0", epoch="g1", activation="turn1"), job=job, purpose=job)


def binding(job: PlayerRole = "military"):
    return WindowBinding.create(identity(job), run_id="run1", parent_call_id="program1", code="player_turn()")


async def value(result=None):
    return result


@run_async
async def test_focus_keeps_fifo_turn_across_reads_and_releases_after_one_settled_mutation():
    scheduler = ExecutionWindowScheduler(game="game", player="p0")
    read, proceed = asyncio.Event(), asyncio.Event()
    order = []

    async def focused():
        async with scheduler.focused(binding()) as window:
            await scheduler.run(identity(), "read", lambda: value(order.append("read")))
            read.set()
            await proceed.wait()
            await scheduler.run(identity(), "move", lambda: value(order.append("move")), write=True)
            assert window.ended == "mutation_settled"
            with pytest.raises(ModelRetry, match="FOCUSED_WINDOW_ENDED"):
                await scheduler.run(identity(), "second-move", lambda: value(), write=True)

    first = asyncio.create_task(focused())
    await read.wait()
    second = asyncio.create_task(scheduler.run(identity("economy"), "worker", lambda: value(order.append("worker"))))
    await asyncio.sleep(0)
    third = asyncio.create_task(scheduler.run(identity("gameplay"), "main", lambda: value(order.append("main"))))
    await asyncio.sleep(0)
    assert order == ["read"]
    proceed.set()
    await asyncio.gather(first, second, third)
    assert order == ["read", "move", "worker", "main"]
    assert scheduler.owner is None and not scheduler.queue


@run_async
async def test_helper_only_program_never_acquires_and_binding_cannot_authorize_another_job():
    scheduler = ExecutionWindowScheduler(game="game", player="p0")
    async with scheduler.focused(binding()) as window:
        assert window.ticket is None and scheduler.owner is None
        with pytest.raises(JournalConflict, match="BINDING_CONFLICT"):
            await scheduler.run(identity("economy"), "forged", lambda: value(), write=True)
        assert window.ticket is None
    assert len(scheduler.events) == 1 and scheduler.events[0]["program_sha256"] == binding().program_sha256


@run_async
async def test_expiry_cannot_release_an_in_flight_call_and_old_window_never_reopens():
    scheduler = ExecutionWindowScheduler(game="game", player="p0")
    entered, settle = asyncio.Event(), asyncio.Event()

    async def held():
        entered.set()
        await settle.wait()

    async def focused():
        async with scheduler.focused(binding(), seconds=0.02) as window:
            await scheduler.run(identity(), "read", held)
            assert window.ended == "expired"
            with pytest.raises(ModelRetry, match="expired"):
                await scheduler.run(identity(), "late", lambda: value(), write=True)

    first = asyncio.create_task(focused())
    await entered.wait()
    next_writer = asyncio.create_task(scheduler.run(identity("economy"), "worker", lambda: value(), write=True))
    await asyncio.sleep(0.04)
    assert not next_writer.done() and scheduler.owner is not None
    settle.set()
    await asyncio.gather(first, next_writer)
    assert scheduler.owner is None


@run_async
async def test_caller_cancellation_keeps_accepted_write_until_receipt_and_bookkeeping_settle():
    scheduler = ExecutionWindowScheduler(game="game", player="p0")
    entered, settle = asyncio.Event(), asyncio.Event()
    order = []

    async def effect():
        entered.set()
        await settle.wait()
        order.append("receipt_saved")

    async def focused():
        async with scheduler.focused(binding()):
            await scheduler.run(identity(), "move", effect, write=True)

    first = asyncio.create_task(focused())
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    following = asyncio.create_task(
        scheduler.run(identity("economy"), "worker", lambda: value(order.append("next_writer")), write=True)
    )
    await asyncio.sleep(0)
    assert not following.done() and scheduler.owner is not None
    settle.set()
    await following
    assert order == ["receipt_saved", "next_writer"] and not scheduler.fenced


@run_async
async def test_unknown_write_fences_already_queued_work_until_exact_identity_recovers():
    scheduler = ExecutionWindowScheduler(game="game", player="p0")
    entered, fail = asyncio.Event(), asyncio.Event()

    async def unknown():
        native_effect_started()
        entered.set()
        await fail.wait()
        raise OSError("lost native response")

    first = asyncio.create_task(scheduler.run(identity(), "move", unknown, write=True))
    await entered.wait()
    following = asyncio.create_task(scheduler.run(identity("economy"), "worker", lambda: value(), write=True))
    await asyncio.sleep(0)
    fail.set()
    with pytest.raises(OSError):
        await first
    with pytest.raises(JournalConflict, match="RECONCILIATION_REQUIRED"):
        await following
    assert scheduler.fenced == {"move"}
    assert await scheduler.run(identity(), "move", lambda: value("original_receipt"), write=True) == "original_receipt"
    assert scheduler.fenced == {"move"}, "A successful handler alone proves no native settlement"
    scheduler.settled("move")
    assert not scheduler.fenced
    await scheduler.run(identity("economy"), "worker-new", lambda: value(), write=True)


@run_async
async def test_queue_timeout_and_waiter_cancellation_do_not_leak_a_turn():
    scheduler = ExecutionWindowScheduler(game="game", player="p0")
    entered, settle = asyncio.Event(), asyncio.Event()

    async def held():
        entered.set()
        await settle.wait()

    first = asyncio.create_task(scheduler.run(identity(), "held", held))
    await entered.wait()
    async with scheduler.focused(binding("economy"), queue_timeout=0.01):
        with pytest.raises(ModelRetry, match="QUEUE_TIMEOUT"):
            await scheduler.run(identity("economy"), "waiting", lambda: value())
    canceled = asyncio.create_task(scheduler.run(identity("gameplay"), "canceled", lambda: value()))
    await asyncio.sleep(0)
    canceled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await canceled
    settle.set()
    await first
    assert scheduler.owner is None and not scheduler.queue


@run_async
async def test_lifecycle_interrupts_waiters_and_suspension_never_regrants_old_focus():
    scheduler = ExecutionWindowScheduler(game="game", player="p0")
    async with scheduler.focused(binding()) as window:
        await scheduler.run(identity(), "read", lambda: value())
        scheduler.suspend("checkpoint")
        assert scheduler.owner is None and window.ended == "checkpoint"
        scheduler.resume()
        with pytest.raises(ModelRetry, match="checkpoint"):
            await scheduler.run(identity(), "stale", lambda: value(), write=True)
    async with scheduler.focused(binding()) as fresh:
        await scheduler.run(identity(), "fresh-read", lambda: value())
        release_focus("before_deferred_wait")
        assert fresh.ended == "before_deferred_wait" and scheduler.owner is None
    await scheduler.close()
    with pytest.raises(JournalConflict, match="CLOSED"):
        await scheduler.run(identity(), "after-close", lambda: value())


@run_async
async def test_call_cap_and_reentrant_command_phases_preserve_single_admission():
    scheduler = ExecutionWindowScheduler(game="game", player="p0")
    async with scheduler.focused(binding(), max_calls=2) as window:

        async def outer():
            return await scheduler.run(identity(), "outer", lambda: value("same_phase"), write=True)

        assert await scheduler.run(identity(), "outer", outer) == "same_phase"
        assert window.calls == 1 and window.ended is None
        with pytest.raises(ModelRetry, match="call_limit"):
            await scheduler.run(identity(), "too-many", lambda: value(), cost=2)
        assert window.calls == 1 and scheduler.owner is None


@run_async
async def test_closing_an_idle_focus_never_grants_queued_new_work():
    scheduler = ExecutionWindowScheduler(game="game", player="p0")
    entered, leave = asyncio.Event(), asyncio.Event()
    effects = []

    async def focused():
        async with scheduler.focused(binding()):
            await scheduler.run(identity(), "read", lambda: value())
            entered.set()
            await leave.wait()

    first = asyncio.create_task(focused())
    await entered.wait()
    queued = asyncio.create_task(
        scheduler.run(identity("economy"), "new-write", lambda: value(effects.append("new-effect")), write=True)
    )
    await asyncio.sleep(0)
    await scheduler.close()
    with pytest.raises(JournalConflict, match="CLOSED"):
        await queued
    leave.set()
    await first
    assert not effects and scheduler.owner is None
