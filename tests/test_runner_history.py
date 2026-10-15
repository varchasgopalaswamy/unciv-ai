"""Transactional history identity, restart cursors and operator privacy."""

import contextlib
import io
import json
import sqlite3

import pytest
from sqlalchemy.exc import DatabaseError

from civ_agent.monitoring import (
    MonitorStore,
    get_record,
    metadata,
    put_record,
    read_collection,
    read_database,
    read_record,
    records,
)
from civ_agent.runner_history import EventHistory, HistoryError
from civ_agent.sqlite_database import sqlite_engine
from civ_agent.tls import TLSConfigError, write_private


def history(path, **changes):
    options = dict(epoch="match", civilization="Rome", activation="job", game_turn=0)
    return EventHistory(path, **{**options, **changes})


def test_distinct_players_epochs_and_attempts_preserve_history(tmp_path):
    path = tmp_path / "monitoring.sqlite3"
    first = history(path)
    first.append([dict(sequence=0, kind="assistant_message", data=dict(text="Settled Rome."))])
    for changes in (dict(activation="retry"), dict(civilization="Greece"), dict(epoch="restored")):
        assert history(path, **changes).cursor == 0
    with pytest.raises(HistoryError):
        history(path, game_turn=1)  # Same activation cannot silently change its turn.
    assert history(path).events == first.events
    assert not list(tmp_path.rglob("*.md")) and not list(tmp_path.rglob("*.jsonl"))


def test_restart_deduplicates_and_a_gap_cannot_commit_half_a_page(tmp_path):
    path = tmp_path / "monitoring.sqlite3"
    first = history(path)
    event = dict(sequence=0, kind="assistant_message", data=dict(itemId="m", text="Settled Rome."))
    first.append([event])
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        resumed = history(path)
        resumed.append([event])
    assert output.getvalue() == "" and resumed.cursor == 1
    with pytest.raises(HistoryError):
        resumed.append([{**event, "sequence": 1}, {**event, "sequence": 3}])
    assert history(path).cursor == 1
    assert resumed.markdown().count("Settled Rome.") == 1


def test_unusual_names_are_values_and_do_not_create_paths(tmp_path):
    path = tmp_path / "monitoring.sqlite3"
    unusual = history(path, civilization="../Rome", activation="../../job")
    literal = history(path, civilization="%2E%2E%2FRome", activation="../../job")
    assert unusual.key != literal.key
    assert {p.name for p in tmp_path.iterdir()} <= {
        "monitoring.sqlite3",
        "monitoring.sqlite3-wal",
        "monitoring.sqlite3-shm",
    }


def test_sanitized_events_are_durable_and_permissions_are_private(tmp_path):
    path = tmp_path / "monitoring.sqlite3"
    log = history(path, secrets=("player-token",))
    log.append(
        [dict(sequence=0, kind="tool_result", data=dict(receipt="accepted player-token", raw_reasoning="hidden"))]
    )
    text = json.dumps(history(path).events)
    assert "accepted" in text and "[REDACTED]" in text
    assert "player-token" not in text and "hidden" not in text
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("value", [None, -1, True, 0.5, "0"])
def test_invalid_turn_does_not_create_a_database(tmp_path, value):
    path = tmp_path / "monitoring.sqlite3"
    with pytest.raises(HistoryError):
        history(path, game_turn=value)
    assert not path.exists()


def test_database_symlink_is_rejected(tmp_path):
    target = tmp_path / "private"
    target.write_text("DO NOT CHANGE")
    target.chmod(0o600)
    link = tmp_path / "monitoring.sqlite3"
    link.symlink_to(target)
    with pytest.raises(TLSConfigError):
        history(link)
    assert target.read_text() == "DO NOT CHANGE"


def test_reader_uses_committed_snapshot_without_blocking_writer(tmp_path):
    path = tmp_path / "monitoring.sqlite3"
    store = MonitorStore(path)
    store.put("state", "run", {"status": "running"})
    with read_database(path) as reader:
        assert get_record(reader, "state", "run") == {"status": "running"}
        store.put("state", "run", {"status": "paused"})
        assert get_record(reader, "state", "run") == {"status": "running"}
    assert store.get("state", "run") == {"status": "paused"}
    from sqlalchemy.exc import OperationalError

    with read_database(path) as reader, pytest.raises(OperationalError):
        reader.execute(records.delete())


def test_monitoring_readers_wait_for_schema_commit_without_modifying_database(tmp_path):
    path = tmp_path / "monitoring.sqlite3"
    write_private(path, b"")
    assert read_record(path, "state", "run") is None
    assert read_collection(path, "state") == []
    assert path.read_bytes() == b""

    engine = sqlite_engine(path, wal=True)
    expected = {"status": "starting"}
    try:
        with engine.begin() as writer:
            metadata.create_all(writer)
            put_record(writer, "state", "run", expected)
            # Independent readers see the file before its schema is committed.
            assert read_record(path, "state", "run") is None
            assert read_collection(path, "state") == []
        assert read_record(path, "state", "run") == expected
        assert read_collection(path, "state") == [expected]
    finally:
        engine.dispose()


@pytest.mark.parametrize("schema", ["unrelated", "missing_columns", "corrupt"])
def test_monitoring_readers_still_reject_invalid_databases(tmp_path, schema):
    path = tmp_path / "monitoring.sqlite3"
    write_private(path, b"not a SQLite database" if schema == "corrupt" else b"")
    if schema != "corrupt":
        with contextlib.closing(sqlite3.connect(path)) as writer:
            table = "unrelated" if schema == "unrelated" else "monitor_records"
            writer.execute(f"CREATE TABLE {table} (collection TEXT)")
    before = path.read_bytes()
    with pytest.raises(DatabaseError):
        read_record(path, "state", "run")
    with pytest.raises(DatabaseError):
        read_collection(path, "state")
    assert path.read_bytes() == before
