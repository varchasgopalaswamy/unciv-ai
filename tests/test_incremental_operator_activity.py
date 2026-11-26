import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai_harness.step_persistence import InMemoryStepStore

from civ_agent.operator_views import OperatorPublisher
from civ_agent.orchestration.job_contracts import NativeJob, host_job_id
from civ_agent.runtime.state import RoleStatus
from civ_agent.step_store_notifications import CommittedStepStore
from tests.native_watch_fixtures import fixture, history, task
from tests.test_local_controller import config, seed


@asynccontextmanager
async def publisher(tmp_path):
    fixture(tmp_path)
    request = task(tmp_path)
    native = NativeJob(scope=request.scope, job=request.job, purpose=request.purpose)
    base = await history(
        tmp_path,
        request,
        run_id="opaque-run",
        conversation_id="opaque-conversation",
        metadata={"native_job_id": host_job_id(native), "game_turn": "2"},
    )
    steps = CommittedStepStore(base)
    state = seed(tmp_path, config())
    state.current.recent_roles = [
        RoleStatus(
            purpose="gameplay",
            job=native.job,
            phase="COMPLETED",
            run_id="opaque-run",
            native_job=native.model_dump(mode="json"),
            released=True,
        )
    ]
    player = SimpleNamespace(session=SimpleNamespace(player="p0"), steps=steps)
    view = OperatorPublisher(tmp_path, state, players={"Rome": player})
    try:
        yield view, steps
    finally:
        await view.close()
        await state.close()


def changed(snapshot, text, *, state="complete"):
    stamp = snapshot.timestamp + timedelta(seconds=1)
    return replace(
        snapshot,
        step_index=snapshot.step_index + 1,
        timestamp=stamp,
        state=state,
        messages=[*snapshot.messages, ModelResponse([TextPart(text)], run_id=snapshot.run_id, timestamp=stamp)],
    )


def test_unchanged_completed_run_reuses_render_and_late_interrupted_snapshot_refreshes(tmp_path, monkeypatch):
    async def run():
        async with publisher(tmp_path) as (view, steps):
            restore = AsyncMock(wraps=steps.wrapped.latest_snapshot)
            monkeypatch.setattr(steps.wrapped, "latest_snapshot", restore)
            first = await view._activity()
            assert await view._activity() == first
            assert restore.await_count == 1
            snapshot = await restore(run_id="opaque-run", include_interrupted=True)
            late = changed(snapshot, "Late committed evidence", state="interrupted")
            await steps.save_snapshot(late)
            second = await view._activity()
            assert any(item.text == "Late committed evidence" for _, item in second)
            assert await view._activity() == second
            expected = view._snapshot_activity(late, "opaque-run", "Rome", 2, "gameplay")
            assert second == sorted(expected, key=lambda entry: entry[1].sort_key)[-600:]
            assert view.performance.snapshot()["counts"]["activityCacheHits"] == 2
            assert len(view._run_index) == 1 and len(view._activity_cache) == 1

    asyncio.run(run())


def test_binding_is_revalidated_on_cache_hit_and_epoch_change_discards_old_entries(tmp_path, monkeypatch):
    async def run():
        async with publisher(tmp_path) as (view, steps):
            await view._activity()
            record = await steps.get_run(run_id="opaque-run")
            assert record is not None
            bad = replace(record, metadata={**record.metadata, "native_job_id": "other-job"})
            monkeypatch.setattr(steps.wrapped, "get_run", AsyncMock(return_value=bad))
            with pytest.raises(ValueError, match="OPERATOR_SDK_RUN_BINDING_CONFLICT"):
                await view._activity()
            view.state.current.epoch = "restored-epoch"
            with pytest.raises(ValueError):
                await view._activity()
            assert not view._activity_cache

    asyncio.run(run())


def test_cancelled_write_that_already_committed_cannot_leave_cached_activity(tmp_path, monkeypatch):
    async def run():
        async with publisher(tmp_path) as (view, steps):
            await view._activity()
            snapshot = await steps.latest_snapshot(run_id="opaque-run", include_interrupted=True)
            late = changed(snapshot, "Committed before cancellation")
            original = steps.wrapped.save_snapshot
            committed = asyncio.Event()
            release = asyncio.Event()

            async def hold_after_commit(value):
                await original(value)
                committed.set()
                await release.wait()

            monkeypatch.setattr(steps.wrapped, "save_snapshot", hold_after_commit)
            writing = asyncio.create_task(steps.save_snapshot(late))
            await committed.wait()
            assert steps.snapshot_token("opaque-run") is None
            writing.cancel()
            await asyncio.sleep(0)
            assert not writing.done()
            assert steps.snapshot_token("opaque-run") is None
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await writing
            assert steps.snapshot_token("opaque-run") is not None
            assert any(item.text == "Committed before cancellation" for _, item in await view._activity())

    asyncio.run(run())


