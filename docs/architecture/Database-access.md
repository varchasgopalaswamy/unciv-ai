# Database access with SQLAlchemy Core

The memory and accounting stores use SQLAlchemy Core with SQLite. Core defines
tables and composes parameterized statements; the domain code still explicitly
controls memory revisions, receipts, visibility and recovery. There are no ORM
sessions or additional database services.

## Ownership

- [memory_schema.py](../../src/civ_agent/memory_schema.py) defines the existing memory
  tables and indexes, shared joins for current records and live requests, and the
  recursive query that follows a restored revision's ancestors. The memory store,
  stale-recovery diagnostics, summaries, exports and checkpoints use these
  expressions instead of maintaining separate SQL strings.
- [telemetry.py](../../src/civ_agent/telemetry.py) defines the accounting schema and
  uses Core for job reports, call counts and acknowledgement updates. Accounting
  remains outside game rollback.
- [sqlite_database.py](../../src/civ_agent/sqlite_database.py) owns connection and
  transaction setup. The existing `ServiceIO` worker runs synchronous database
  work outside the HTTPS event loop.

The read-only scenario verifier and live-run analysis tools retain their
independent `sqlite3` readers. They can inspect both old and new databases without
depending on the runtime query implementation. Compatibility fixtures also use
the old SQL schema deliberately.

## Transactions and failures

Each transaction opens a connection and closes it afterward through `NullPool`.
SQLite uses `synchronous=FULL`, foreign-key enforcement and a 15-second lock wait.
The connection disables the driver's legacy implicit transaction handling;
SQLAlchemy's begin hook explicitly issues `BEGIN IMMEDIATE`. This preserves the
memory store's locking and gives accounting read/modify/write operations the
same explicit serialization. Success commits; exceptions roll back.

Do not introduce a separate commit inside an operation that updates a revision,
its current head, request identity, receipt or tool-call result. Those changes
must retain their existing transaction boundaries. Similarly, an accounting
acknowledgement must still check its sequence against the latest saved report.

SQLAlchemy wraps driver failures. The service and gateway catch `SQLAlchemyError`
at their existing failure boundaries. Engine exception formatting hides bound
parameter values. The few driver-level statements are connection configuration
and `BEGIN IMMEDIATE`, rather than domain queries. See SQLAlchemy's
[SQLite transaction guidance](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#enabling-non-legacy-sqlite-transactional-modes-with-the-sqlite3-or-aiosqlite-driver).

## Observation refresh cost

Observation changes flag the active plan/belief dependency graph in one batch.
The batch reads evidence head versions without loading every observation body,
propagates dependencies and writes each affected private record once. Repeated
changes advance the required review evidence even when the reason is unchanged.

Cached detail inputs carry a fingerprint tied to their exact record-head version
in service metadata. Unchanged inputs reuse the existing evidence; changed inputs
are reprojected and only factual differences create refreshed historical details.
A late read from another job's older snapshot cannot conceal a newer change.
Existing databases initialize these fingerprints on their next refresh. Immutable
history, delivered evidence, assumption validation and rollback remain intact.

Heartbeats renew the native lease without joining the database/history queue.
Periodic accounting and telemetry use the background renderer and retain durable
unsent reports. `service_io_slow` diagnostics separate queue and work milliseconds
and name the operation without recording its arguments or private contents.

The turn-126 Rome regression is covered by `test_observation_refresh_scaling.py`
and a heartbeat test with an intentionally blocked database worker. A replay on
the stopped game's copied database is retained under
`build/stopped-game-audit-20260923/`; the original match is unchanged. Its first
refresh, including fingerprint initialization, fell from about 9.0 seconds to
0.69 seconds. This measures service refresh work, not model response time.

## Compatibility

Existing structured-memory format-2 files and accounting databases open directly.
No data conversion or new match is needed. The migration preserves column order,
types, nullability, foreign keys, JSON text encoding, rowid evidence handles and
the memory handle sequence. Core inserts retain SQLite's existing ignore/replace
conflict behavior where it was already used.

Checkpoint files and history hashes retain their format. Restoring an earlier
head excludes discarded future revisions from history while retaining those
rows and never reusing their identifiers. Queries keep their explicit ordering;
checkpoint rows use positional values, while named results use Core mappings.
The default summary query also preserves the previous handling of an unspecified
turn instead of treating it as a request for jobs with a null turn.

Schema creation defines fresh databases. Existing tables are left in place;
missing indexes and the previously introduced export queue are initialized as
before. This is not a general schema-migration framework: a future change to
existing columns will need an explicit migration.

## Validation

The full Python suite passed with 616 tests and 90 subtests; the opt-in installed
Codex capture test was skipped. Ruff lint/format, Pyrefly and diff checks passed.

`test_sqlalchemy_storage.py` creates databases from frozen pre-migration SQL and
checks old checkpoint hashes, schema compatibility, rollback ancestry, monotonically
allocated identifiers, missing-index upgrades and accounting recovery. It also
checks foreign-key failures roll back the entire transaction, uncommitted writes
remain invisible to readers, and another writer cannot enter before commit.
The existing memory, gateway, HTTPS, scheduling and recovery suites exercise the
converted runtime paths.

On a copy of the saved Greece game, all 36 turn summaries matched the previous
`sqlite3` implementation exactly. A dirty turn-35 export took about 0.064 seconds;
an unchanged export took about 0.018 seconds. This confirms the incremental-export
behavior remains effective; SQLAlchemy itself is a maintainability change, not
a measured gameplay speedup. No live game or paid model run was started.
