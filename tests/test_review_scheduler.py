"""Review work overlaps gameplay while required advice gates only its owner."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_agent.orchestration.contracts import Scope, digest
from civ_agent.runtime.review_cycles import ReviewCycle, ReviewScheduler


def cycle(player, turn=0, **updates):
    from civ_agent.orchestration.contracts import Reference

    identity = digest([player, turn])
    return ReviewCycle(
        scope=Scope(game="game", player=player, epoch="g1", activation=identity),
        source_turn=turn,
        target_turn=0 if turn == 0 else turn + 1,
        kind="opening" if turn == 0 else "periodic",
        inputs=Reference(sha256=identity),
        **updates,
    )


def test_background_review_and_required_review_overlap_with_three_reserved_slots():
    async def run():
        scheduler = ReviewScheduler(4)
        started = {name: asyncio.Event() for name in ("p0", "p1", "p2")}
        release = {name: asyncio.Event() for name in started}

        def player(name):
            async def review(_):
                started[name].set()
                await release[name].wait()

            return SimpleNamespace(review=review)

        try:
            await scheduler.submit(player("p0"), cycle("p0"))
            await asyncio.wait_for(started["p0"].wait(), 1)
            await scheduler.submit(player("p1"), cycle("p1"))
            await scheduler.submit(player("p2"), cycle("p2"))
            await scheduler.prioritize("p2", 0)
            await asyncio.wait_for(started["p2"].wait(), 1)
            assert not started["p1"].is_set()
            release["p2"].set()
            await scheduler.tasks[("p2", 0)]
            assert not started["p1"].is_set()
            release["p0"].set()
            await asyncio.wait_for(started["p1"].wait(), 1)
        finally:
            await scheduler.close()

    asyncio.run(run())


def test_next_turn_waits_for_owned_review_and_checks_terminal_result():
    async def run():
        scheduler = ReviewScheduler(4)
        frozen = cycle("p0", 10)
        started, release = asyncio.Event(), asyncio.Event()
        service = SimpleNamespace(for_target=AsyncMock(return_value=frozen), require_target=AsyncMock())

        async def review(received):
            assert received == frozen
            started.set()
            await release.wait()

        player = SimpleNamespace(
            session=SimpleNamespace(player="p0"), root=SimpleNamespace(reviews=service), review=review
        )
        waiting = asyncio.create_task(scheduler.wait(player, 11))
        try:
            await asyncio.wait_for(started.wait(), 1)
            assert not waiting.done()
            service.require_target.assert_not_awaited()
            release.set()
            await asyncio.wait_for(waiting, 1)
            service.require_target.assert_awaited_once_with(11)
        finally:
            await scheduler.close()

    asyncio.run(run())


def test_uncommitted_snapshot_never_starts_model_and_bug_propagates():
    async def run():
        scheduler = ReviewScheduler(4)
        player = SimpleNamespace(review=AsyncMock(side_effect=RuntimeError("OWN_BUG")))
        try:
            await scheduler.submit(player, cycle("p0", 10, committed=False, source_handoff=7))
            assert not scheduler.tasks
            player.review.assert_not_awaited()
            await scheduler.submit(player, cycle("p0", 10))
            await asyncio.sleep(0)
            with pytest.raises(RuntimeError, match="OWN_BUG"):
                scheduler.raise_if_failed()
        finally:
            await scheduler.close()

    asyncio.run(run())
