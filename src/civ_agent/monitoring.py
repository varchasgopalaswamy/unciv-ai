"""Operator records shared by the host, scheduler and read-only dashboard.

Each replacement gets a new revision, so observers fetch only changed rows.
Events have immutable stream/sequence identities. No player mounts this database.
The Kotlin writer uses the same small schema (MonitoringStore.kt).
"""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Column, Index, Integer, MetaData, String, Table, Text, UniqueConstraint, inspect, select

from civ_agent.sqlite_database import sqlite_engine
from civ_agent.tls import private_directory, regular_file

metadata = MetaData()
records = Table(
    "monitor_records",
    metadata,
    Column("revision", Integer, primary_key=True, autoincrement=True),
    Column("collection", String, nullable=False),
    Column("key", String, nullable=False),
    Column("epoch", String),
    Column("civilization", String),
    Column("game_turn", Integer),
    Column("activation", String),
    Column("sequence", Integer),
    Column("payload", Text, nullable=False),
    Column("updated_ms", Integer, nullable=False),
    UniqueConstraint("collection", "key"),
    sqlite_autoincrement=True,
)
Index("monitor_changes", records.c.collection, records.c.revision)
Index("monitor_turns", records.c.collection, records.c.epoch, records.c.civilization, records.c.game_turn)
Index("monitor_events", records.c.collection, records.c.epoch, records.c.activation, records.c.sequence)


def path_for(state_directory: Path) -> Path:
    return state_directory / "runner" / "monitoring.sqlite3"


def record_key(*parts) -> str:
    return json.dumps(parts, ensure_ascii=True, separators=(",", ":"))


def get_record(db, collection, key):
    value = db.scalar(select(records.c.payload).where(records.c.collection == collection, records.c.key == key))
    return json.loads(value) if value is not None else None


def put_record(db, collection, key, payload, **identity):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    previous = db.scalar(select(records.c.payload).where(records.c.collection == collection, records.c.key == key))
    if previous == encoded:
        return
    # No foreign keys reference revisions. REPLACE deliberately allocates a new
    # monotonic revision even when updating a previously published turn.
    db.execute(
        records.insert()
        .prefix_with("OR REPLACE")
        .values(
            collection=collection,
            key=key,
            payload=encoded,
            updated_ms=int(time.time() * 1000),
            **identity,
        )
    )


@contextmanager
def read_database(path):
    """Never create a file, reserve a write lock, or construct a game service."""
    regular_file(path, private=True)
    engine = sqlite_engine(path, read_only=True)
    try:
        with engine.connect() as db, db.begin():
            yield db
    finally:
        engine.dispose()


def read_record(path, collection, key):
    if not path.exists():
        return None
    with read_database(path) as db:
        # Writers publish the file before committing the schema. Only a wholly
        # empty database is pending; nonempty incompatible schemas still fail.
        if not inspect(db).get_table_names():
            return None
        return get_record(db, collection, key)


def read_collection(path, collection):
    if not path.exists():
        return []
    with read_database(path) as db:
        if not inspect(db).get_table_names():
            return []
        return [
            json.loads(value)
            for value in db.scalars(
                select(records.c.payload).where(records.c.collection == collection).order_by(records.c.revision)
            )
        ]


class MonitorStore:
    def __init__(self, path):
        self.path = Path(path)
        private_directory(self.path.parent)
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            regular_file(self.path, private=True)
        else:
            os.close(descriptor)
        self.engine = sqlite_engine(self.path, wal=True)
        metadata.create_all(self.engine)

    def put(self, collection, key, payload, **identity):
        with self.engine.begin() as db:
            put_record(db, collection, key, payload, **identity)

    def get(self, collection, key):
        return read_record(self.path, collection, key)
