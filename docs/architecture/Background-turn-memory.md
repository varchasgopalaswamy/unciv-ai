# Background turn memory and bookkeeping

Scheduled agents now keep the same model conversation open after a successful
native `END_TURN`. The next player can act while the outgoing agent batches useful
private memory changes. This uses the evidence already in its conversation;
there is no separate reflection agent or second context preload.

## Turn lifecycle

1. Gameplay calls durably record decisions, requests, engine receipts and tool
   responses. These writes remain synchronous so a crash cannot silently replay
   an accepted action.
2. Successful `END_TURN` returns its receipt and a `memoryPhase` instruction.
   Gameplay access ends immediately. The scheduler records the confirmed handoff
   and pending finalization before activating the next player.
3. The outgoing conversation may call only `memory_read`, `memory_search`,
   `memory_update` and `history_read`. It should batch useful changes and finish
   without a prose report. A turn with nothing worth saving needs no memory write.
4. Model completion closes that job and freezes its structured summary. The
   service generates archives asynchronously; the scheduler mirrors them and
   records final usage. The same civilization's next activation and game
   completion wait for pending work. Round checkpoints capture unfinished work
   without waiting for the model to finish.

This overlaps latency. It does not make memory reasoning or model requests free:
post-turn requests and all their token categories belong to the original job.
The agent can still revise memory during gameplay when an immediate decision needs
it. Routine intent can use a fresh `intent.reason` without saving a plan first.

## Evidence and concurrent communication

- Game observations remain the versions actually delivered during the completed
  turn. The memory phase cannot fetch fresh game state, discover gameplay tools,
  send diplomatic messages or execute commands. Exact cached call replays return
  the original response; recovery of an accepted `END_TURN` never sends it again.
- Private notes and plans may be refreshed to resolve a concurrent communication
  edit. Normal optimistic version checks still reject an overwrite based on an
  older copy. Memory search is restricted to private record types.
- Changed evidence and unread review flags stay attached to revised plans, except
  dependencies explicitly removed by the new plan. A
  frozen observation can support a historical belief, but does not establish that
  an ongoing assumption is still true. If an edit needs new game evidence, defer
  that edit until gameplay can inspect it. Repeated stale failures still stop the
  run through the existing host-side diagnostic path.
- History reads use the frozen actions for the completed turn, allow earlier
  history, and reject later turns. Memory metadata records the actual write turn;
  the job and source versions retain the original evidence context.

Historical citations and note-to-plan links do not require an active or reviewed
source record. Existing citations survive a revision without a fresh evidence
read; retained ongoing assumptions still require validation. This lets the
outgoing player record an unsuccessful plan while newer information remains
flagged for its next gameplay turn.

### Edit accounting

`telemetry.memoryEdits` reports `batches`, `accepted`, `rejected`,
`rejectedBatches`, and `rejectionReasons`. These come from the committed memory
call journal, so a lost response, replay, service restart, or game rollback does
not lose or double-count edits. A successful API request can contain rejected
edits; transport errors remain in `apiCalls`. Request validation failures that
never attempt individual edits remain API errors alone. Reasons are bounded
codes; private text and record contents are excluded.

The host preserves these counts per job and aggregates them per turn, player,
and game in its telemetry exports. Background edits belong to the outgoing turn.
`memoryEdits.complete` describes counter coverage, separately from provider token
usage completeness; aggregates with older, uninstrumented jobs are partial, and
wholly unavailable counts remain null. Console turn summaries and the TUI show
accepted/rejected edits and rejection reasons without additional API calls.

## Timeouts, durability and recovery

The service gives post-handoff memory work its own allowance equal to the
activation's configured `timeoutSeconds` (`--turn-timeout`), independently of
time already spent on gameplay. The service persists and reports that allowance
as `memoryTimeoutSeconds`; the full-game launcher's default is one hour. The
finalization worker renews the agent lease while waiting. Pause, stop and the
match deadline interrupt pending model
work, including during a checkpoint/finalization barrier. Accepted edits remain
durable; interrupted or failed provider accounting is not reported as complete.

`finish_handoff` accepts the original activation-bound handoff proof.
`begin_finalize` queues archive work and returns promptly. The scheduler polls
`finalizationState` (`PENDING`, `RUNNING`, `COMPLETED`, `FAILED`) with a separate
300-second archive budget. Status and heartbeats do not wait for file rendering.
All normal turn completion, including resumed handoffs, uses this background
protocol. The old synchronous `finalize` endpoint and capability fallbacks have
been removed. Archive downloads, interruption and terminal-outcome archiving
retain a 60-second HTTP timeout.
An idempotent control request whose timeout loses its acknowledgement is retried
once with the same proof; gameplay commands are not retried by this mechanism.

