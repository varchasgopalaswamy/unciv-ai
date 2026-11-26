"""Passive observations preserve source bytes and retain valid publications."""

import asyncio
import hashlib
import sqlite3
import threading
import time
import tracemalloc
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter

from civ_agent.operator_state import published_game
from civ_agent.operator_views import ViewGeneration, passive_root
from civ_agent.orchestration.history_exports import copy_native_database
from civ_agent.tls import private_directory, write_private_json
from civ_agent.watch_state import WatchReader, WatchSnapshot
from tests.native_watch_fixtures import fixture


@pytest.mark.parametrize("cancel", [False, True])
def test_durable_publication_keeps_loop_responsive_and_settles_before_cancellation(tmp_path, monkeypatch, cancel):
    from civ_agent import operator_views
    from civ_agent.operator_views import OperatorPublisher
    from tests.test_local_controller import config, seed

    async def run():
        state = seed(tmp_path, config())
        publisher = OperatorPublisher(tmp_path, state)
        publisher.reader = SimpleNamespace(
            read=lambda: WatchSnapshot({"epoch": state.current.epoch}, {}, (), (), (), None), host_cache={}
        )
        publisher._activity = AsyncMock(return_value=[])
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = operator_views.write_private_json

        def slow_write(path, value):
            if path.name == "view.json":
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(2):
                    raise AssertionError("Publication blocked the event loop while its disk write waited.")
            original(path, value)

        monkeypatch.setattr(operator_views, "write_private_json", slow_write)
        publishing = asyncio.create_task(publisher.publish())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert not publishing.done()
            if cancel:
                publishing.cancel()
                await asyncio.sleep(0)
                assert not publishing.done(), "An accepted publication must settle before cancellation returns."
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await publishing
            else:
                await publishing
            generation = ViewGeneration.model_validate_json((publisher.directory / "current.json").read_bytes())
            directory = publisher.directory / generation.generation
            assert ViewGeneration.model_validate_json((directory / "generation.json").read_bytes()) == generation
            assert (directory / "view.json").is_file()
        finally:
            release.set()
            await asyncio.gather(publishing, return_exceptions=True)
            await publisher.close()
            await state.close()

    asyncio.run(run())


def files(root):
    return {
        str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode)
        for p in root.rglob("*")
        if p.is_file()
    }


def test_stopped_capture_includes_committed_wal_without_source_sidecars(tmp_path):
    root = private_directory(tmp_path / "source")
    directory = private_directory(root / "runner")
    source = directory / "monitoring.sqlite3"
    db = sqlite3.connect(source)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE evidence(value)")
    db.execute("INSERT INTO evidence VALUES (1)")
    db.commit()
    source.chmod(0o600)
    # Preserve an uncheckpointed committed WAL after the writer has stopped.
    frozen = private_directory(tmp_path / "frozen")
    frozen_runner = private_directory(frozen / "runner")
    for p in directory.iterdir():
        (frozen_runner / p.name).write_bytes(p.read_bytes())
        (frozen_runner / p.name).chmod(0o600)
    db.close()
    before = files(frozen)
    with passive_root(frozen) as captured:
        with sqlite3.connect(captured / "runner/monitoring.sqlite3") as reader:
            assert reader.execute("SELECT count(*) FROM evidence").fetchone() == (1,)
    assert files(frozen) == before


def test_live_backup_participates_in_wal_even_when_started_without_wal(tmp_path):
    source, target = tmp_path / "source.sqlite3", tmp_path / "capture.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE evidence(value)")
        db.execute("INSERT INTO evidence VALUES (1)")
        db.commit()
        source.chmod(0o600)
        copy_native_database(source, target)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT value FROM evidence").fetchall() == [(1,)]


@pytest.mark.parametrize("live", [False, True])
def test_database_capture_memory_is_independent_of_file_size(tmp_path, live):
    from civ_agent.operator_views import capture_tree

    root = private_directory(tmp_path / "source")
    directory = private_directory(root / "runner")
    source = directory / "monitoring.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE evidence(value)")
        db.execute("INSERT INTO evidence VALUES (zeroblob(24 * 1024 * 1024))")
    source.chmod(0o600)
    destination = private_directory(tmp_path / "capture")
    tracemalloc.start()
    try:
        sources = capture_tree(root, destination, live=live)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024, "Capturing history must not load the database into Python memory."
    target = destination / "runner/monitoring.sqlite3"
    with target.open("rb") as stream:
        assert sources["runner/monitoring.sqlite3"] == hashlib.file_digest(stream, "sha256").hexdigest()
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT length(value) FROM evidence").fetchone() == (24 * 1024 * 1024,)
    assert target.stat().st_mode & 0o777 == 0o600


