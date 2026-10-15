# Decisions memory/review triage trial — 2026-10-14

The implementation delivers native suggestions directly to the gameplay model.
`native_advice_enabled` is independent of `decisions_enabled`. Decisions handles
diplomacy routing, memory relevance/preservation, belief notices and early-review
triage. It receives no native option list and generates no gameplay selections
or strategic priorities. The broker rejects the removed gameplay task.

## Local verification

- Full Python suite: 1,458 passed, 11 opt-in checks skipped, 76 subtests passed.
- Final focused memory, broker, native-delivery, review and recovery checks:
  171 passed after the final eligibility/recovery adjustments.
- Ruff lint and formatting passed; Pyrefly reported zero errors.
- Focused Kotlin `NativeAdviceIntegrationTest`, `PlayerSummaryJsonTest` and
  `InputSchemaTest` passed using the selected engine checkout. This work adds no
  engine edits; pre-existing workspace changes were preserved.
- Tests cover independent flags, direct native options with unchanged mechanics,
  legal unrecommended actions, no paid calls for native reads/actions, unchanged
  explicit notebook writes, old active memory retrieval, version-pinned notices,
  non-recursion, stale results, review barriers/cooldowns/anchors, concurrent budget
  reservations, unknown usage, lost replies, restart, rewind, credential rotation
  and accounting-authority changes.

## Budgeted live classifier trial

The trusted local broker evaluated seven synthetic, provenance-labeled cases
using `gpt-6-luna`, task policy `player-advisories-v1`, confidence threshold 0.90,
five-second timeout and no provider retries. An isolated immutable trial account
had a $0.005 local estimated ceiling. No real game's allowance was used and no
gameplay or player-model job was started.

| Case | Questions | Accepted answers | Fallback |
| --- | ---: | ---: | --- |
| Attributed unresolved question | 1 | 1 | None |
| Routine acknowledgment | 1 | 0 | Low confidence |
| Old active memory relevance | 1 | 0 | Low confidence |
| Belief versus new visible evidence | 1 | 1 | None |
| Belief versus unrelated evidence | 1 | 1 | None |
| Material change review triage | 1 | 0 | Low confidence |
| Coalesced independent memory tasks | 3 | 2 | Low confidence on one question |

All seven case submissions produced a provider usage record: seven calls for
seven deliberately eligible synthetic cases. The coalesced case used one call
for three independent questions. This is not a measurement of call frequency in
normal play.

Validated usage was 2,674 input tokens and zero output tokens. At the verified
[standard Decisions input rate](https://developers.openai.com/api/docs/guides/decisions#pricing-and-availability),
the recorded estimated cost was **$0.0002674**. The broker ledger settled all
seven requests, with zero outstanding reservations and zero unknown completions.
The limit remains a local estimate rather than an invoice guarantee.

Five of nine labeled questions had accepted answers, all matching their labels.
The remaining four fell below the configured threshold. Four batches reported a
low-confidence fallback; the coalesced batch retained its two usable answers.
These results support contract/fallback operation on a small synthetic sample,
not a claim of classifier quality in normal play or stronger gameplay. Native
recommendation use was not measured because no gameplay was executed.

## Offline reproduction

The [case fixture](../../../tests/fixtures/decisions-advisory-replay.json) retains
the exact frozen evidence and labels. The
[captured broker answers](../../../tests/fixtures/decisions-advisory-responses-2026-10-14.json)
retain typed accepted choices, usage and fallback metadata. Low-confidence
choices are omitted by the broker; they are not fabricated during replay.

```sh
uv run python -m civ_agent.advisory_replay \
  tests/fixtures/decisions-advisory-replay.json \
  --responses tests/fixtures/decisions-advisory-responses-2026-10-14.json
```

Live diplomacy replay, container acceptance with a freshly built image and
matched gameplay evaluation remain separate rollout work. Active benchmarks were
not restarted or reconfigured.
