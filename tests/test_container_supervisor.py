"""Monitoring contention must not interrupt independent games in a batch."""

import csv
import json
import sqlite3
import threading
from contextlib import closing

import pytest
from sqlalchemy.exc import OperationalError

from civ_agent import container_supervisor as supervisor
from civ_agent.game_config import GameConfig
from civ_agent.monitoring import MonitorStore, path_for
from civ_agent.tls import write_private, write_private_json


@pytest.fixture
def batch_state(tmp_path):
    manifest = {
        "format": 2,
        "parallel": 2,
        "runs": [{"id": name, "status": "pending"} for name in ("g0001", "g0002")],
    }
    write_private_json(tmp_path / "batch.json", manifest)
    for index, run in enumerate(manifest["runs"]):
        root = tmp_path / "games" / run["id"]
        write_private_json(root / "resolved.json", GameConfig(game={"master_seed": index}).resolved().model_dump())
        MonitorStore(path_for(root)).put("state", "result", {"status": "incomplete", "turn": 33})
    return tmp_path, manifest


def summary_rows(root):
    return json.loads((root / "results.json").read_text())["runs"]


def read_error(code):
    original = sqlite3.OperationalError("simulated database failure")
    original.sqlite_errorcode = code
    return OperationalError("SELECT payload FROM monitor_records", {}, original, hide_parameters=True)


@pytest.mark.parametrize("legacy_summary", [False, True])
def test_real_database_lock_retains_cached_result_and_other_games_keep_refreshing(batch_state, caplog, legacy_summary):
    root, manifest = batch_state
    supervisor.batch_summary(root, manifest)
    path = root / "results.json"
    previous = json.loads(path.read_text())
    if legacy_summary:
        # Existing paused batches have no per-result freshness fields.
        for row in previous["runs"]:
            del row["result_stale"], row["result_updated_at"]
        write_private_json(path, previous)
    cached_at = previous["runs"][0].get("result_updated_at", previous["updated_at"])
    game_path = path_for(root / "games/g0001")
    latest = {"status": "incomplete", "turn": 34}
    MonitorStore(game_path).put("state", "result", latest)
    other = {"status": "completed", "turn": 35}
    MonitorStore(path_for(root / "games/g0002")).put("state", "result", other)
    for run in manifest["runs"]:
        run["status"] = "running"

    with closing(sqlite3.connect(game_path)) as writer:
        # A rollback journal gives a deterministic real SQLITE_BUSY on reads.
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        try:
            for _ in range(2):
                supervisor.batch_summary(root, manifest)
                first, second = summary_rows(root)
                assert first["result"] == previous["runs"][0]["result"]
                assert first["result_stale"] is True and first["result_updated_at"] == cached_at
                assert first["status"] == "running"
                assert second["result"] == other and second["result_stale"] is False
                with (root / "results.csv").open() as stream:
                    csv_first, csv_second = list(csv.DictReader(stream))
                assert csv_first["result_stale"] == "True"
                assert float(csv_first["result_updated_at"]) == cached_at
                assert json.loads(csv_second["result"]) == other
            assert caplog.text.count("monitoring database is busy") == 1
            assert str(game_path) in caplog.text
        finally:
            writer.rollback()

    supervisor.batch_summary(root, manifest)
    first = summary_rows(root)[0]
    assert first["result"] == latest and first["result_stale"] is False
    assert first["result_updated_at"] > cached_at


@pytest.mark.parametrize("previous", [None, "{", "[]", '{"runs":[null]}'])
def test_initial_lock_without_usable_cached_summary_is_reported_as_unavailable(batch_state, monkeypatch, previous):
    root, manifest = batch_state
    if previous is not None:
        write_private(root / "results.json", previous.encode())
    original_read = supervisor.read_record

    def read(path, *args):
        if path == path_for(root / "games/g0001"):
            raise read_error(sqlite3.SQLITE_BUSY)
        return original_read(path, *args)

    monkeypatch.setattr(supervisor, "read_record", read)
    supervisor.batch_summary(root, manifest)
    first, second = summary_rows(root)
    assert first["result"] is None and first["result_updated_at"] is None
    assert first["result_stale"] is True
    assert second["result"] == {"status": "incomplete", "turn": 33}
    assert second["result_stale"] is False


@pytest.mark.parametrize(
    "code", [sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY_RECOVERY, sqlite3.SQLITE_LOCKED_SHAREDCACHE]
)
def test_primary_and_extended_lock_codes_defer_the_summary(batch_state, monkeypatch, code):
    root, manifest = batch_state

    def read(*_):
        raise read_error(code)

    monkeypatch.setattr(supervisor, "read_record", read)
    supervisor.batch_summary(root, manifest)
    assert all(row["result_stale"] and row["result"] is None for row in summary_rows(root))


@pytest.mark.parametrize("code", [sqlite3.SQLITE_ERROR, sqlite3.SQLITE_CORRUPT])
def test_nonlocking_database_failures_are_not_silently_treated_as_contention(batch_state, monkeypatch, code):
    root, manifest = batch_state
    failure = read_error(code)

    def read(*_):
        raise failure

    monkeypatch.setattr(supervisor, "read_record", read)
    with pytest.raises(OperationalError) as caught:
        supervisor.batch_summary(root, manifest)
    assert caught.value is failure
    assert not (root / "results.json").exists()


