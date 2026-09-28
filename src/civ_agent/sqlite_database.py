"""SQLAlchemy connections with explicit SQLite durability and transaction control."""

from pathlib import Path

from sqlalchemy import URL, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool


def sqlite_engine(path: Path, *, wal: bool = False, read_only: bool = False) -> Engine:
    url = (
        URL.create("sqlite+pysqlite", database=path.resolve().as_uri(), query={"mode": "ro", "uri": "true"})
        if read_only
        else URL.create("sqlite+pysqlite", database=str(path))
    )
    engine = create_engine(
        url,
        connect_args={"timeout": 0.1 if read_only else 15},
        poolclass=NullPool,
        hide_parameters=True,
    )

    @event.listens_for(engine, "connect")
    def configure(connection, _):
        # SQLAlchemy controls BEGIN, including SELECT and DDL transactions.
        # Disable sqlite3's legacy implicit-transaction handling first.
        connection.isolation_level = None
        cursor = connection.cursor()
        try:
            if not read_only:
                if wal:
                    cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=FULL")
                cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN" if read_only else "BEGIN IMMEDIATE")

    return engine