def test_stopped_capture_rechecks_database_after_copying_wal(tmp_path, monkeypatch):
    from civ_agent import operator_views

    root = private_directory(tmp_path / "source")
    directory = private_directory(root / "runner")
    source = directory / "monitoring.sqlite3"
    source.write_bytes(b"before")
    source.chmod(0o600)
    wal = source.with_name(source.name + "-wal")
    wal.write_bytes(b"wal")
    copyfile = operator_views.shutil.copyfile

    def changing_copy(original, target):
        result = copyfile(original, target)
        if original == wal:
            source.write_bytes(b"changed during WAL capture")
        return result

    monkeypatch.setattr(operator_views.shutil, "copyfile", changing_copy)
    with pytest.raises(ValueError, match="OPERATOR_SOURCE_CHANGED"):
        operator_views.capture_tree(root, private_directory(tmp_path / "capture"))


def test_stale_published_dashboard_never_opens_private_databases(tmp_path, monkeypatch):
    fixture(tmp_path)
    current = published_game(tmp_path)
    directory = private_directory(tmp_path / "operator-views")
    generation = ViewGeneration(
        generation="a" * 32,
        game=current.authority.game,
        epoch=current.authority.epoch,
        started_at=time.time() - 20,
        captured_at=time.time() - 11,
    )
    payload = private_directory(directory / generation.generation)
    view = WatchSnapshot({"epoch": current.authority.epoch}, {}, (), (), (), None)
    write_private_json(payload / "view.json", TypeAdapter(WatchSnapshot).dump_python(view, mode="json"))
    write_private_json(directory / "current.json", generation.model_dump(mode="json"))
    monkeypatch.setattr(
        "civ_agent.watch_state.native_host_rows", lambda *_args: (_ for _ in ()).throw(AssertionError("source reader"))
    )
    before = files(tmp_path)
    assert WatchReader(tmp_path).read().errors == ("Operator view is stale",)
    assert files(tmp_path) == before


def test_runtime_publishes_exact_opaque_sdk_runs_without_full_history_capture(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from civ_agent.operator_views import OperatorPublisher
    from civ_agent.orchestration.job_contracts import NativeJob, host_job_id
    from civ_agent.runtime.state import RoleStatus
    from tests.native_watch_fixtures import history, task
    from tests.test_local_controller import config, seed

    async def run():
        fixture(tmp_path)
        request = task(tmp_path)
        native = NativeJob(scope=request.scope, job=request.job, purpose=request.purpose)
        steps = await history(
            tmp_path,
            request,
            run_id="opaque-run",
            conversation_id="opaque-conversation",
            metadata={"native_job_id": host_job_id(native), "game_turn": "2"},
        )
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
        publisher = OperatorPublisher(
            tmp_path, state, players={"Rome": SimpleNamespace(session=SimpleNamespace(player="p0"), steps=steps)}
        )
        render = publisher._snapshot_activity
        worker_threads = []

        def rendered(*args):
            worker_threads.append(threading.get_ident())
            return render(*args)

        monkeypatch.setattr(publisher, "_snapshot_activity", rendered)
        try:
            activity = await publisher._activity()
            assert any(item.text == "Rome is expanding." and item.turn == 2 for _, item in activity)
            assert len(activity) <= 600
            assert worker_threads and threading.get_ident() not in worker_threads
        finally:
            await publisher.close()
            await state.close()

    asyncio.run(run())


@pytest.mark.parametrize("name", ["literal-%n-%Q", "with spaces", "ordinary"])
def test_supervisor_log_properties_use_shared_systemd_path_escaping(tmp_path, monkeypatch, name):
    from types import SimpleNamespace

    from civ_agent import container_support

    root = private_directory(tmp_path / name)
    commands = []

    def capture(arguments, **_options):
        commands.append(arguments)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(container_support, "require_restart_systemd", lambda: None)
    monkeypatch.setattr(container_support, "unit_state", lambda _unit: {"LoadState": "not-found"})
    monkeypatch.setattr(container_support, "command", capture)
    container_support.launch_background(root, "game")
    properties = [item for item in commands[-1] if item.startswith("--property=Standard")]
    expected = container_support.systemd_path(root / "supervisor.log")
    assert properties == ["--property=StandardOutput=append:" + expected, "--property=StandardError=append:" + expected]
