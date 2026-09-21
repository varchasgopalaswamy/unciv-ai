# Prompt caching for player jobs

Status: implemented; automated checks and a matched three-round live benchmark
completed. The shared prefix remained stable and larger cache hits appeared late
in the run. Overall efficiency improved, with substantial savings from fewer reads.

## Why this pass

The nine-turn benchmark in `/tmp/unciv-call-reduction-live-20260920-1` spent
149,529 input tokens on the first request of each job, including 106,521 uncached
tokens. Later requests used 1,443,207 input tokens, including 226,951 uncached
tokens. Opening requests accounted for about 32% of all uncached input, despite
representing only nine of the 68 requests.

Our previous preload put changing turn instructions and private memory before
the core tool schemas. Codex also injected each job's different workspace path
before the preload. Both shortened the reusable prefix.

## Implemented layout

Scheduled gameplay and communication now use two separate messages:

1. **Developer instructions:** job workflow, discovery/recovery guidance, current
   core tool definitions and prepared-read interpretation instructions. This
   contains no civilization identity, turn, private memory or observation values.
2. **User input:** freshly read capabilities, the current request and selected
   private memory, followed by current observations and communication.

The service supplies the workflow through `thread/start` or `thread/resume`
`developerInstructions`. The scheduler sends the current civilization/turn
request. Core schemas still come from the permitted gateway catalogue. Canonical
JSON serialization keeps object-key order stable; changed schemas change the
prefix. Gameplay and communication retain different instructions and tools.

Game data stays at user priority, including capabilities, scenario text and other
players' messages. Preparation still runs through normal gateway reads and
evidence/accounting paths on every activation. This does not reuse an old game
snapshot or relax inspection requirements. Interactive clients also receive
prepared definitions before memory, while retaining their normal Codex context.

For scheduled jobs only, Codex omits environment/permission prose, local project
documents, and unrelated skill/plugin, app and collaboration instructions.
This removes volatile paths and irrelevant catalogues from the prompt. Actual
per-job directories, private credentials, workspace sandbox, approval policy,
player visibility checks and fresh conversations remain in force.

Audit histories retain the exact service-supplied developer and user text with
their respective roles, including mirrored histories. Activation `startup`
metadata records `layoutVersion`, `instructionsSha256`, `instructionCharacters`,
`userCharacters` and `totalPromptCharacters`. Its existing `characters` field
continues to measure just the prepared definitions/reads, excluding the workflow
and private memory. These are character counts, not tokenizer measurements or
proof of a provider cache hit.

## Codex limitation

OpenAI's [prompt-caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
requires a matching rendered prefix and usable cache boundaries. For GPT-5.6+
models, putting static instructions before a changing user message alone does
not guarantee a separate cache write at the static boundary; an explicit
breakpoint can preserve it. Implicit caching remains useful within a conversation.

Installed Codex **0.155.1** exposes `developerInstructions`, but its generated
app-server schema has no explicit cache-breakpoint setting. A local wire probe
also found that `thread/inject_items` drops `prompt_cache_breakpoint` from an
injected content block. We therefore do not claim explicit boundary support or
guaranteed reuse across jobs. The implementation preserves the existing provider
caching mode. A client version that forwards explicit markers would allow a
follow-up to mark the end of the reusable instructions while retaining implicit
caching for subsequent tool exchanges.

The real Codex capture test uses two separate processes and workspaces, changing
player context, and a local canned Responses endpoint. It verifies identical
tool/developer content before the changing user message, omitted workspace/local
file text, and unchanged model, effort, sandbox and approval settings. It does not
contact a paid model or measure cache hits:

```bash
CIV_TEST_NATIVE_CODEX="$(command -v codex)" .venv/bin/pytest -q -s tests/test_prompt_cache_native.py
```

## Live comparison method

Keep `gpt-5.6-luna`, medium reasoning, the same opening checkpoint, roster and
three-round limit. `tests/compare_vanilla_live.py` now reports `firstRequestUsage`
and `laterRequestUsage` separately for each job and in aggregate. Both include
uncached input and provider-reported cache-write tokens; missing write counts
remain `null`. Compare absolute uncached input, total input, request count and
elapsed time. Check that fresh turns actually reuse more of the prefix, rather
than interpreting ordinary within-turn reuse as an improvement across turns.

## Matched live results (2026-09-21)

Completed nine agent turns for Rome, Greece and France, alongside built-in Egypt
and two city-states. Both runs used `gpt-5.6-luna`, medium reasoning, Codex 0.155.1,
the same original opening checkpoint and fresh agent memory. The engine remained
at `f2408cde26543e404ea2844b18c1e648fa97691d`. The new run used uncommitted host
changes on top of `6d9c9d10e0f4e14a89a2d7a45c941150fde629d2`; source fingerprints
are retained in `cache-run-context.json`.

