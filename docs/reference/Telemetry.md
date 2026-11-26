# Telemetry and game results

Use the [full-game launcher](../guides/Full-vanilla-run.md) to record an unattended
run, and the [dashboard](../guides/Match-dashboard.md) to inspect current metrics.

## Storage and accounting

The host owns durable accounting in `<checkpoint>.accounting.sqlite3`. Game
checkpoints retain a small accounting outbox so a crash cannot separate a committed
turn boundary from its accounting. Reports and permit renewals update database
rows without rewriting the game. See [host accounting](../architecture/Host-accounting.md).
The host asynchronously publishes changed rows in the operator's
`runner/monitoring.sqlite3`:

- `turns`: one record per scheduled civilization turn, identified by native game
  ID, recovery epoch and handoff. Includes activation IDs, tokens, API counts,
  accounting completeness and timing.
- `jobs`: gameplay, memory and communication jobs, with their originating player,
  turn/handoff, model settings, counters and completeness.
- `host-api`: server call counts grouped by handoff window and actor, including
  controller bookkeeping, subscriptions and receipt retries.
- `state/result`: native terminal winner/type/turn and frozen scores for every
  major player. Unfinished runs contain current scores and running/incomplete
  status, plus match/player totals and setup provenance.

The TUI and console read these records directly. `civ-agent history export` creates
JSONL and readable reports on demand. See [monitoring](../architecture/Monitoring.md)
for transaction ownership, read-only access and importing an existing stopped game.

Token fields are `input_tokens`, `cached_input_tokens`, `output_tokens`, and
`reasoning_output_tokens`. Total tokens are input plus output; cached and reasoning
counters are subsets. Counts are provider-reported usage, not a billing invoice.
Unreported usage remains explicitly incomplete, even if the engine accepted the
turn. Match totals include both gameplay and chat and retain interrupted attempts.
Input usage adds the context processed by every model request within a game turn.
It is not the size of one prompt or a count of distinct game information. Cached
input is included in that total; uncached input is input minus cached input.
`modelRequests` counts distinct provider responses observed through cumulative
usage updates. After a model process closes, the service reconciles tokens and
the exact response-ID count from that thread's local Codex usage records. Only
usage records are read; conversation content is not copied into telemetry. A
missing or malformed trace leaves the live counters and incomplete label intact.

Codex control RPC rejections produce `codex_request_failed` records in the owning
player service's private `diagnostics.jsonl`. Gameplay, economy, military,
communication and review jobs also retain `requestFailure` in their lifecycle
record. Details identify the activation, purpose, RPC method and ID, numeric error
code, known error category, structured HTTP status when supplied, and available
thread/turn IDs. The provider message's length and SHA-256 fingerprint distinguish
repeated failures without copying free-form error text, request inputs, headers or
credentials. Error reporting drains during process shutdown even when a request
fails before the thread or model turn has been established.

Strategic reviewers have separate `review` jobs. Periodic reviews link to their
source gameplay job; opening reviews have no parent and count toward their owner's
turn 0. Every attempt's tokens, calls and duration count toward that source turn,
even if it finishes during a later turn. Player totals expose `reviewUsage`
separately from `gameplayUsage` and `chatUsage`. Provider usage and completeness
remain per job; a failed attempt is still counted. Snapshot capture calls are
service reads, recorded under the source gameplay job for periodic reviews.
Opening capture happens before gameplay and appears in host API accounting.

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

Local human turns have `controller: HUMAN`, their own elapsed/active timing and
`turn.nativeActions` counts (native screen/action categories, including End Turn).
They have no gameplay model job or token report to wait for. Native clicks are not
counted as MCP/API calls; any actual communication/API work retains its own counts.
Human turns become accounting-complete at handoff or a terminal action.

