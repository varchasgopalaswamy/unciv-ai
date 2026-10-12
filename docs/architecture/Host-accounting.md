# Host accounting and responsiveness

The native game and its command receipts commit atomically to the game checkpoint.
Operator accounting lives in an owner-only `<checkpoint>.accounting.sqlite3`
database, using SQLite WAL and `synchronous=FULL`. The dashboard's
`runner/monitoring.sqlite3` remains a rebuildable projection.

API instrumentation records call starts and completions as small transactions.
Job reports, claims, releases and permit renewals update individual records.
Cumulative report sequence checks reject conflicting or decreasing reports and
make retries idempotent. These requests create no game checkpoint unless an
independent game event, such as an expired negotiation, requires a native commit.

The executor freezes changed records and submits them to one ordered writer while
holding the game lock. It waits for durable acknowledgement after releasing that
lock. Publication, historical aggregation and dashboard I/O run on a separate
worker, normally once per second. Match/player totals update from changed records;
status requests do not rescan historical jobs. A dashboard failure retains its
pending rows and exposes `telemetryWriteError`. An authoritative accounting write
failure stops acknowledgements and halts the runtime.

## Crash recovery and migration

Each accounting batch has a unique commit ID. A native checkpoint includes its
accounting database ID and all outstanding accounting batches, including the
current turn-boundary records. Only after atomic game-file replacement is the
new batch submitted to the writer. Recovery replays this outbox transactionally.
Already committed batch IDs are skipped, so an old outbox cannot overwrite a
newer report received after the game checkpoint. Calls without a completed record
remain explicitly uncertain after restart. Rewinding a turn preserves accounting
and costs, invalidates old job permits, and restores game/diplomacy state through
the existing coordinated recovery protocol.

Loading a format 8 checkpoint imports its accounting, preserves the original as
`<checkpoint>.v8.json`, and atomically publishes format 9. An interrupted import
can be repeated. Format 9 requires the matching accounting database; a missing or
unrelated database is an error. Back up the stopped match directory as a unit,
including the accounting database and any WAL files. A coordinated turn snapshot
contains the rewindable game state; accounting remains outside that rewind.

## Health checks

Operator status reads an immutable snapshot of the last committed game state.
An in-progress mutation cannot advertise a completed handoff. Status includes
`busyOperation` and `busyElapsedMs`. Lease renewal uses atomic control state and
does not acquire the game lock. Expiry immediately fences authority; an expired
lease cannot be renewed. Native cleanup runs on the next game access.

Blocking player work runs away from HTTP request threads. Python agent-service
heartbeats acknowledge service liveness without waiting for a host call; a
separate task renews the native job permit every five seconds and stops the job
if renewal fails.

Ordinary host and agent control requests have a **15-second timeout**. Existing
long-operation allowances remain 60 seconds. Host status and heartbeat may retry
one transport timeout, using only the remaining confirmed lease time with a
five-second safety margin. Rejections and non-idempotent operations are not retried.

## Validation and diagnostics

Host timing counters distinguish game-lock waits, game execution, game capture,
checkpoint serialization, disk flushes, accounting commits and monitoring writes.
Counters contain no request bodies and create no persistence work themselves.
The monitoring result includes these counters under `hostTimings`.

Unit and HTTPS probes cover lost responses, expired leases, blocked game access,
concurrent reports, duplicate/outdated reports, failed transactions, crashes
around atomic replacement, outbox replay, migration and turn rewind. To exercise
a copied legacy checkpoint without model calls:

```sh
./gradlew verifyHostReliability \
  -PreliabilityCheckpoint=/absolute/path/to/stopped-game/exploration.json \
  -PreliabilityDir=/absolute/path/to/new-output-directory
```

The probe copies the source, runs concurrent reports and permit renewals, checks
that the game file did not change, blocks game access while checking HTTPS health,
and restarts to verify the reports survived. Omitting `reliabilityCheckpoint`
creates a fresh game. The source match is never resumed or modified.
