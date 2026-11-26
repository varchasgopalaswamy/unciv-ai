"""Review permits renew before Main starts, without renewing capacity-queued jobs."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.orchestration.contracts import ModelSelection, Reference, ReviewRequest, Scope
from civ_agent.orchestration.job_contracts import NativeJob, NativeReviewBinding
from civ_agent.runtime import player as module
from civ_agent.runtime.player import LocalPlayer, Permit, PermitState
from civ_agent.runtime.review_cycles import ReviewCycle


def player_fixture():
    player: Any = object.__new__(LocalPlayer)
    player.root = SimpleNamespace(
        store=SimpleNamespace(close=Mock()),
        gateway=SimpleNamespace(routing=None),
        close=AsyncMock(),
    )
    player.clients = SimpleNamespace(close=AsyncMock())
    player.state = SimpleNamespace(role=AsyncMock())
    player._heartbeat_task = None
    player.tasks = set()
    player.permits = {}
    player.parent = player.ticket = None
    return player


def review_job(name):
    scope = Scope(game="game", player="p0", epoch="e1", activation=name)
    return NativeJob(
        scope=scope,
        job=name,
        purpose="review",
        review=NativeReviewBinding(cycle_id="a" * 64, source_turn=0, target_turn=0),
    )


def test_opening_review_starts_heartbeat_owner_before_main_admission(tmp_path, monkeypatch):
    async def run():
        player = player_fixture()
        player.review_generation = "generation"
        cycle = ReviewCycle(
            scope=review_job("a" * 64).scope,
            source_turn=0,
            target_turn=0,
            kind="opening",
            inputs=Reference(sha256="a" * 64),
        )
        assert cycle.inputs is not None
        request = ReviewRequest(
            scope=review_job("b" * 64).scope,
            inputs=cycle.inputs,
            selection=ModelSelection(model="test"),
            source_turn=0,
            target_turn=0,
            review_cycle_id=cycle.id,
        )
        player.root.reviews = SimpleNamespace(begin=AsyncMock(return_value=request))

        async def prepare(*_args, **_kwargs):
            assert player._heartbeat_task is not None
            assert player.parent is None and player.ticket is None
            assert not next(iter(player.permits.values())).admitted
            raise RuntimeError("STOP_AFTER_REVIEW_HEARTBEAT_START")

        monkeypatch.setattr(module, "prepare_review", prepare)
        with pytest.raises(RuntimeError, match="STOP_AFTER_REVIEW_HEARTBEAT_START"):
            await player.review(cycle)
        heartbeat = player._heartbeat_task
        assert heartbeat is not None and heartbeat in player.tasks
        await player.close()
        assert heartbeat.cancelled()
        player.clients.close.assert_awaited_once()
        player.root.close.assert_awaited_once()

    asyncio.run(run())


def test_first_review_renews_only_after_admission_and_close_joins_heartbeat(monkeypatch):
    async def run():
        player = player_fixture()
        ticks: asyncio.Queue[None] = asyncio.Queue()
        allow_claim, renewed = asyncio.Event(), asyncio.Event()
        sleep = asyncio.sleep
        clock = SimpleNamespace(now=0)

        async def tick(_delay):
            await ticks.get()

        async def claim(_job):
            await allow_claim.wait()
            return SimpleNamespace(granted=True)

        async def heartbeat(_job):
            renewed.set()
            return SimpleNamespace(granted=True)

        monkeypatch.setattr(module.asyncio, "sleep", tick)
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: 0))
        player.admission = SimpleNamespace(
            claim=claim,
            report=AsyncMock(return_value=SimpleNamespace(granted=True)),
            heartbeat=AsyncMock(side_effect=heartbeat),
        )
        queued = Permit(review_job("queued"), "test")
        released = Permit(review_job("released"), "test", lifecycle=PermitState.RELEASED)
        player.permits = {"queued": queued, "released": released}
        player._ensure_heartbeats()
        computing = asyncio.create_task(player._compute(queued, True))
        ticks.put_nowait(None)
        await sleep(0)
        player.admission.heartbeat.assert_not_awaited()
        assert not queued.admitted
        allow_claim.set()
        await computing
        assert queued.admitted and queued.computing and player.parent is None
        clock.now = module.JOB_HEARTBEAT_INTERVAL
        ticks.put_nowait(None)
        await asyncio.wait_for(renewed.wait(), timeout=5)
        player.admission.heartbeat.assert_awaited_once_with(queued.job)
        heartbeat_task = player._heartbeat_task
        await player.close()
        assert heartbeat_task.cancelled()
        assert player.admission.heartbeat.await_count == 1

    asyncio.run(run())


def test_heartbeat_and_release_transitions_are_serialized(monkeypatch):
    async def run():
        player = player_fixture()
        committed, ack = asyncio.Event(), asyncio.Event()

        async def release(_report):
            committed.set()
            await ack.wait()
            return SimpleNamespace(granted=True)

        player.admission = SimpleNamespace(release=AsyncMock(side_effect=release))
        permit = Permit(review_job("review"), "test", lifecycle=PermitState.WAITING)
        player.permits = {"review": permit}
        task = asyncio.create_task(player._report(permit, "COMPLETED", release=True, complete=True))
        await committed.wait()
        assert permit.releasing and permit.lock.locked()
        ack.set()
        await task
        assert permit.released and not player.permits
        assert player.state.role.await_args.args[0].released

    asyncio.run(run())


def test_review_definite_native_limit_keeps_original_reason_without_releasing_unknown_job(tmp_path, monkeypatch):
    async def run():
        player = player_fixture()
        player.review_generation = "generation"
        player.model_limiter = None
        player.root.store.root = tmp_path
        player.steps = SqliteStepStore(database=tmp_path / "steps.sqlite3")
        player.session = SimpleNamespace(settings=SimpleNamespace(scripted=True))
        cycle = ReviewCycle(
            scope=review_job("a" * 64).scope,
            source_turn=0,
            target_turn=0,
            kind="opening",
            inputs=Reference(sha256="a" * 64),
        )
        assert cycle.inputs is not None
        request = ReviewRequest(
            scope=review_job("b" * 64).scope,
            inputs=cycle.inputs,
            selection=ModelSelection(model="scripted-v1"),
            source_turn=0,
            target_turn=0,
            review_cycle_id=cycle.id,
        )
        player.root.reviews = SimpleNamespace(begin=AsyncMock(return_value=request))
        player.admission = SimpleNamespace(
            claim=AsyncMock(return_value=SimpleNamespace(granted=False, reason="MODEL_INVOCATION_LIMIT")),
            release=AsyncMock(side_effect=AssertionError("Unadmitted review has no native lease to release")),
        )
        monkeypatch.setattr(
            module,
            "prepare_review",
            AsyncMock(
                return_value=SimpleNamespace(
                    model_dump=lambda: {"instructions": "Frozen review.", "user": "Current view."},
                )
            ),
        )
        try:
            with pytest.raises(RuntimeError, match="^MODEL_INVOCATION_LIMIT$"):
                await player.review(cycle)
            assert request.scope.activation not in player.permits
            assert player.state.role.await_args.args[0].released
            player.admission.release.assert_not_awaited()
        finally:
            await player.close()

    asyncio.run(run())
