# Reduce agent requests and turn latency

Status: implemented; automated validation and the matched live measurement passed.
This is a new five-part follow-up to
[prompt streamlining](Prompt-streamlining.md), with its own numbering. The
[original efficiency tasks](Agent-efficiency.md) are already implemented.

## Evidence and objective

The corrected three-round opening used 129 model requests and 131 player-tool
calls for nine completed turns. The trace contains 42 discovery-only executions,
36 routine opening reads (overview, capabilities, victory and communication),
44 detail/map reads, 36 commands and 15 memory calls. Nine model responses only
reported the completed turn. Capabilities were identical across all nine turns.
Read batching already occurred in 21 executions, so simply recommending parallel
reads does not address all of this overhead.

Reduce discovery, redundant observations, avoidable retries and end-of-turn
requests. Preserve player visibility, native rules, explicit consequential
decisions, reliable receipts, accounting and recovery. Keep all game agents on
`gpt-5.6-luna` with medium reasoning. No engine change or package build is planned.

## 1. Supply a small core of tool definitions at startup

- Supply the actual gateway definitions for `player_turn`, `player_command` and
  `memory_update` in gameplay startup context, including argument schemas. Do not
  maintain a second hand-written schema. Give their callable names explicitly.
- Keep the rest of the catalogue selectively discoverable. Fetch only definitions
  needed for the next decision; avoid looking up core definitions again.
- Give communication and interactive jobs an appropriate, permitted core too.
  Recovery starts with current definitions, never stale saved tool metadata.
- Measure injected characters alongside discovery output; moving text into the
  prompt does not make it free. Confirm the initial discovery round trip can be
  skipped while later tools remain available.

## 2. Prepare the initial decision context

- Combine the current permitted overview, versioned capabilities, victory status,
  communication and selected private memory into prepared startup context.
  Prefer service preparation before the first model request; retain explicit
  reads for recovery and later changes.
- Route preparation through the same observation, evidence, privacy and audit
  paths as ordinary reads. A delivered startup overview must establish the same
  state-update base and inspection prerequisites as `player_turn`.
- Reuse capabilities only with an explicit content/version identity. Treat game
  observations and communication as fresh reads, and preserve full warnings,
  pending offers and ambiguous promises. Do not let compaction silently drop
  information needed for decisions.
- Label service preparation separately from model-issued tool calls. Preserve
  underlying host API counts and evidence of exactly what reached the model.
- Do not use a prepared snapshot from a prior activation or skip required detail
  inspections. Handle unavailable startup reads explicitly without inventing state.

## 3. Inspect details when a decision needs them

- Base the turn workflow on pending decisions and the current roster. Inspect
  relevant units/cities and named research/construction candidates when choosing
  an action; keep broader planning available on demand.
- Do not routinely compare construction alternatives while a city's production
  continues unchanged. Reconsider when completion, threats, resources or strategy
  warrant it. This is advice, not a restriction on the agent's choices.
- Reuse current inspections and apply command state updates. Refresh only needed
  invalidated details. Preserve mandatory native-action inspections and previews.
- Verify continued production needs no construction read while a changed queue,
  settlement, movement or consequential purchase still gets its required read.

## 4. Make the next command and required inspection explicit

- Extend validated copyable arguments to alert acknowledgement and turn completion.
  Use `target` for alert handles and top-level `deferReason` for end-turn deferral.
- Point units/actions at the exact required settlement detail or movement preview
  before issuing a command. Error recovery should identify those reads precisely.
- Keep intent and confirmations as the agent's decision. Templates must not accept
  consequences, dismiss warnings or fabricate a tactical deferral explanation.
- Test actual template execution, required inspection rejection and successful
  recovery; measure remaining malformed calls in a subsequent live comparison.

## 5. Finish gameplay at the handoff; overlap bookkeeping

- Save only useful changes to plans, beliefs or durable notes. Routine facts and
  action narration already exist in observations and receipts.