Baseline: `/tmp/unciv-call-reduction-live-20260920-1`.
New run: `/tmp/unciv-cache-live-20260921-1`.

| Metric | Baseline | New layout | Change |
| --- | ---: | ---: | ---: |
| Gameplay wall time | 525.0 s | 451.7 s | −14.0% |
| Model requests | 68 | 61 | −10.3% |
| Model-issued player-tool calls | 87 | 63 | −27.6% |
| Service-prepared reads | 36 | 36 | — |
| Total input tokens | 1,592,736 | 1,235,711 | −22.4% |
| Cached input tokens | 1,259,264 | 987,904 | — |
| Uncached input tokens | 333,472 | 247,807 | −25.7% |
| Output tokens, including reasoning | 14,166 | 12,141 | −14.3% |
| Reasoning tokens, included above | 5,811 | 4,253 | −26.8% |
| Opening-request input tokens | 149,529 | 136,608 | −8.6% |
| Opening-request uncached input | 106,521 | 105,376 | −1.1% |
| Later-request uncached input | 226,951 | 142,431 | −37.2% |
| Discovery-bearing requests | 13 | 12 | — |
| Rejected tool calls | 0 | 1 | — |
| Rejected memory changes | 1 | 0 | — |

### Cache reuse appeared late

All nine new jobs had identical service-supplied developer text: 24,991 characters,
SHA-256 `9e93db804c8405dc476802c01b09f6aee317f16a574d7a53df4afda66a787143`.
The provider traces matched that fingerprint and omitted the environment block.
The baseline had nine different developer prefixes, including workspace paths.

| Opening request | Input tokens | Cached tokens | Cached share |
| --- | ---: | ---: | ---: |
| Rome, turn 0 | 15,068 | 0 | 0% |
| Greece, turn 0 | 15,062 | 0 | 0% |
| France, turn 0 | 15,058 | 0 | 0% |
| Rome, turn 1 | 15,287 | 1,792 | 11.7% |
| Greece, turn 1 | 15,273 | 1,792 | 11.7% |
| France, turn 1 | 15,148 | 1,792 | 11.8% |
| Rome, turn 2 | 15,289 | 1,792 | 11.7% |
| Greece, turn 2 | 15,274 | 12,032 | 78.8% |
| France, turn 2 | 15,149 | 12,032 | 79.4% |

The last two openings reused substantially more input than their baseline's
5,888 cached tokens each. Across all nine openings, however, the cached share
fell from 28.8% to 22.9%; the smaller prompts left opening uncached input only
1.1% lower. This demonstrates larger late cache hits, without establishing
consistent reuse from the second job onward. The traces do not establish why
the larger hits started late. Explicit breakpoint support remains unavailable
through the tested Codex interface.

### Other contributors and validation

Agents reread research three times instead of nine, construction four times
instead of eight, and cities three times instead of six. Memory updates fell
from seven to three. France's turn 1 needed only an end-turn call because its
previous destination order was still running. These differences reduce calls
and repeated input independently of cache reuse. Different choices and provider
latency also affect this single comparison; the 14% time reduction is an observed
run result, not an isolated estimate of the caching change's effect.

All capitals were founded, research and production were queued, and nine native
handoffs completed. Successful commands numbered 29 versus 32; the new run had
fewer explicit movement commands. France tried a destination order after a
research change had invalidated its movement preview. The gateway returned
`DETAIL_REQUIRED`; the agent reread the preview and successfully retried. No
implementation change was made during gameplay.

Provider, service and host usage/call counts reconcile for all nine jobs, with
no incomplete accounting. All 20 new state deltas reconstruct correctly. Each
model-facing result was delivered once; all nine end-turn receipts remained
audited and intentionally withheld at handoff. The harness stopped at its
three-round limit and closed all four local listeners. Sleep was inhibited for
the benchmark. This remains an opening test; the native game has no winner yet.

The analysis also covered a one-request turn: zero later requests now report
zero later cache writes, while genuinely missing provider counters remain
`null`. The focused metric tests pass.

Artifacts include `opening-comparison.json`, `cache-analysis.json`,
`cache-run-context.json`, checkpoints, receipts and private histories in the new
run directory. The harness log is `/tmp/unciv-cache-live-20260921-1.run.log`.

```bash
.venv/bin/python -m tests.compare_vanilla_live \
  /tmp/unciv-call-reduction-live-20260920-1 /tmp/unciv-cache-live-20260921-1
```

A longer or repeated matched run is needed to measure how consistently the
larger opening cache hits persist beyond this brief startup period.
