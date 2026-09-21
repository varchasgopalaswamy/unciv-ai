# Agent prompt streamlining

The September 20, 2026 pass shortens the instructions supplied to gameplay and
communication jobs. It keeps the existing model (`gpt-5.6-luna`, medium), tool
schemas, visibility boundaries, action inspections, consent and recovery checks.

## Where instructions live

| Surface | Responsibility |
| --- | --- |
| Shared startup guidance | Selective discovery, structured intent, review flagged plans before reuse, save useful private memory, defer repeated stale-assumption conflicts and continue |
| Gameplay task | Ordered turn workflow: observe, review strategy, inspect and act, review and end the turn |
| Communication task | Read the message context, choose an appropriate diplomatic response, summarize |
| Service footer | Service-owned turn files and summaries; no calls after successful `END_TURN` |
| `player_turn` definition | The complete `stateUpdate` protocol, including matching bases, removals, inspection invalidation and recovery |
| Action/detail definitions | Applicable command shapes, previews, consequences and confirmations |
| `chat_read` definition | Asynchronous delivery and ambiguous, nonbinding social promises |
| `memory_update` definition | Record shapes, existing plan IDs and delivered evidence requirements |
| Error responses | Specific recovery reads for rejected actions; individual rejected memory changes also need the startup deferral guidance |

The startup lookups still fetch gameplay's `player_turn`, `player_capabilities`,
`victory_progress` and `chat_read`, or communication's `chat_read`, `chat_send`,
`memory_search` and `memory_update`. Thus the shared state/social instructions are
available before decisions that need them. Other definitions remain discoverable
as needed; no capabilities were removed.