- On confirmed native `END_TURN`, durably retain the receipt/handoff and fence the
  outgoing job's gameplay tools. End the model job without asking for a prose-only
  summary; service summaries continue to come from receipts and typed memory.
- Persist pending finalization before activating another player. Archive rendering,
  history mirroring and final accounting may finish while that player acts.
- Background work consumes the outgoing turn's saved evidence. It must not read
  another player's live state or blindly replace memory changed by communication.
  Required finalization finishes before that civilization's next activation.
- Make pending work retryable and idempotent after restart, tied to its original
  epoch/activation and confirmed handoff. Later global handoffs cannot invalidate
  its proof; rollback cannot allow an old job to publish into a new epoch.
- Record gameplay time separately from post-turn finalization time. Keep all token
  and API usage attributed to its original job; never mark partial provider usage
  complete. Drain remaining records at game completion, including a winning action
  that finishes the game without `END_TURN`.
- Test overlap with a deliberately delayed finalizer, same-player ordering,
  shutdown/restart, rollback, duplicate finalization, missing usage and native win.

Overlap reduces elapsed time. Removing a summary-only request reduces request and
token counts. These are separate benefits and must be measured separately.

## Delivery and validation

Implement in dependency order: startup definitions/context (1–2), need-driven
inspection and command guidance (3–4), then handoff and background finalization (5).
Keep each change reviewable and retain the existing recovery contract throughout.
Bounded multi-action command batches are a later, separate proposal.

Run relevant Python regression tests, Ruff and Pyrefly. No package build is needed.
After implementation, a separate three-round live comparison should use the same
opening checkpoint and all four major civilizations, preserving prior artifacts.
Report model requests, player and host calls, prepared reads, discovery and startup
characters, token categories, retries, completed native actions, gameplay duration
and finalization duration. Check all receipts, update reconstructions and usage
totals. One live pair cannot establish a stable speedup or full-game readiness.

## Progress

- [x] 1. Core definitions at startup.
- [x] 2. Prepared initial context.
- [x] 3. Need-driven detail inspection.
- [x] 4. Action templates and precise inspection guidance.
- [x] 5. Handoff completion and background bookkeeping.
- [x] Automated validation.
- [x] Matched live measurement.

### Implementation details

- Startup uses current gateway schemas and permitted observations. Gameplay gets
  four prepared reads; communication gets only its chat context and permitted
  core. Preparation establishes normal evidence and the command-update base.
  Capabilities are freshly read and content-versioned for each activation; there
  is no cross-activation capability cache. Startup characters and `startup:`
  calls are recorded separately from model-issued calls.
- Guidance now makes detail reads decision-dependent and memory writes conditional
  on useful changes. Overview options provide validated acknowledgement/end-turn
  arguments; settlement and movement options point to the required inspections.
  These changes preserve required inspection checks. Actual model adherence to
  the guidance remains part of the live measurement.
- Scheduled gameplay stops at the native handoff. The gateway retains the receipt,
  prevents further gameplay calls, and holds the end-turn response while the
  service interrupts and stops the model. Its audit explicitly labels the
  withheld response `deliveredToModel: false`. Provider usage is complete only
  when the terminal notification and usage evidence support it. Interactive jobs
  retain their human-facing final response.
- The scheduler persists `pendingFinalizations` before another player starts.
  Workers render and mirror archives from the outgoing job's saved evidence and
  frozen typed summary. They do not run a reflection model or issue gameplay
  reads. Finalization must finish before that civilization acts again, before a
  round checkpoint, and on normal shutdown or game completion. Retries retain
  the original handoff proof; rollback clears abandoned work.
- Gameplay timing ends at the handoff. `finalizationElapsedMs` measures from the
  handoff through service archive finalization, excluding the subsequent
  scheduler-side archive copy. Existing job and native-turn timings remain.
  Finalization can overlap another player's gameplay, so these durations must
  not be summed to infer game wall time. A finished-game restart can reconcile
  pending accounting without starting chat or a model.

