# Three-round opening efficiency comparison

The later [prompt streamlining comparison](Prompt-streamlining.md#corrected-three-round-replay)
uses this report's efficient run as its baseline.

## Setup

The September 20, 2026 comparison replays the original opening checkpoint for
three full rounds: nine gameplay jobs controlling Rome, Greece and France, with
native Egypt and two city-states. Every model job uses `gpt-5.6-luna` with medium
reasoning. The agents choose their own actions.

- Original batch: `/tmp/unciv-vanilla-live-20260920-1`.
- Current batch: `/tmp/unciv-efficient-live-20260920-1`.
- Initial checkpoint SHA-256:
  `c771aff31eb4aff44bb100b1646531595cdd4aa4bd6053c63793f5f708d6fdc8`.
- Current service revision: `413fea42ff2cfb530a9b4f1042a31e11ba1a3d3e`;
  service source was unchanged during the run.
- Engine revision: `f2408cde26543e404ea2844b18c1e648fa97691d`.

This compares the combined efficiency changes against the original batch:
selective discovery, single-payload responses, focused observations, copyable
command arguments, and [deterministic command updates](Command-state-updates.md).
It does not isolate the effect of any one change. The exact map and opening
positions match, but later choices, game randomness and model latency can differ.

## Results

Both runs completed three rounds and nine handoffs, with all nine jobs using the
requested model/settings and complete reconciled accounting. The rerun stopped
at the requested round limit and shut down its services. The game remains
unfinished; this opening benchmark does not close full-game acceptance.

| Measurement | Original batch | Current batch | Reduction |
| --- | ---: | ---: | ---: |
| Gameplay wall time | 971.7 s (16m 12s) | 759.6 s (12m 40s) | 21.8% |
| Sum of gameplay job durations | 968.1 s | 756.9 s | 21.8% |
| Mean duration per agent turn | 107.6 s | 84.1 s | 21.8% |
| Model requests | 163 | 134 | 17.8% |
| Player-tool calls | 208 | 144 | 30.8% |
| Explicit overview reads | 34 | 9 | 73.5% |
| Host overview reads, including internal refreshes | 34 | 34 | 0.0% |
| Total input tokens | 6,156,532 | 2,740,493 | 55.5% |
| Cached input tokens | 5,618,432 | 2,339,840 | 58.4% |
| Uncached input tokens | 538,100 | 400,653 | 25.5% |
| Output tokens, including reasoning | 25,710 | 20,265 | 21.2% |
| Reasoning tokens, included above | 8,741 | 7,172 | 17.9% |
| Rejected tool calls | 13 | 6 | 53.8% |
| Rejected individual memory changes | 10 | 4 | 60.0% |
| Successful game commands, including handoff | 32 | 34 | — |
| Cumulative service tool handling | 12.046 s | 8.431 s | 30.0% |
| Native game-query/command handling | 1.533 s | 1.268 s | 17.3% |

Each rerun turn used exactly one explicit overview. The service maintained fresh
state with 25 internal refreshes: the total number of host overview reads stayed
at 34. All **25 deltas reconstructed the saved permitted state exactly**; nine
final actions returned `turn_ended`. Delta bodies totaled 36,810 characters versus
86,237 for equivalent full updates, a **57.3% reduction**.

All **144 printed responses matched the audit records**, with no duplicated
payloads or truncated output. One response was printed directly as structured
JSON; the rest used the single-payload MCP wrapper. The original batch had 11
truncated output blocks and 138 duplicated payloads in its captured envelopes.
Only 166 of its 208 audited responses were found as complete printed JSON, so
printed-character totals alone would undercount the original batch.

The response verifier now recognizes directly printed payloads and JavaScript's
equivalent integral-number formatting (`0.0` becomes `0`). It still distinguishes
booleans from numbers and rejects mismatched values. Regression coverage for
these cases passed, as did Ruff, formatting and Pyrefly.

### Per-turn comparison

Values show original → current. These are the engine's zero-based turn numbers.

| Turn | Player | Job seconds | Input tokens | Model requests | Tool calls |
| --- | --- | ---: | ---: | ---: | ---: |
| 0 | Rome | 150.4 → 101.2 | 1,193,434 → 419,028 | 26 → 20 | 32 → 22 |
| 0 | Greece | 159.8 → 118.4 | 1,182,498 → 329,209 | 27 → 16 | 35 → 18 |
| 0 | France | 122.3 → 99.9 | 849,356 → 321,880 | 23 → 16 | 27 → 18 |
| 1 | Rome | 155.9 → 65.7 | 793,735 → 181,034 | 21 → 10 | 27 → 12 |
| 1 | Greece | 40.7 → 59.5 | 217,396 → 234,885 | 8 → 12 | 13 → 12 |
| 1 | France | 83.3 → 73.7 | 324,454 → 336,415 | 11 → 16 | 15 → 17 |
| 2 | Rome | 83.6 → 83.5 | 665,221 → 326,665 | 19 → 15 | 23 → 15 |
| 2 | Greece | 60.0 → 88.1 | 279,077 → 349,973 | 10 → 16 | 12 → 17 |
| 2 | France | 111.9 → 67.0 | 651,361 → 241,404 | 18 → 13 | 24 → 13 |

The aggregate improvement did not apply equally to every turn. For example,
Greece's last turn included new city-state contact and an acknowledgement. The
agents executed 34 successful commands overall, compared with 32 before; their
movement and production choices differed.

### Remaining overhead

Most elapsed time remains outside service tool handling, in the model/API loop.
The 8.4 seconds spent handling tools includes the 1.3 seconds of native work;
these times must not be added together. No negotiation waits occurred.

All six rejected tool calls involved stale private-plan references, and all
recovered. There were no command-argument or state-refresh failures. Four
individual memory changes were rejected separately: two stale assumptions and
two references to evidence not yet read. Reviewing flagged plans before attaching
them to actions, and using only delivered evidence, remain opportunities to
reduce retries. This run made no changes to those safeguards.

## Measurement

The comparison reconciles each job's provider usage and settings with service
accounting and host turn reports. Model requests and player-tool calls are
separate counts: one model request can issue several player-tool calls. Input
tokens are summed across model requests, including repeatedly cached context.
Reasoning tokens are included in output tokens.

Job duration includes the model/API interaction and service work. Cumulative tool
handling measures time inside the service's player-tool calls; native host handling
is a subset of that time. Background communication long polls are excluded from
the native processing total. The remainder cannot be split reliably into inference,
network, provider queueing and client runtime with the current instrumentation.

The gameplay wall interval runs from the first gameplay job's start to the last
job's completion. It excludes service startup. The checkpoint retains its original
match creation time and provenance, so its lifetime `game-result.elapsedMs` is not
used as this rerun's elapsed time. `live-run.json` records the current service
revision separately.

## Reproduction

Use an empty state directory and four available consecutive ports:

```sh
.venv/bin/python -m tests.live_vanilla \
  --state-dir /tmp/unciv-efficient-live-20260920-1 \
  --source-state /tmp/unciv-vanilla-live-20260920-1 \
  --checkpoint /tmp/unciv-vanilla-live-20260920-1/round-checkpoints/gt1h0xdqeamas-round-0.json \
  --port 8985 --max-turns 3 --max-invocations 36 --max-seconds 3600 --turn-timeout 600

.venv/bin/python -m tests.compare_vanilla_live \
  /tmp/unciv-vanilla-live-20260920-1 /tmp/unciv-efficient-live-20260920-1
```

The comparison writes `opening-comparison.json` in the new run directory. It
includes per-job counters, errors, response audit comparisons and state-update
reconstruction. It exports no credentials or raw reasoning. Fresh checkpoint
runs require an opening with no prior jobs or handoffs, preserving the old run's
files and accounting.
