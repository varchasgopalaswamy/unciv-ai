"""Live baseline capture includes committed WAL rows and never replaces evidence."""

import sqlite3
from contextlib import closing

import pytest

from tests.preserve_benchmark_baseline import backup_database, digest


def test_online_backup_freezes_wal_without_changing_or_overwriting_source(tmp_path):
    source, destination = tmp_path / "live.sqlite3", tmp_path / "frozen/memory.sqlite3"
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE history (value TEXT)")
        writer.execute("INSERT INTO history VALUES ('at boundary')")
        writer.commit()
        assert source.with_name(source.name + "-wal").stat().st_size > 0
        fingerprint = backup_database(source, destination)
        assert destination.stat().st_mode & 0o777 == 0o600
        with closing(sqlite3.connect(destination)) as frozen:
            assert frozen.execute("SELECT value FROM history").fetchall() == [("at boundary",)]
        writer.execute("INSERT INTO history VALUES ('later')")
        writer.commit()
        assert writer.execute("SELECT count(*) FROM history").fetchone() == (2,)
        assert digest(destination) == fingerprint
        with pytest.raises(FileExistsError):
            backup_database(source, destination)
        assert digest(destination) == fingerprint
