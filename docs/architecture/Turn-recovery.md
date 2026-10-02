# Turn recovery

Recovery retains one coordinated checkpoint at the start of the active player's
turn. `game resume` rewinds an interrupted turn, including player memories and
shared diplomacy, and starts a new conversation. Completed turns are never replayed.
The host's durable handoff receipt determines whether a turn completed.

The checkpoint is published before an activation can start. It references immutable
database records and completed jobs; it copies only unfinished job state. A new
checkpoint replaces the previous checkpoint. Partial captures are not restore
points. A restore journal makes an interrupted multi-service restore repeatable.
Usage, retry counts and operator diagnostics remain outside the rewind.
Accounting retains accepted completion flags as well as counters: restoring an
older activation record cannot downgrade a report for work that already completed.
Host rejections leave reports pending and surface as telemetry failures.

The coordinator renews the host lease while waiting for each service's checkpoint
operations, including restores that take longer than the lease duration. If renewal
fails, it finishes the in-flight request and stops before starting the next step;
the restore journal lets the next resume continue with the same recovery epoch.

Recovery restores canonical memory and event prefixes without generating reports.
Derived summaries are reconstructed only when explicitly requested. Operator event
streams remain in the monitoring database under their original epochs; the TUI
filters the current epoch. Repeated restores no longer move or repair event mirror
directories. Activation/turn identity is checked before model work starts.
See [monitoring](Monitoring.md) for storage and explicit export commands.

## Background reviews

Opening reviews capture each civilization's own starting view before any player
moves, then run concurrently. Every K rounds, starting at K (default 5), the
scheduler waits for all players and active conversations to finish, freezes play
and communication, and captures fresh inputs for concurrent reviews. Every report
must finish before the shared phase is released. Reviewers can read only the captured position and planning
catalogues, and cannot write notebook entries.

Pending reviews restart from saved inputs after a stop; completed reports are
reused. The next gameplay checkpoint includes the completed reports and reviewed
round marker, so rewinding that turn does not repeat its preceding review phase.
Attempts and paid usage survive rewind. Completion atomically saves advice and removes input.
`--review-attempts` defaults to 3; exhaustion stops for inspection.

## Stops

- An active-player crash, deadline or incomplete turn stops gameplay. Explicit
  resume restores the turn checkpoint and starts a fresh attempt.
- A lost action response is reconciled against its durable receipt first.
- A failed strategic reviewer can retry within the shared review phase. Gameplay
  and communication wait until all reports are ready.
- Repeated state conflicts, broken invariants, persistence failures and exhausted
  review retries stop for inspection; they are not automatic gameplay retry loops.
- Operator pauses and budget limits stop work until explicit resume.

This replaces historical round checkpoints and the manual round-rollback command.
The memory store uses format 9 and scheduler state uses format 5. Older matches
require their original code; there is no migration. Start a fresh match with this
version. Native checkpoint behavior is unchanged.
Replaying work spends additional tokens and time and may produce different choices.
Human players cannot forget messages already delivered before a rewind.

## Validation

The scripted engine tests interrupt a turn after founding a city and moving a unit,
restart the host, rewind and verify the original position and fresh credentials.
They also lose acknowledgements, inject storage failures before and after atomic
replacement, and reject rewinding across a committed handoff. Trade tests verify
that a rewind also restores ownership, treasury, diplomacy and private messages.

Python tests cover partial coordinated restores, pending reviewers across another
player's turn, frozen evidence, atomic publication, bounded retries, discarded
future history, compact checkpoints, and persistence failure reporting. These use
fake model processes; no paid model run is needed for recovery validation.