def test_sqlite_cancel_before_commit_keeps_run_pending_until_worker_finishes(tmp_path, monkeypatch):
    from pydantic_ai_harness.step_persistence import SqliteStepStore

    async def run():
        async with publisher(tmp_path) as (view, steps):
            await view._activity()
            snapshot = await steps.latest_snapshot(run_id="opaque-run", include_interrupted=True)
            late = changed(snapshot, "Committed after cancellation")
            assert isinstance(steps.wrapped, SqliteStepStore)
            original = steps.wrapped._sync_save_snapshot
            started, release, finished = threading.Event(), threading.Event(), threading.Event()

            def delayed(*args):
                started.set()
                try:
                    if not release.wait(5):
                        raise TimeoutError("Test did not release the accepted SQLite write")
                    return original(*args)
                finally:
                    finished.set()

            monkeypatch.setattr(steps.wrapped, "_sync_save_snapshot", delayed)
            writing = asyncio.create_task(steps.save_snapshot(late))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                writing.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not writing.done()
                assert steps.snapshot_token("opaque-run") is None
                # An observer between cancellation and commit may see the old
                # snapshot, but must not retain it under a stable cache token.
                assert not any(item.text == "Committed after cancellation" for _, item in await view._activity())
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await writing
                assert await asyncio.to_thread(finished.wait, 2)
            assert steps.snapshot_token("opaque-run") is not None
            assert any(item.text == "Committed after cancellation" for _, item in await view._activity())
            assert await view._activity() == await view._activity()

    asyncio.run(run())


def test_notification_index_is_bounded_and_eviction_cannot_restore_an_old_token():
    async def run():
        from pydantic_ai_harness.step_persistence import RunRecord

        store = CommittedStepStore(InMemoryStepStore(), max_runs=2)
        original = store.snapshot_token("first")
        notices = []
        store.subscribe(lambda run_id: notices.append(run_id))
        store.subscribe(lambda _: (_ for _ in ()).throw(RuntimeError("observer failed")))
        for name in ("first", "second", "third"):
            await store.register_run(RunRecord(run_id=name, agent_name="test"))
        assert len(store._runs) == 2 and not store._pending
        assert store.snapshot_token("first") != original
        assert notices == ["first", "first", "second", "second", "third", "third"]
        with pytest.raises(ValueError):
            CommittedStepStore(InMemoryStepStore(), max_runs=0)

    asyncio.run(run())


def test_recovery_uses_only_new_bound_run_and_performance_is_local(tmp_path):
    async def run():
        import json

        from pydantic_ai_harness.step_persistence import RunRecord

        from civ_agent.orchestration.contracts import canonical

        async with publisher(tmp_path) as (view, steps):
            await view._activity()
            old = await steps.latest_snapshot(run_id="opaque-run", include_interrupted=True)
            assert old is not None
            native = NativeJob.model_validate(view.state.current.recent_roles[0].native_job)
            new = native.model_copy(
                update={"scope": native.scope.model_copy(update={"epoch": "g2"}), "job": "restored"}
            )
            await steps.register_run(
                RunRecord(
                    run_id="restored-run",
                    agent_name="player_role_v1",
                    conversation_id="restored-conversation",
                    metadata={
                        "scope": canonical(new.scope.model_dump(mode="json")),
                        "native_job_id": host_job_id(new),
                        "game_turn": "3",
                    },
                )
            )
            await steps.save_snapshot(
                replace(
                    old,
                    run_id="restored-run",
                    conversation_id="restored-conversation",
                    messages=[ModelResponse([TextPart("Restored source only")], run_id="restored-run")],
                )
            )
            view.state.current.epoch = "g2"
            view.state.current.recent_roles = [
                RoleStatus(
                    purpose="gameplay",
                    job=new.job,
                    phase="COMPLETED",
                    run_id="restored-run",
                    native_job=new.model_dump(mode="json"),
                    released=True,
                )
            ]
            activity = await view._activity()
            assert [item.text for _, item in activity] == ["Restored source only"]
            assert set(view._activity_cache) == {"restored-run"}
            await view.start()
            await asyncio.sleep(0.55)
            from civ_agent.operator_views import ViewGeneration

            view._publish_performance(
                ViewGeneration(
                    generation="f" * 32,
                    game=view.state.current.game,
                    epoch="g2",
                    started_at=0,
                    captured_at=1,
                )
            )
            metrics = json.loads((view.directory / "performance.json").read_text())
            assert metrics["epoch"] == "g2"
            assert metrics["publication"]["timings"]["eventLoopLag"]["retainedSamples"] >= 1
            assert "Restored source only" not in json.dumps(metrics)

    asyncio.run(run())