@pytest.mark.parametrize("operator_stop", [False, True])
def test_batch_survives_locks_at_start_during_play_and_on_final_refresh(batch_state, monkeypatch, operator_stop):
    root, _ = batch_state
    stop_event = threading.Event()
    finish_games = threading.Event()
    started = threading.Barrier(3)
    refreshes = []
    original_summary, original_read = supervisor.batch_summary, supervisor.read_record

    class Game:
        def __init__(self, directory, event):
            assert directory.parent == root / "games"
            assert event is stop_event

        def run(self):
            started.wait(timeout=5)
            if operator_stop:
                assert stop_event.wait(5)
                return "paused"
            assert finish_games.wait(5)
            assert not stop_event.is_set(), "Monitoring contention stopped gameplay"
            return "completed"

    def read(path, *args):
        # Read successfully once mid-run, then lock again through the final refresh.
        if len(refreshes) != 2 and path == path_for(root / "games/g0001"):
            raise read_error(sqlite3.SQLITE_BUSY)
        return original_read(path, *args)

    def refresh(directory, manifest):
        original_summary(directory, manifest)
        refreshes.append(summary_rows(directory))
        if len(refreshes) == 2:
            started.wait(timeout=5)
            assert not stop_event.is_set()
        if len(refreshes) == 3:
            assert not stop_event.is_set()
            if operator_stop:
                stop_event.set()
            else:
                finish_games.set()

    monkeypatch.setattr(supervisor, "GameRun", Game)
    monkeypatch.setattr(supervisor, "read_record", read)
    monkeypatch.setattr(supervisor, "batch_summary", refresh)
    monkeypatch.setattr(supervisor, "BATCH_SUMMARY_INTERVAL", 0)
    supervisor.run_batch(root, stop_event)
    expected = "paused" if operator_stop else "completed"
    assert {run["status"] for run in json.loads((root / "batch.json").read_text())["runs"]} == {expected}
    assert {run["status"] for run in refreshes[0]} == {"pending"}
    assert {run["status"] for run in refreshes[1]} == {"running"}
    assert refreshes[0][0]["result_stale"] and refreshes[1][0]["result_stale"]
    assert not refreshes[2][0]["result_stale"]
    assert {run["status"] for run in refreshes[-1]} == {expected}
    assert refreshes[-1][0]["result_stale"]
    assert refreshes[-1][0]["result"] == refreshes[2][0]["result"]
    assert refreshes[-1][0]["result_updated_at"] == refreshes[2][0]["result_updated_at"]


def test_pending_games_without_monitoring_database_do_not_create_one(tmp_path):
    manifest = {"format": 2, "parallel": 1, "runs": [{"id": "g0001", "status": "pending"}]}
    directory = tmp_path / "games/g0001"
    write_private_json(directory / "resolved.json", GameConfig().resolved().model_dump())
    supervisor.batch_summary(tmp_path, manifest)
    assert not path_for(directory).exists()
    assert summary_rows(tmp_path)[0]["result"] is None
    assert summary_rows(tmp_path)[0]["result_updated_at"] is None


def test_batch_survives_monitoring_file_creation_before_schema_initialization(tmp_path, monkeypatch):
    manifest = {
        "format": 2,
        "parallel": 2,
        "runs": [{"id": name, "status": "pending"} for name in ("g0001", "g0002")],
    }
    write_private_json(tmp_path / "batch.json", manifest)
    for run in manifest["runs"]:
        write_private_json(tmp_path / "games" / run["id"] / "resolved.json", GameConfig().resolved().model_dump())
    stop_event = threading.Event()
    created_files = threading.Barrier(3)
    initialize_schema = threading.Event()
    startup_rows = []
    original_summary = supervisor.batch_summary
    expected = {"status": "completed", "turn": 1}

    class Game:
        def __init__(self, directory, event):
            self.path = path_for(directory)
            assert event is stop_event

        def run(self):
            write_private(self.path, b"")
            created_files.wait(timeout=5)
            assert initialize_schema.wait(5)
            assert not stop_event.is_set(), "Monitoring initialization stopped gameplay"
            MonitorStore(self.path).put("state", "result", expected)
            return "completed"

    def refresh(root, current):
        starting = not startup_rows and all(run["status"] == "running" for run in current["runs"])
        if starting:
            created_files.wait(timeout=5)
        original_summary(root, current)
        if starting:
            startup_rows.extend(summary_rows(root))
            initialize_schema.set()

    monkeypatch.setattr(supervisor, "GameRun", Game)
    monkeypatch.setattr(supervisor, "batch_summary", refresh)
    supervisor.run_batch(tmp_path, stop_event)
    assert len(startup_rows) == 2
    assert all(row["result"] is None and row["result_updated_at"] is None for row in startup_rows)
    assert {run["status"] for run in json.loads((tmp_path / "batch.json").read_text())["runs"]} == {"completed"}
    assert all(row["result"] == expected and row["result_updated_at"] is not None for row in summary_rows(tmp_path))