The old blanket instruction to re-observe after commands conflicted with command
updates. The turn task now says to apply `stateUpdate`; its detailed protocol is
described once, by `player_turn`. Moving flagged-plan review ahead of action
selection also addresses the plan-reference retries seen in the
[previous opening run](Opening-efficiency-results.md#remaining-overhead).

Ordered task instructions and separate context follow the general structure in
the [OpenAI prompt engineering guide](https://developers.openai.com/api/docs/guides/prompt-engineering#message-formatting-with-markdown-and-xml).
This is a model-independent cleanup, not a model migration.

## Static size comparison

Counts use the rendered Rome turn-1 prompts, excluding appended private memory.
These are characters, not tokens; they do not measure model latency or total
conversation size.

| Text | Before | After | Reduction |
| --- | ---: | ---: | ---: |
| Scheduled gameplay startup | 5,401 | 2,915 | 46.0% |
| Communication startup | 4,365 | 2,152 | 50.7% |
| `player_command` description | 3,168 | 995 | 68.6% |
| Gameplay startup plus `player_turn`, `player_command`, `chat_read` descriptions | 9,921 | 5,770 | 41.8% |
| Communication startup plus its four bootstrap descriptions | 6,416 | 4,467 | 30.4% |

The gameplay subtotal includes the growth of `player_turn` from 902 to 1,257
characters, so it accounts for moving the update protocol out of startup.
Subtotals exclude tool schemas and other descriptions; they are not whole-request
token estimates.

## Live comparison

The first shortened-prompt run, `/tmp/unciv-prompts-live-20260920-1`, stopped after
five handoffs. France received two individual `STALE_ASSUMPTIONS` rejections while
editing a defense plan, then returned without ending its turn. The scheduler
paused and retained the checkpoint; no command outcome was lost or uncertain.

Removing all startup stale-assumption guidance was too aggressive: individual
memory-change rejections contain a reason, without the action-error recovery
instructions. The final prompt explicitly says to defer a repeatedly conflicting
action or memory change and continue other work. `memory_update` also explains
that these rejections call for rereading the record and its changed dependencies.
The original action-error guidance and all validation rules remain intact.

### Corrected three-round replay

The corrected run, `/tmp/unciv-prompts-live-20260920-2`, completed three rounds and
nine agent handoffs. It uses the exact original opening checkpoint and compares
against `/tmp/unciv-efficient-live-20260920-1`, the previous efficient run. Rome,
Greece and France use `gpt-5.6-luna` with medium reasoning; Egypt uses the built-in
AI, with two city-states present. Agents choose their own actions.

- Checkpoint SHA-256:
  `c771aff31eb4aff44bb100b1646531595cdd4aa4bd6053c63793f5f708d6fdc8`.
- Host repository revision: `413fea42ff2cfb530a9b4f1042a31e11ba1a3d3e`, with
  the five prompt-source files modified. `prompt-source.json` records all Python
  source hashes; none changed during the corrected run.
- Engine revision: `f2408cde26543e404ea2844b18c1e648fa97691d`.
- The run retains `prompt-text-before.json`, `prompt-text-after.json`, provider
  trace references, native receipts, checkpoints and `opening-comparison.json`.

The following table compares the two completed batches. It excludes startup,
automated checks and the stopped diagnostic attempt above.

| Measurement | Previous efficient run | Corrected prompts | Change |
| --- | ---: | ---: | ---: |
| Gameplay wall time | 759.6 s (12m 40s) | 777.1 s (12m 57s) | +2.3% |
| Sum of gameplay job durations | 756.9 s | 773.6 s | +2.2% |
| Model requests | 134 | 129 | −3.7% |
| Player-tool calls | 144 | 131 | −9.0% |
| Total input tokens | 2,740,493 | 2,539,926 | −7.3% |
| Cached input tokens | 2,339,840 | 2,144,000 | −8.4% |
| Uncached input tokens | 400,653 | 395,926 | −1.2% |
| Output tokens, including reasoning | 20,265 | 22,036 | +8.7% |
| Reasoning tokens, included above | 7,172 | 9,009 | +25.6% |
| Rejected tool calls | 6 | 5 | — |
| Rejected individual memory changes | 4 | 2 | — |
| Successful game commands, including handoff | 34 | 32 | — |
| Explicit overview reads | 9 | 9 | — |
| Host overview reads, including internal refreshes | 34 | 32 | — |
| Cumulative service tool handling | 8.431 s | 8.502 s | +0.8% |
| Native game-query/command handling | 1.268 s | 1.438 s | — |

The run shows modest reductions in input and calls, with slightly longer elapsed
time. Most input savings were cached context. Increased reasoning output and
variable model/API latency limit what can be inferred from shorter prompts;
these measurements cannot separate inference, provider queueing, network and
client overhead. Native handling is a subset of service handling, not additional
time. No negotiation waits occurred. Different strategic choices produced two
fewer successful commands, so this is not an identical-action experiment or
proof of a repeatable speed or cost improvement.

### Discovery and remaining retries

Discovered definition text fell from 158,883 to 150,850 characters, but names-only
index output grew from 938 to 8,033 characters. Discovery-bearing model requests
rose from 34 to 42; total discovery output barely changed (159,821 to 158,885
characters). Neither run repeated definitions or exceeded four definitions per
lookup. Fewer discovery round trips remain an opportunity; the large static
startup reduction did not translate into the same reduction in whole-request
input.

The five rejected calls in the corrected run were:

- A destination order issued before its movement preview.
- Founding before reading the settlement section.
- An alert acknowledgement using `handle` instead of `target`.
- `deferReason` nested inside `intent` instead of at the command's top level.
- A plan update containing an unsupported `plan` field.

All recovered. The two individually rejected memory changes referenced unread
evidence or stale assumptions. France continued through the latter rejection and
completed its turn. No gameplay action was rejected for a stale plan reference,
compared with six such failures in the baseline. These are single-run observations,
not a guarantee that those failures cannot recur.

### Per-turn comparison

Values show previous efficient run → corrected prompts. Turns use the engine's
zero-based numbers.

| Turn | Player | Job seconds | Input tokens | Model requests | Tool calls |
| --- | --- | ---: | ---: | ---: | ---: |
| 0 | Rome | 101.2 → 116.6 | 419,028 → 318,338 | 20 → 16 | 22 → 18 |
| 0 | Greece | 118.4 → 87.8 | 329,209 → 398,297 | 16 → 19 | 18 → 21 |
| 0 | France | 99.9 → 115.4 | 321,880 → 490,199 | 16 → 24 | 18 → 21 |
| 1 | Rome | 65.7 → 60.2 | 181,034 → 208,778 | 10 → 11 | 12 → 12 |
| 1 | Greece | 59.5 → 85.5 | 234,885 → 284,089 | 12 → 14 | 12 → 13 |
| 1 | France | 73.7 → 84.1 | 336,415 → 174,025 | 16 → 10 | 17 → 12 |
| 2 | Rome | 83.5 → 66.9 | 326,665 → 175,172 | 15 → 10 | 15 → 10 |
| 2 | Greece | 88.1 → 91.3 | 349,973 → 246,827 | 16 → 12 | 17 → 12 |
| 2 | France | 67.0 → 65.7 | 241,404 → 244,201 | 13 → 13 | 13 → 12 |

### Validation and reproduction

Provider usage/settings, service records and all nine host turn reports reconcile.
All **131 printed responses match their audit records**, without duplication or
truncation. All **23 deltas reconstruct the saved permitted snapshots**; nine
final calls are successful `END_TURN` with `turn_ended` updates and no later player
calls. Delta bodies total 33,382 characters versus 80,484 for equivalent full
updates. Every job used one explicit overview. All four service ports were closed
after the harness stopped at its round budget. The game is unfinished, with no
winner; this does not close full-game acceptance.

The initial prompt pass passed 154 Python tests plus 22 subtests. After restoring
the stale-assumption guidance, 49 relevant tests plus 22 subtests passed again.
Ruff lint/format, Pyrefly and diff checks passed. No package build was run.

```sh
.venv/bin/python -m tests.live_vanilla \
  --state-dir /tmp/unciv-prompts-live-20260920-2 \
  --source-state /tmp/unciv-efficient-live-20260920-1 \
  --checkpoint /tmp/unciv-vanilla-live-20260920-1/round-checkpoints/gt1h0xdqeamas-round-0.json \
  --port 8985 --max-turns 3 --max-invocations 36 --max-seconds 3600 \
  --turn-timeout 600 --startup-timeout 900

.venv/bin/python -m tests.compare_vanilla_live \
  /tmp/unciv-efficient-live-20260920-1 /tmp/unciv-prompts-live-20260920-2
```

Use an unused state directory when repeating the run. The source-state directory
supplies the same checkpoint credentials as the original batch; the checkpoint
itself is the unchanged original opening.
