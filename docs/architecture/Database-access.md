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
independent `sqlite3` readers. They inspect stored records without depending on
the runtime query implementation. Historical analysis does not make an old match
resumable with the current runtime.

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

Observation changes publish direct advisory notices in one batch. The batch reads
head versions without loading every observation body. It does not propagate a
dependency graph or rewrite notebook entries. Repeated changes advance notice
references even when the reason is unchanged.

Cached detail inputs carry a fingerprint tied to their exact record-head version
in service metadata. Unchanged inputs reuse the existing evidence; changed inputs
are reprojected and only factual differences create refreshed historical details.
A late read from another job's older snapshot cannot conceal a newer change.
Immutable history, delivered evidence, command-state validation and rollback
retain their transaction boundaries.

Heartbeats renew the native lease without joining the database/history queue.
Periodic accounting and telemetry use the background renderer and retain durable
unsent reports. `service_io_slow` diagnostics separate queue and work milliseconds
and name the operation without recording its arguments or private contents.

The turn-126 Rome regression is covered by `test_observation_refresh_scaling.py`
and a heartbeat test with an intentionally blocked database worker. A replay on
the stopped game's copied database from before format 3 is retained under
`build/stopped-game-audit-20260923/`; the original match is unchanged. Its first
refresh, including fingerprint initialization, fell from about 9.0 seconds to
0.69 seconds. This measures service refresh work, not model response time.

## Store format and rollback

The notebook uses format 3. Older memory files and checkpoints are rejected;
there is no migration or compatibility path. Use a fresh match after this change.

Within the current format, checkpoint hashes cover reachable immutable records,
events and command receipts. Restoring an earlier head excludes discarded future
revisions while retaining their rows and never reusing identifiers. Notebook
entries, communication contributions and advisory notices share this durable
revision mechanism. Their meanings and write ownership are separate.

## Validation

`test_sqlalchemy_storage.py` independently checks current checkpoint hashes, rollback
ancestry, monotonically allocated identifiers, old-format rejection and accounting
recovery. It also checks foreign-key failures roll back the entire transaction, uncommitted writes
remain invisible to readers, and another writer cannot enter before commit.
The existing memory, gateway, HTTPS, scheduling and recovery suites exercise the
converted runtime paths.

During the SQLAlchemy conversion, the full Python suite passed with 616 tests and
90 subtests. On a copy of the saved Greece game, all 36 turn summaries matched the previous
`sqlite3` implementation exactly. A dirty turn-35 export took about 0.064 seconds;
an unchanged export took about 0.018 seconds. This confirms the incremental-export
behavior remains effective; SQLAlchemy itself is a maintainability change, not
a measured gameplay speedup. Those measurements predate notebook format 3.
