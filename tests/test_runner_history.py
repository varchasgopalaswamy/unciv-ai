"""Transactional history identity, restart cursors and operator privacy."""

import contextlib
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
from civ_agent.sqlite_database import sqlite_engine
from civ_agent.tls import write_private


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
