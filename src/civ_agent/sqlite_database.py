"""SQLAlchemy connections with explicit SQLite durability and transaction control."""

from pathlib import Path

from sqlalchemy import URL, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool


def sqlite_engine(path: Path) -> Engine:
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(path)),
        connect_args={"timeout": 15},
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
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine
