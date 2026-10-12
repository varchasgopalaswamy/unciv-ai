# Host bookkeeping reliability, October 12, 2026

The stopped games were `all-luna-attempt-1-1` g0001 and g0003. Their recorded
failures were host operator status and heartbeat timeouts during active gameplay.
Neither had pending turn finalization. The previous turn's bookkeeping took
1.566 seconds in g0001 and 0.974 seconds in g0003; the full-run medians were
0.863 and 0.874 seconds. The evidence does not point to slow finalization as the
cause of these stops.

Immediately before the stops, a construction request in g0001 succeeded after
7.235 seconds and a Great Person request in g0003 succeeded after 5.123 seconds.
The next player reads received HTTP 401 after the scheduler paused the game and
revoked the activation. These observations are consistent with health checks
waiting behind host work longer than their five-second timeout.

The deployed host wrote its entire checkpoint at API start, at API completion,
and again for a changed job report or permit. The two checkpoint files were
17,939,243 and 18,049,752 bytes, although their embedded native saves were only
about 0.5 MB. Serialization, atomic replacement and disk synchronization ran
under the same game lock used by operator status and lease renewal. Job reports
arrived every two seconds and renewals every five seconds. These synchronous
writes explain the avoidable contention; the old runtime did not record enough
per-stage timings to apportion each observed delay precisely.

## Implemented behavior

- Job accounting uses small, ordered SQLite transactions. Callers wait for durable
  acknowledgement outside the game lock. Reports and renewals do not save the game.
- Native state and command receipts still commit together. Outstanding accounting
  batches travel in a checkpoint outbox and replay idempotently after a crash.
- Dashboard publication runs separately, updates totals incrementally, preserves
  diplomacy chronology, and retains pending rows after a failed publication.
- Status reads a committed snapshot and lease renewal uses atomic control state.
  Blocking player work runs away from HTTP request threads. Agent-service health
  checks are independent of the background native-permit renewal task.
- Ordinary control requests time out after 15 seconds. Host status and heartbeat
  retry at most one transport timeout within the confirmed lease budget, retaining
  a five-second margin. Native mutations are not automatically retried.
- Timing counters distinguish game-lock waits, game execution, native capture,
  checkpoint serialization, disk synchronization, accounting, and publication.

See [host accounting](../../architecture/Host-accounting.md) for the storage,
migration, backup and failure contracts.

## Copied-game replay

The October 11 replay used copies of the two stopped checkpoints, with five
concurrent clients per game. Each client claimed a job, submitted 20 cumulative
reports and 20 renewals, then released the job. No model calls were made. A
separate phase deliberately blocked game access and queued two player reads;
HTTPS status and lease renewal had to complete within two seconds. A host restart
then verified that the final report sequences and usage survived.

| Measurement | g0001 | g0003 |
| --- | ---: | ---: |
| Reports, each followed by renewal | 100 | 100 |
| Full game writes during accounting traffic | **0** | **0** |
| Median report + renewal duration | 280 ms | 249 ms |
| 95th percentile report + renewal duration | 518 ms | 323 ms |
| Game checkpoint after migration | 10,397,395 bytes | 10,341,651 bytes |
| Accounting database after close | 10,502,144 bytes | 10,735,616 bytes |
| Blocked-game health check and restart verification | Passed | Passed |

Raw results: [g0001](../../../build/host-reliability-20261011/g0001/result.json)
and [g0003](../../../build/host-reliability-20261011/g0003/result.json).
The preserved format 8 input SHA-256 values are
`bdb8abcd62040af7faea3c366aca6ed5a83d7df266400301282464df7283aea5`
and `c77a97a927b35307e9667373ffca21f51e715bb98e6361b2b6013645cfe3008a`.

These are local concurrency and recovery measurements, not a production latency
guarantee or a gameplay-strength evaluation. Native commands still require full
game commits. The original games were not resumed or modified, and the running
containers have not received this implementation.

## Regression coverage

Tests exercise before/after-replacement failures, repeated outbox replay, newer
reports surviving an old outbox, interrupted accounting transactions, migration,
missing databases, uncertain API calls, turn rewind, lease expiry, lost responses,
blocked service heartbeats, and permit renewal failure. Dashboard failure tests
verify retained event order and cumulative totals after retry.

The complete Python suite passed: **1,337 tests and 76 subtests**, with 10
environment-dependent tests skipped. Ruff lint/format checks and Pyrefly passed.
The complete Kotlin build passed with **168 tests**, no failures and no skips.
Its recovery suites cover native receipts, economic and third-party trades,
notification restoration, ordinary launch, and scenario restart behavior. Both
the authoritative accounting failure and dashboard retry injections passed.