Usage often arrives after `END_TURN`. A committed turn can therefore initially
have `accountingComplete=false`. The service stores cumulative reports in a private
SQLite outbox outside game rollback snapshots, then sends them with increasing
sequence numbers. Duplicate and out-of-order deliveries cannot double charge a
job. Restart preserves the latest known spending, and reconnect retries unsent
reports. A failed monitoring write does not undo a committed game command: the
operator status exposes `telemetryWriteError`, and later commits/restart republish
the affected database rows from the checkpoint.

Winner selection and scoring come from Unciv. The score leader is not substituted
for a native winner. Budget exhaustion, interruption, and recovery create no winner.
Full telemetry and all-player scores remain operator data, outside player observations.

## Gameplay and finalization timing

`gameplayElapsedMs` ends when native handoff fences the job's game actions.
`finalizationElapsedMs` covers provider completion and durable service accounting,
excluding the scheduler's later event download. There is no post-turn note writer.
Another player may act during this interval. Periodic review jobs have their own
elapsed time; do not sum overlapping durations to infer wall time. See
[background reviews](../architecture/Background-turn-memory.md).

Delayed provider reports or an unfinished strategic review can leave the source
turn's accounting incomplete after gameplay has advanced. Completion, interruption
and unavailable usage remain distinguishable. Game shutdown stops remaining work
and flushes accounting, preserving the native result independently of advice.

## Model and host latency profiles

New agent services collect selected Codex OpenTelemetry timing histograms through
a per-process collector bound to loopback inside the service container. Only
allowlisted numeric histograms and bounded success/status attributes are retained
in the service's `telemetry/accounting.sqlite3`, in `llm_metrics`. Prompts,
messages, tool arguments/results, credentials, and arbitrary OTel attributes are
not recorded. Cumulative exports replace earlier samples; duplicate or reordered
exports cannot add the same spending or timing twice. Profiling does not change
native token accounting or game checkpoints. Periodic export limits loss on an
interrupted process, but the last unexported observations can still be missing.

Inspect one game or aggregate batch directories without contacting any service:

```sh
.venv/bin/civ-agent match profile --state-dir PATH --since-restart
.venv/bin/civ-agent match profile --state-dir BATCH_A BATCH_B --since-restart --json
```

The report keeps provider-reported inference time, API transport, time to first
token, full Codex turns, and tool latency separate. These are the runner's
[documented metrics](https://learn.chatgpt.com/docs/config-file/config-advanced#runtime-and-model-transport).
An HTTP request timer does not include the entire streamed response; a Codex turn
can include several model requests and tools. Provider inference measurements are
shown only when supplied by the upstream transport. No residual job time is
presented as measured inference. Counts, sums, means and histogram percentile
upper bounds are available by model and role in JSON output.

Host round trips come from service diagnostics, excluding idle communication
subscription reads. They include network and service overhead and have the
diagnostic files' bounded retention. Decision latency comes from classifier
records, with repeated advisory receipts deduplicated. Completed model-job elapsed
time is reported separately. Calls/jobs can overlap across roles, civilizations
and games: these cumulative timings are not percentages of match wall time.
`--since-restart` excludes cumulative histogram series that began before that
launch, since a histogram cannot be sliced accurately at an arbitrary timestamp.

Existing images and historical jobs have no retroactive LLM timing measurements.
Build a new runtime image and resume games with that image to begin collection;
older databases remain readable and missing measurements are marked unavailable.

## Partial memory rejections

API counters classify the whole tool response. A successful `memory_update`
response can contain individual `accepted: false` changes, which are not counted
as separate API errors. Inspect per-edit results when measuring recovery work.
The `memoryEdits` counters separately record accepted/rejected edits, rejected
batches and rejection reasons.

## Implementation

The native [telemetry store](../../src/main/kotlin/uncivai/Telemetry.kt) owns turn
and result records. [Service accounting](../../src/civ_agent/telemetry.py) keeps
job reports durable in SQLite; [database access](../architecture/Database-access.md)
describes connection and transaction ownership.