### Automated validation

The full Python suite passed with 532 tests and 90 subtests. After final cleanup,
141 relevant tests and 17 subtests passed again; the four handoff/accounting tests
also passed after adding storage-failure and lost-acknowledgement coverage.
Kotlin telemetry and input-schema integration tests passed. Ruff lint/format,
Pyrefly and diff checks passed.

Coverage includes real HTTPS handoff/interruption, retained usage and receipts,
overlap with another player's activation, same-player ordering, recovery without
replaying gameplay, epoch rejection, startup visibility/evidence, required
inspections, partial accounting, and a native win without `END_TURN`.

The Codex integration uses the existing app-server lifecycle. Consult the
[official app-server documentation](https://developers.openai.com/codex/app-server)
for `turn/interrupt` and completion notifications; an interruption request alone
does not establish completed provider accounting or process shutdown.

## Matched live benchmark (2026-09-20)

Completed three rounds: nine agent turns for Rome, Greece and France, with native
Egypt and two city-states. All model jobs used `gpt-5.6-luna` with medium reasoning.
The baseline is `/tmp/unciv-prompts-live-20260920-2`; this run is
`/tmp/unciv-call-reduction-live-20260920-1`. Both started from the same original
opening checkpoint, with fresh agent memory. The engine remained at
`f2408cde26543e404ea2844b18c1e648fa97691d`. This run exercised the uncommitted host
changes on top of `bbaed4d055f6badd72fd1b8e44d55d471f6d372d`.

| Metric | Previous prompts | Call reduction | Change |
| --- | ---: | ---: | ---: |
| Gameplay wall time | 777.1 s | 525.0 s | −32.4% |
| Model requests | 129 | 68 | −47.3% |
| Model-issued player-tool calls | 131 | 87 | −33.6% |
| Service-prepared reads | 0 | 36 | — |
| Player-tool calls including preparation | 131 | 123 | −6.1% |
| Discovery-bearing requests | 42 | 13 | −69.0% |
| Final prose responses | 9 | 0 | — |
| Input tokens | 2,539,926 | 1,592,736 | −37.3% |
| Cached input tokens | 2,144,000 | 1,259,264 | −41.3% |
| Uncached input tokens | 395,926 | 333,472 | −15.8% |
| Output tokens, including reasoning | 22,036 | 14,166 | −35.7% |
| Reasoning tokens, included above | 9,009 | 5,811 | −35.5% |
| Rejected tool calls | 5 | 0 | — |
| Rejected individual memory changes | 2 | 1 | — |
| Successful game commands, including handoff | 32 | 32 | — |
| Explicit model overview reads | 9 | 0 | — |
| Host overview reads, including internal refreshes | 32 | 32 | — |
| Service tool handling, including preparation | 8.502 s | 6.416 s | −24.5% |
| Native game-query/command handling | 1.438 s | 1.432 s | — |

Gameplay wall time runs from the first gameplay job's start through the last
job's completion; it excludes launching/building services. Model jobs total
522.4 seconds, of which 522.3 seconds precede handoff. Prepared context totals
259,706 characters across nine jobs and is included in provider token counts.
It is not free context, and the 36 prepared reads still incur host/service work.

### What changed in the traces

- Discovery output fell from 158,883 to 43,696 characters, with no names-only
  index reads or repeated definitions in this run. The actual core schemas were
  already in startup context. All nine jobs used their prepared overview,
  capabilities and victory status without requesting them again; one later chat
  read occurred.
- All nine jobs stopped at native handoff without a final prose response. Their
  last player call was successful `END_TURN`; no player calls followed it.
  Memory calls fell from 15 to seven. One Greece memory revision was rejected
  with `STALE_ASSUMPTIONS`; Greece continued and completed its turn.
- Detail/map reads rose from 44 to 47. In particular, all nine turns inspected
  the four opening research choices, although research queues were only set on
  the first round. Construction details remained at eight reads: six in the
  opening round and two later named reads for ongoing Monument production.
  The new guidance has not eliminated routine detail reinspection.
- Service finalization took 2.416 seconds in total, 0.163–0.425 seconds per turn.
  Every service archive finished before the next model job started, so this
  opening batch provides no measured benefit from overlapping slow archives.
  The delayed-finalizer regression test establishes that overlap works; the
  live speed improvement coincides with fewer model requests. Native handling
  remained about 1.4 seconds for the whole batch.

These are one pair of runs with different strategic choices, even though the
number of successful commands matched. They do not establish a repeatable
32% speed improvement, isolate each optimization's contribution, or prove
full-game readiness.

### Per-turn comparison

Values show previous prompts → call reduction. Turn numbers are zero-based;
job duration includes model startup and shutdown, but excludes later archive
finalization in the new implementation.

| Turn | Player | Job seconds | Model requests | Model-issued tool calls | Input tokens |
| --- | --- | ---: | ---: | ---: | ---: |
| 0 | Rome | 116.6 → 85.8 | 16 → 12 | 18 → 14 | 318,338 → 287,954 |
| 0 | Greece | 87.8 → 79.4 | 19 → 15 | 21 → 18 | 398,297 → 375,304 |
| 0 | France | 115.4 → 96.9 | 24 → 11 | 21 → 14 | 490,199 → 248,544 |
| 1 | Rome | 60.2 → 20.6 | 11 → 4 | 12 → 6 | 208,778 → 82,513 |
| 1 | Greece | 85.5 → 52.4 | 14 → 7 | 13 → 8 | 284,089 → 167,771 |
| 1 | France | 84.1 → 37.5 | 10 → 3 | 12 → 6 | 174,025 → 57,943 |
| 2 | Rome | 66.9 → 35.4 | 10 → 4 | 10 → 6 | 175,172 → 85,761 |
| 2 | Greece | 91.3 → 70.1 | 12 → 7 | 12 → 9 | 246,827 → 180,687 |
| 2 | France | 65.7 → 44.2 | 13 → 5 | 12 → 6 | 244,201 → 106,259 |

### Validation and reproduction

Provider per-request usage, service accounting, scheduler totals and all nine
host turn reports reconcile with complete accounting. The 78 model-delivered
responses match their durable audits without duplication or truncation. The
nine withheld handoff responses are separately audited. All 23 deltas reconstruct
the saved permitted snapshots: 34,766 characters versus 94,624 for equivalent
full updates. The scheduler stopped at its three-round budget with nine handoffs,
no in-flight activation and no pending finalization. All four service ports closed.
The game remains unfinished, with no winner.

`opening-comparison.json` contains the reconciled totals and per-job metrics;
`opening-analysis.json` records discovery, reads, memory rejections and host-call
breakdowns. Both are retained in the run directory alongside checkpoints,
receipts and service histories. The harness log is
`/tmp/unciv-call-reduction-live-20260920-1.run.log`.

```sh
.venv/bin/python -m tests.live_vanilla \
  --state-dir /tmp/unciv-call-reduction-live-20260920-1 \
  --source-state /tmp/unciv-prompts-live-20260920-2 \
  --checkpoint /tmp/unciv-vanilla-live-20260920-1/round-checkpoints/gt1h0xdqeamas-round-0.json \
  --port 8985 --max-turns 3 --max-invocations 36 --max-seconds 3600 \
  --turn-timeout 600 --startup-timeout 900

.venv/bin/python -m tests.compare_vanilla_live \
  /tmp/unciv-prompts-live-20260920-2 /tmp/unciv-call-reduction-live-20260920-1
```

Use a fresh state directory for another run. The source-state supplies credentials
for the unchanged opening checkpoint; it does not import the baseline's agent
memory or later game state.
