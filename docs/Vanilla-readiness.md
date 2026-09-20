# Vanilla full-game readiness and accounting

## Accepted scope

Audit remaining ordinary turn blockers and planning information; run a normal
Ancient-start game and fix failures exposed by play. Preserve native Unciv rules,
player visibility, nonbinding social promises, and checkpoint recovery. Agent
strategy mistakes are recorded separately from API failures.

The live match uses Rome, Greece, and France as external players and Egypt as a
built-in AI. Two native city-states are enabled. Setup is ordinary Vanilla,
Quick/Prince, Small Pangaea, Ancient era, with all five native victory types.
All gameplay and chat model jobs use `gpt-5.6-luna`, medium reasoning. There are no
prepared milestones or privileged gameplay interventions.

## Accounting contract

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

## Live batches

The opt-in wrapper starts the host and all three services, runs the ordinary
scheduler, and shuts services down in reverse order. It issues no game commands.
For an initial three-round batch:

```sh
.venv/bin/python -m tests.live_vanilla --state-dir /tmp/unciv-vanilla-live-1 \
  --port 8971 --max-turns 3 --max-invocations 36 --max-seconds 3600 --turn-timeout 600
```

Resume with the same directory and `--resume`, increasing cumulative limits as
needed. An eventual full Quick game permits 331 rounds; elapsed-time and invocation
limits must allow the actual accumulated work. Preserve the original checkpoint
for exact reproduction: the terrain seed alone does not determine every setup choice.

### First live batch: input-token diagnosis

The September 20 run at `/tmp/unciv-vanilla-live-20260920-1` exposed substantial
context growth. Rome's opening turn made 32 player-tool calls across 26 model
requests. Summing the provider's individual usage records exactly reproduces the
host's 1,193,434 input tokens, of which 1,113,600 were cached and 79,834 uncached.
The first model request used 8,872 input tokens and the last used 64,798; the
average was 45,901. This is accumulated request usage, not duplicate telemetry
reports being added together.

The recorded model inputs identify several avoidable contributors:

- Broad tool discovery printed 34 catalogue entries, totaling 35,511 characters.
  The next request's input grew by 8,566 tokens, including that discovery result
  and the intervening model output.
- The agent printed whole MCP result envelopes. These contain the same game JSON
  in `content[].text` and `structuredContent`. For example, one Warrior detail
  appeared as 18,771 characters rather than 8,857 for its structured payload alone.
  Audit history already removes this duplicate; that does not change live inputs.
- `research_details` returned all 74 technologies (29,619 characters), including
  future eras. Rome also read `player_turn` six times and `unit_details` five times.
  Large result batches were truncated by the model's tool-output limit.
- Three rejected commands required recovery: stale founding details, an incorrect
  research-queue argument, and missing named construction details.

These character counts measure recorded payloads, not exact token attribution to
each component. The largest opportunities are emitting each result once, concise
tool discovery, focused detail queries, and fewer redundant refresh/retry cycles.
Any change must preserve player-visible information, native legality, and reliable
observation of command results. A longer playthrough should measure improvements
against this baseline.

## Acceptance

- Automated coverage: real HTTPS rejected/accepted/replayed calls; cached service
  tools; late, repeated and stale usage reports; missing usage; interruption and
  restart; recovery without usage refunds; a native AI turn; complete player scores;
  native winner differing from the score leader; export reconstruction.
- Live acceptance: unscripted Ancient start through native victory/defeat, with
  checkpoint/restart, model settings verified, accounting reconciled, and blockers
  diagnosed. This remains in progress until the recorded match actually finishes.