def test_store_without_commit_notifications_uses_safe_uncached_reads(tmp_path, monkeypatch):
    async def run():
        async with publisher(tmp_path) as (view, steps):
            restore = AsyncMock(wraps=steps.wrapped.latest_snapshot)
            monkeypatch.setattr(steps.wrapped, "latest_snapshot", restore)
            view.players["Rome"].steps = steps.wrapped
            first = await view._activity()
            assert await view._activity() == first
            assert restore.await_count == 2
            assert not view._activity_cache

    asyncio.run(run())


def test_run_index_and_render_cache_are_bounded_to_latest_owned_runs(tmp_path):
    async def run():
        from pydantic_ai_harness.step_persistence import RunRecord

        from civ_agent.orchestration.contracts import canonical

        async with publisher(tmp_path) as (view, original):
            snapshot = await original.latest_snapshot(run_id="opaque-run", include_interrupted=True)
            assert snapshot is not None
            native = NativeJob.model_validate(view.state.current.recent_roles[0].native_job)
            steps = CommittedStepStore(InMemoryStepStore())
            statuses = {}
            for number in range(40):
                run_id = f"owned-run-{number}"
                job = native.model_copy(update={"job": f"job-{number}"})
                await steps.register_run(
                    RunRecord(
                        run_id=run_id,
                        agent_name="player_role_v1",
                        conversation_id=run_id,
                        metadata={
                            "scope": canonical(job.scope.model_dump(mode="json")),
                            "native_job_id": host_job_id(job),
                            "game_turn": "2",
                        },
                    )
                )
                await steps.save_snapshot(
                    replace(
                        snapshot,
                        run_id=run_id,
                        conversation_id=run_id,
                        messages=[ModelResponse([TextPart(run_id)], run_id=run_id)],
                    )
                )
                statuses[f"p0:{job.job}"] = RoleStatus(
                    purpose="gameplay",
                    job=job.job,
                    phase="COMPLETED",
                    run_id=run_id,
                    native_job=job.model_dump(mode="json"),
                    released=True,
                )
            view.players["Rome"].steps = steps
            view.state.current.recent_roles = []
            view.state.current.native_jobs = statuses
            activity = await view._activity()
            assert {entry.text for _, entry in activity} == {f"owned-run-{n}" for n in range(8, 40)}
            assert len(view._run_index) == len(view._activity_cache) == 32
            assert await view._activity() == activity
            assert view.performance.snapshot()["counts"]["activityCacheHits"] == 32
            with pytest.raises(AttributeError):
                cast(Any, view._run_index[0]).turn = 999

    asyncio.run(run())


def test_public_sdk_persistence_and_continue_work_through_notification_wrapper(tmp_path):
    async def run():
        from pydantic_ai import Agent
        from pydantic_ai.models.function import FunctionModel
        from pydantic_ai_harness import StepPersistence
        from pydantic_ai_harness.step_persistence import SqliteStepStore, continue_run

        store = CommittedStepStore(SqliteStepStore(database=tmp_path / "steps.sqlite3"))
        agent = Agent(
            FunctionModel(lambda _messages, _info: ModelResponse([TextPart("Saved result")])),
            capabilities=[StepPersistence(store=store, agent_name="notification-test")],
        )
        await agent.run("Persist this run")
        runs = await store.list_runs()
        assert len(runs) == 1
        snapshot = await store.latest_snapshot(run_id=runs[0].run_id, include_interrupted=True)
        assert snapshot is not None
        assert snapshot.messages == await continue_run(store, run_id=runs[0].run_id)
        assert store.snapshot_token(runs[0].run_id) is not None
        assert await store.list_events(run_id=runs[0].run_id)

    asyncio.run(run())


def test_write_notifications_coalesce_without_increasing_publication_frequency(tmp_path, monkeypatch):
    async def run():
        from civ_agent import operator_views

        async with publisher(tmp_path) as (view, _steps):
            monkeypatch.setattr(operator_views, "_PUBLICATION_INTERVAL", 0.02)
            times = []

            async def publish():
                times.append(asyncio.get_running_loop().time())

            monkeypatch.setattr(view, "publish", publish)
            publishing = asyncio.create_task(view.run())
            try:
                for _ in range(30):
                    view.refresh.set()
                    await asyncio.sleep(0.001)
                await asyncio.sleep(0.03)
            finally:
                publishing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await publishing
            assert len(times) >= 2
            assert all(later - earlier >= 0.019 for earlier, later in zip(times, times[1:], strict=False))

    asyncio.run(run())
