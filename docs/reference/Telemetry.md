# Telemetry and game results

Use the [full-game launcher](../guides/Full-vanilla-run.md) to record an unattended
run, and the [dashboard](../guides/Match-dashboard.md) to inspect current metrics.
This is the accounting contract; historical measurements are in the
[archive](../archive/README.md).

## Files and accounting

The game server's checkpoint owns durable telemetry. It exports private,
replaceable views beside that checkpoint under `telemetry/`:

- `turns.jsonl`: one record per scheduled civilization turn, identified by the
  native game ID, recovery epoch, and host handoff. Includes activation IDs,
  cumulative gameplay tokens and tool counts across attempts, host API counts,
  completion state, and timing.
- `jobs.jsonl`: gameplay and asynchronous communication jobs, their civilization,
  originating turn/handoff, reported token counters, model settings, API counts,
  elapsed time, and completeness. Off-turn chat is charged to its own civilization.
- `host-api.jsonl`: server call counts grouped by handoff window and actor,
  including controller bookkeeping, communication subscriptions, and receipt retries.
- `game-result.json`: native terminal winner/type/turn and frozen native scores
  for every major player, including defeated players and built-in AI. During an
  unfinished run it has `running` or `incomplete` status and current score snapshots;
  these are not a victory result. Also includes match/player totals and setup provenance.

Token fields are `input_tokens`, `cached_input_tokens`, `output_tokens`, and
`reasoning_output_tokens`. Total tokens are input plus output; cached and reasoning
counters are subsets. Counts are provider-reported usage, not a billing invoice.
Unreported usage remains explicitly incomplete, even if the engine accepted the
turn. Match totals include both gameplay and chat and retain interrupted attempts.
Input usage adds the context processed by every model request within a game turn.
It is not the size of one prompt or a count of distinct game information. Cached
input is included in that total; uncached input is input minus cached input.

Agent-facing tools are counted by the trusted service, including cached reads,
validation failures, and repeated calls. Server tools are independently counted at
the authenticated MCP boundary. `tool:`, `command:`, and `communication:` keys are
different breakdowns of the same requests: sum only `tool:` keys for total calls.
Counters distinguish success, errors, receipt replay, and uncertain interruption.
The model cannot provide its own telemetry through its advertised tools.

Turn start is the first controller activation; end is the committed native handoff
or terminal action. Wall elapsed time includes gaps between attempts. `activeMs`
uses the host's monotonic clock while the turn is unpaused, including engine work
and network/model waits; it is not model CPU time. `inactiveOrRecoveryMs` is the
remaining wall time. After an interrupted host process, `timingComplete=false`
marks active time as a lower bound. Job timing and negotiation waits are also
reported separately. Concurrent job durations must not be summed as match wall time.

Usage often arrives after `END_TURN`. A committed turn can therefore initially
have `accountingComplete=false`. The service stores cumulative reports in a private
SQLite outbox outside game rollback snapshots, then sends them with increasing
sequence numbers. Duplicate and out-of-order deliveries cannot double charge a
job. Restart preserves the latest known spending, and reconnect retries unsent
reports. A failed telemetry export does not undo a committed game command: the
operator status exposes `telemetryExportError`, and later commits/restart regenerate
the files from the checkpoint.

Winner selection and scoring come from Unciv. The score leader is not substituted
for a native winner. Budget exhaustion, interruption, and recovery create no winner.
Full telemetry and all-player scores remain operator data, outside player observations.

## Gameplay and finalization timing

`gameplayElapsedMs` ends when native handoff fences the job's game actions.
`finalizationElapsedMs` covers subsequent memory work and service archive
finalization, excluding the later scheduler-side mirror download. Memory work
continues in the same conversation and its tokens belong to that activation.
Another player may be active during finalization; do not add these overlapping
intervals to infer match wall time. See [background turn memory](../architecture/Background-turn-memory.md).

A delayed report or unfinished archive can leave turn accounting incomplete
while gameplay has already advanced. Completion, interruption and unavailable
usage remain distinguishable. Game completion flushes pending accounting before
publishing the native final result.

## Partial memory rejections

API counters classify the whole tool response. A successful `memory_update`
response can contain individual `accepted: false` changes, which are not counted
as separate API errors. Inspect per-edit results when measuring recovery work.
Dedicated rejection counts remain an [open follow-up](../Roadmap.md#observed-follow-ups).

## Implementation

The native [telemetry store](../../src/main/kotlin/uncivai/Telemetry.kt) owns turn
and result exports. [Service accounting](../../src/civ_agent/telemetry.py) keeps
job reports durable in SQLite; [database access](../architecture/Database-access.md)
describes connection and transaction ownership.
