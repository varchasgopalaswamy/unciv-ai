"""Notebook, observation journal and turn recovery schema."""

from sqlalchemy import Column, ForeignKey, Index, Integer, MetaData, Table, Text, and_, literal_column, select

FORMAT_VERSION = 10

metadata = MetaData()
meta = Table(
    "meta", metadata, Column("key", Text, primary_key=True, nullable=True), Column("value", Text, nullable=False)
)
events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, nullable=True),
    Column("source", Text, nullable=False, unique=True),
    Column("kind", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("wake", Integer, nullable=False),
)
records = Table(
    "records",
    metadata,
    Column("key", Text, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("category", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("parent", Integer),
    Column("job", Text),
)
handles = Table("handles", metadata, Column("id", Integer, primary_key=True, nullable=True), sqlite_autoincrement=True)
jobs = Table(
    "jobs",
    metadata,
    Column("id", Text, primary_key=True, nullable=True),
    Column("epoch", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("snapshot", Text, nullable=False),
    Column("settings", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("game_turn", Integer),
)
outbox = Table(
    "outbox",
    metadata,
    Column("id", Text, primary_key=True, nullable=True),
    Column("job", Text, nullable=False),
    Column("transport", Text, nullable=False),
    Column("payload", Text, nullable=False),
    Column("dependencies", Text, nullable=False),
    Column("result", Text),
    Column("decision", Text, nullable=False),
)
calls = Table(
    "calls",
    metadata,
    Column("job", Text, primary_key=True, nullable=True),
    Column("id", Text, primary_key=True, nullable=True),
    Column("name", Text, nullable=False),
    Column("arguments", Text, nullable=False),
    Column("result", Text),
)
heads = Table(
    "heads", metadata, Column("key", Text, primary_key=True, nullable=True), Column("version", Integer, nullable=False)
)
inbox = Table("inbox", metadata, Column("id", Integer, ForeignKey(events.c.id), primary_key=True, nullable=True))
outgoing = Table("outgoing", metadata, Column("id", Text, ForeignKey(outbox.c.id), primary_key=True, nullable=True))
live_jobs = Table("live_jobs", metadata, Column("id", Text, ForeignKey(jobs.c.id), primary_key=True, nullable=True))
reviews = Table(
    "reviews",
    metadata,
    Column("job", Text, primary_key=True),
    Column("turn", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("inputs", Text),
    Column("settings", Text, nullable=False),
    Column("timeout", Integer, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("advice", Text),
    Column("error", Text),
)
Index("records_by_job", records.c.job)
Index("jobs_by_turn", jobs.c.game_turn, jobs.c.purpose)
Index("outbox_by_job", outbox.c.job)

# SQLite rowids are durable ordering/evidence handles, not new schema columns.
record_order = literal_column("records.rowid", Integer)
job_order = literal_column("jobs.rowid", Integer)
request_order = literal_column("outbox.rowid", Integer)
call_order = literal_column("calls.rowid", Integer)


def record_versions(versions):
    return records.join(versions, and_(records.c.key == versions.c.key, records.c.version == versions.c.version))


current_records = record_versions(heads)
live_requests = outbox.join(outgoing, outbox.c.id == outgoing.c.id)
active_jobs = jobs.join(live_jobs, jobs.c.id == live_jobs.c.id)

# A restored head reaches only its own ancestors, never a discarded future.
reachable = select(heads.c.key, heads.c.version).cte("reachable", recursive=True)
reachable = reachable.union(
    select(records.c.key, records.c.parent).select_from(record_versions(reachable)).where(records.c.parent.is_not(None))
)
