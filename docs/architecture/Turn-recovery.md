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

## Native state rebuilt during loading

Loading an engine save also rebuilds visibility, city connections and other caches.
This can create first-contact alerts and ordinary notifications, establish diplomacy,
reveal tiles, and award native city-state or natural-wonder discovery rewards.
Restoration must retain those effects along with the loaded game.

Both process restart and turn rewind validate saved alert and notification handles
against the original serialized queues. The original entries must remain an
unchanged, ordered prefix of the loaded queues. Appended entries receive fresh
player-local handles when observed. Missing, reordered or modified saved entries,
including changed alert request IDs, stop restoration instead of rebinding a handle
to a different event. Unread saved entries can also receive their first handles after
loading. Repeated loads of the resulting checkpoint must not repeat native events
or rewards.

Other recovery boundaries require different checks:

- City and unit references bind to stable engine IDs, not collection positions.
  Engine/ruleset migrations that remove or replace those objects need separate
  compatibility validation; allowing appended alerts does not make arbitrary engine
  upgrades safe.
- Observations, legal actions and revisions are rebuilt after restoration. A saved
  receipt can replay, but an uncommitted action requires a fresh observation.
- Native trade requests retain durable request IDs; transient response callbacks
  are reattached by the native trade bridge before player operations.
- Accounting remains outside turn rewind and uses commit IDs for outbox replay;
  it must not be inferred from the number or order of engine events.

## Background reviews

Opening reviews capture each civilization's own starting view before any player
moves, then run concurrently. Every K rounds, starting at K (default 5), the
scheduler waits for all players and active conversations to finish, freezes play
and communication, and captures fresh inputs for concurrent reviews. Every report
must finish before the shared phase is released. Reviewers can read only the captured position and planning
catalogues, and cannot write notebook entries.

Human text messages submitted during review wait in a checkpointed, sender-private
queue. They enter recipient streams only when the shared phase ends. Restoring a
checkpoint restores its queue and request receipts together; discarded-attempt
messages are not delivered.

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
- Broken invariants, persistence failures and exhausted
  review retries stop for inspection; they are not automatic gameplay retry loops.
- Operator pauses and budget limits stop work until explicit resume.

This replaces historical round checkpoints and the manual round-rollback command.
The memory store uses format 11 and scheduler state uses format 5. Matches using
older memory or scheduler formats require their original code; those formats have
no migration. Native checkpoint format 9 separates accounting from the game save;
format 8 checkpoints are imported on load with an original-file backup. The game
checkpoint and its accounting database must stay together. See
[host accounting](Host-accounting.md) for migration and crash recovery.
Replaying work spends additional tokens and time and may produce different choices.
Human players cannot forget messages already delivered before a rewind.

## Local human turns

The desktop shares the host's executor. Native input and asynchronous UI work run
on the application thread; HTTPS operations dispatch there before acquiring the
executor lock. Persistent native changes invalidate player observations and are
checkpointed before the next operation. End Turn uses the host's durable handoff,
not a second desktop copy of the game. Replacing the game or changing authority
invalidates old UI callbacks and rebuilds the human's fixed player view.
While chat is open, ordinary background changes defer that world rebuild so typing
keeps its stage, focus and selection. Returning to native controls refreshes the
world before accepting input. Unsent drafts are retained per conversation within
the desktop session and cleared when a rewind replaces the game.

The scheduler captures the same start-of-turn checkpoint before human activation.
There is no human model conversation or memory store to restore. Credential
rotation retains both controller roles and the complete roster. Human telemetry
records elapsed time and native actions separately from model/API spend. Closing
the window writes the ordinary match stop request and lets services shut down.

## Validation

The scripted engine tests interrupt a turn after founding a city and moving a unit,
restart the host, rewind and verify the original position and fresh credentials.
They also lose acknowledgements, inject storage failures before and after atomic
replacement, and reject rewinding across a committed handoff. Trade tests verify
that a rewind also restores ownership, treasury, diplomacy and private messages.
Alert recovery tests rebuild first contact during restart and rewind, preserve
observed and unread saved alerts, replay acknowledgements, reject changed saved
queues, and verify that a natural-wonder reward is retained without duplication on
the next restart.

Python tests cover partial coordinated restores, pending reviewers across another
player's turn, frozen evidence, atomic publication, bounded retries, discarded
future history, compact checkpoints, and persistence failure reporting. These use
fake model processes; no paid model run is needed for recovery validation.