Pending work stays associated with its original epoch, turn and handoff even if
other players have since acted. Rollback drains old writers before restoring the
epoch. Archive failures retain pending finalization for recovery. A completion
marker is published only after it is saved; telemetry acknowledgements can be
retried independently. Restart preserves accepted memory, marks an unfinished
model job failed and retains its partial summary for inspection. It does not
restart paid model work automatically.

A model timeout, provider interruption or service restart after handoff stops the
run for inspection. On an explicit `match resume`, a failed memory attempt already
present in the saved run can be acknowledged after its handoff, stopped process
and archive are verified. The scheduler records `acknowledgedMemoryFailures` and
prints a warning before continuing. Confirmed actions and committed memory edits
remain; unsaved notes are unavailable. The activation retains its failed status,
incomplete memory marker and partial provider accounting. Neither gameplay nor
reflection is replayed. A new failure during the resumed run still stops it.
Internal state failures, repeated stale assumptions, unconfirmed shutdown and
archive errors are not acknowledged by this recovery path.

`gameplayElapsedMs` ends at handoff. `finalizationElapsedMs` runs from handoff
through memory completion and service archive finalization; it excludes the
scheduler's subsequent archive copy. These intervals overlap another player's
gameplay and must not be added to estimate match wall time.

## Round checkpoints during memory work

At a round boundary, the coordinator stops admitting new tool calls and lets
accepted calls finish their writes and receipts. It then freezes committed memory,
captures the host and each player's exact record versions and job snapshots,
and publishes the manifest with the scheduler's pending finalizations. The hold
is released after all components are durable. Calls arriving during capture wait
inside the service and continue after release, without a model-visible error or
retry. Model computation itself continues. Job completions during the hold are
deferred until release.

The next player's turn can start while the previous round's last agent finishes
its memory update. In a mixed game, placing humans first in the native turn order
provides human turns during which the preceding round's agent memory can finish.
The scheduler still waits for a civilization's own previous finalization before
its next activation.

Restoration cancels abandoned jobs, restores the captured memory revisions and
job state, and rebuilds factual turn summaries from committed receipts. A memory
job unfinished at capture is restored as interrupted even if it completed later.
Its summary and operator diagnostics explicitly report incomplete reflection;
only edits committed at capture are visible. Later notes and job completion
metadata cannot leak into the restored timeline. No gameplay command or paid
reflection is automatically rerun. Accounting remains outside the rewind.

Regression coverage holds the last player's reflection across a checkpoint,
checks next-player overlap and same-player ordering, drains an in-flight memory
write, queues a write during capture, and restores before/after late job completion.

## Incremental exports

SQLite remains authoritative. A persistent `export_jobs` queue tracks jobs whose
generated history needs rebuilding. Export regenerates current private views and
only changed turn/communication groups; retries in the same turn contribute to
the same receipt file. Previously generated history remains intact. Missing
generated files are rebuilt, and rollback removes abandoned projections.

Dirty work is cleared only after file writes succeed. An interrupted export is
safe to repeat. Existing structured-memory databases gain the indexes and queue
automatically and receive one initial full export; no new match is required.
Memory writes do not render files before replying, and gameplay job closure no
longer performs a duplicate export. Turn archives also cache earlier receipt IDs
until an attempt changes, avoiding repeated full-history parsing.

## Validation and measurement

The full Python suite passed with 610 tests and 90 subtests; the opt-in installed
Codex capture test was skipped. Five finalization tests passed again after the
final pause/deadline check. Ruff lint and formatting, Pyrefly and diff checks passed.

Regression tests cover frozen evidence, concurrent memory edits, recovered
handoff receipts, timeouts, pause, lost acknowledgements, incremental exports,
missing files and same-turn retries. A real HTTPS test holds export for more than
the former five-second timeout while status stays responsive. A scheduler test
with fake model processes confirms Greece can finish gameplay before Rome writes
its post-turn note. No paid models are used in those tests.

The existing vanilla run stopped after Greece's turn 35, with France activated:
the scheduler's five-second synchronous finalization request timed out. Greece's
service did finish the bookkeeping, about 6.6 seconds after handoff. That elapsed
time includes more than the HTTP request itself. Full-history memory export was
repeated during finalization and at job close.

On a private copy of that run's Greece database, the previous memory export took
about 2.4 seconds. Exporting only dirty turn 35 took about 0.05 seconds; an
unchanged export took about 0.014 seconds. This is a local export measurement,
not a live gameplay or model-token benchmark. The original run was not modified
or resumed.

## Database access follow-up: implemented

Memory, history/export, checkpoint, stale-recovery and accounting queries now use
SQLAlchemy Core with the existing SQLite files. Shared schema definitions and
query expressions preserve transaction durability, revision filtering and
recovery. See [database access](Database-access.md) for compatibility checks and
the comparison against all 36 saved Greece turn summaries.
