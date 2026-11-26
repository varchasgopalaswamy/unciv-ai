"""Native atomic result publication must not disturb independent batch supervision."""

import csv
import json
import os
import sqlite3
import threading
from contextlib import closing

import pytest

from civ_agent import container_supervisor as supervisor
from civ_agent.container_support import ContainerError
from civ_agent.game_config import GameConfig
from civ_agent.monitoring import path_for
from civ_agent.tls import write_private_json
from tests.test_native_status import host_publication, native_files, publish_host


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
        native_files(root, identifier=run["id"])
        host_publication(root, match_id="engine-" + run["id"])
    return tmp_path, manifest


def summary_rows(root):
    return json.loads((root / "results.json").read_text())["runs"]


def test_batch_native_host_results_keep_row_freshness_and_csv_fields(batch_state):
    root, manifest = batch_state
    first_path = root / "games/g0001/native-runtime/result.json"
    os.utime(first_path, (1234.5, 1234.5))
    supervisor.batch_summary(root, manifest)
    first, second = summary_rows(root)
    assert first["result"]["result"]["winner"] == "Rome" and first["result_stale"] is False
    assert first["runtime_status"]["game"] == "g0001"
    assert first["runtime_result"]["turn"] == 33
    assert first["result_updated_at"] == 1234.5
    assert second["result"]["matchId"] == "engine-g0002"
    # Summary refresh time never masquerades as a new native result observation.
    supervisor.batch_summary(root, manifest)
    assert summary_rows(root)[0]["result_updated_at"] == 1234.5
    latest = {**first["result"], "elapsedMs": 110000}
    publish_host(root / "games/g0001", latest, updated_ms=2345750)
    os.utime(first_path, (2345.75, 2345.75))
    manifest["runs"][0]["status"] = "completed"
    supervisor.batch_summary(root, manifest)
    first = summary_rows(root)[0]
    assert first["result"] == latest and first["result_updated_at"] == 2345.75
    assert first["status"] == "completed" and first["result_stale"] is False
    with (root / "results.csv").open() as stream:
        csv_first = next(csv.DictReader(stream))
    assert csv_first["result_stale"] == "False"
    assert float(csv_first["result_updated_at"]) == 2345.75
    assert json.loads(csv_first["result"]) == latest
    assert json.loads(csv_first["players"]) == first["players"]


def test_locked_native_host_result_does_not_reuse_a_cached_batch_summary(batch_state):
    root, manifest = batch_state
    supervisor.batch_summary(root, manifest)
    previous = (root / "results.json").read_bytes()
    with closing(sqlite3.connect(path_for(root / "games/g0001"))) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(ContainerError, match="Cannot read native host result"):
                supervisor.batch_summary(root, manifest)
            assert (root / "results.json").read_bytes() == previous
        finally:
            writer.rollback()
    supervisor.batch_summary(root, manifest)
    assert all(row["result_stale"] is False for row in summary_rows(root))


@pytest.mark.parametrize("filename", ["status", "result"])
def test_corrupt_native_publication_fails_instead_of_reusing_cached_summary(batch_state, filename):
    root, manifest = batch_state
    supervisor.batch_summary(root, manifest)
    previous = (root / "results.json").read_bytes()
    (root / f"games/g0001/native-runtime/{filename}.json").write_text("{")
    with pytest.raises(ContainerError, match="Invalid native runtime projection"):
        supervisor.batch_summary(root, manifest)
    assert (root / "results.json").read_bytes() == previous


@pytest.mark.parametrize("operator_stop", [False, True])
def test_batch_supervision_preserves_parallel_games_and_final_refresh(batch_state, monkeypatch, operator_stop):
    root, _ = batch_state
    stop_event = threading.Event()
    finish_games = threading.Event()
    started = threading.Barrier(3)
    refreshes = []
    original_summary = supervisor.batch_summary

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
            assert not stop_event.is_set()
            return "completed"

    def refresh(directory, manifest):
        original_summary(directory, manifest)
        refreshes.append(summary_rows(directory))
        if len(refreshes) == 2:
            started.wait(timeout=5)
            if operator_stop:
                stop_event.set()
            else:
                finish_games.set()

    monkeypatch.setattr(supervisor, "GameRun", Game)
    monkeypatch.setattr(supervisor, "batch_summary", refresh)
    supervisor.run_batch(root, stop_event)
    expected = "paused" if operator_stop else "completed"
    assert {run["status"] for run in json.loads((root / "batch.json").read_text())["runs"]} == {expected}
    assert {run["status"] for run in refreshes[0]} == {"pending"}
    assert {run["status"] for run in refreshes[1]} == {"running"}
    assert {run["status"] for run in refreshes[-1]} == {expected}
    assert all(not row["result_stale"] for refresh in refreshes for row in refresh)


def test_pending_games_without_native_publications_create_no_runtime_store(tmp_path):
    manifest = {"format": 2, "parallel": 1, "runs": [{"id": "g0001", "status": "pending"}]}
    directory = tmp_path / "games/g0001"
    write_private_json(directory / "resolved.json", GameConfig().resolved().model_dump())
    supervisor.batch_summary(tmp_path, manifest)
    assert not (directory / "native-runtime").exists()
    assert summary_rows(tmp_path)[0]["result"] is None
    assert summary_rows(tmp_path)[0]["result_updated_at"] is None
    assert summary_rows(tmp_path)[0]["result_stale"] is False


def test_batch_survives_native_publication_after_initial_pending_refresh(tmp_path, monkeypatch):
    manifest = {
        "format": 2,
        "parallel": 2,
        "runs": [{"id": name, "status": "pending"} for name in ("g0001", "g0002")],
    }
    write_private_json(tmp_path / "batch.json", manifest)
    for run in manifest["runs"]:
        write_private_json(tmp_path / "games" / run["id"] / "resolved.json", GameConfig().resolved().model_dump())
    stop_event = threading.Event()
    started = threading.Barrier(3)
    publish = threading.Event()
    startup_rows = []
    original_summary = supervisor.batch_summary

    class Game:
        def __init__(self, directory, event):
            self.directory = directory
            assert event is stop_event

        def run(self):
            started.wait(timeout=5)
            assert publish.wait(5)
            assert not stop_event.is_set()
            native_files(self.directory, identifier=self.directory.name, turn=35)
            host_publication(self.directory, match_id="engine-" + self.directory.name)
            return "completed"

    def refresh(root, current):
        starting = not startup_rows and all(run["status"] == "running" for run in current["runs"])
        if starting:
            started.wait(timeout=5)
        original_summary(root, current)
        if starting:
            startup_rows.extend(summary_rows(root))
            publish.set()

    monkeypatch.setattr(supervisor, "GameRun", Game)
    monkeypatch.setattr(supervisor, "batch_summary", refresh)
    supervisor.run_batch(tmp_path, stop_event)
    assert len(startup_rows) == 2
    assert all(row["result"] is None and row["result_updated_at"] is None for row in startup_rows)
    assert {run["status"] for run in json.loads((tmp_path / "batch.json").read_text())["runs"]} == {"completed"}
    assert all(
        row["runtime_result"]["turn"] == 35
        and row["result"]["result"]["winner"] == "Rome"
        and row["result_updated_at"] is not None
        for row in summary_rows(tmp_path)
    )
