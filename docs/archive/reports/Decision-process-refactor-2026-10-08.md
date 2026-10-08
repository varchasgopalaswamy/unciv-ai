# Decision-process refactor — October 8, 2026

The first implementation closes the review–choice–action–feedback loop and has
completed native saved-position trials. It does not yet establish better play
against the built-in AI. The [design](../../architecture/Decision-process-refactor.md)
and [comparison procedure](../../scenarios/Strategic-positions.md#decision-process-comparisons)
define the remaining acceptance gates.

## Preserved baseline

The four existing `benchmarks/all-luna` games are retained in
`build/decision-baseline-2026-10-07`. Their fixed recovery boundaries are g0001
turn 175, g0002 turn 200, g0003 turn 231 and g0004 turn 191. Game 2 advanced after
the [audit](Benchmark-agent-play-audit-2026-10-07.md), which used its turn 180
comparison. All 20 player-memory histories verified against their copied recovery
boundaries. Online SQLite backup includes committed WAL data; checkpoint hashes,
runtime images, model settings, prompt evidence and source revisions accompany
the private bundle. It contains credentials and stays outside Git.

Six prepared positions cover active conversion, urgent military pressure, a large
treasury, an unresolved military operation, Greece's successful culture position
and a different held-out start. Native load/reload validation passed for all six.
The fixture preparation needed to discard stale observation callbacks belonging
to former human opponents when converting them to native AI. This is a host test
fixture change; the pinned engine remains `c93c2d380b0fd4d0e399a8d3738fec3938793792`.

## Implementation

- Gameplay and the adviser share dated output summaries, including science from
  production conversion separately from the remainder. Missing evidence stays
  unknown.
- Named construction reads accept a common horizon and compare native build and
  purchase previews with continued current output. Costs, blockers and constant-rate
  assumptions remain explicit; happiness is a level change, not an accumulating yield.
- The adviser compares current alternatives and earlier expectations. Compulsory
  per-victory essays and overlapping short/mid/long reports are removed.
- Gameplay can record up to three choices in its existing strategy memory. Each
  has expectations, a review point and an immutable service-owned baseline identity.
  Exact action-intent references connect receipts to the chosen revision.
- Existing turn and review calls deliver bounded factual feedback, including
  recently ended choices. Restart and rewind retain matching history and exclude
  discarded-future actions. Advice, deadlines and unverified references never
  restrict native-legal commands.

The private-store format is 10; new stores are required. Existing benchmark
images and stores remain preserved. No extra model role, reflection job or review
trigger was added. The independent adviser cannot adopt a choice for gameplay.

## Initial diagnostic trials

All trials used `gpt-5.6-luna`, maximum reasoning effort, both guides, fresh memory
and the same pinned engine. Other majors use native AI after preparation. The
historical games used model opponents, retained memory and older host `a5358d5`;
they are baseline evidence, not a controlled counterpart to these new trials.
The facts-only variant retains host `89a93c0`'s prompts and memory/review contracts
with the factual comparison changes. The full variant also changes those contracts
and prompt guidance. This is not a schema-only ablation.

| Position / variant | Completed turns | Population change | Treasury change | Uncached input tokens | Output tokens | Gameplay seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| China military pressure / facts | 5 | +2 | +71 | 590,071 | 78,557 | 491 |
| China military pressure / full | 5 | +2 | +66 | 612,951 | 80,053 | 565 |
| Inca conversion / facts | 7 | +1 | −62 | 793,396 | 99,496 | 619 |
| Inca conversion / full | 7 | +1 | +188 | 652,869 | 86,554 | 464 |

All four trials completed their requested turns, and provider, agent and host
usage reconciled. These are individual samples, not evidence of an overall
advantage. Native endpoint changes include the opponent turns after the last
handoff; observed yield rates below refer to the last delivered player position.

The initial China selection was mislabeled as a conversion case: its cities were
building military units and Barracks under Arabian pressure. Both variants
completed the Barracks. The full variant retained more military units, but roster
differences alone do not identify casualties or tactical quality. Neither saved
a chosen strategy. The full variant supplied invented plan names in intent, so
those annotations could not link receipts to a durable choice. The original suite
manifest remains intact; later selection checks verify active conversion and the
required culture-output threshold before launching trials.

The Inca pair tested actual Science conversion. Both completed Cuzco's Colosseum,
acquired Economics and gained one population. Both ended their observed turn 181
at 43 production and 125.5 science excluding conversion. Facts retained more
conversion output (11.5 versus 7.5 science); full was building Tiwanaku's Theatre.
The 250-gold difference came from facts gifting Hanoi 250 gold. Treasury retained
is not itself an advantage; this short interval does not establish the return
from either the gift or the Theatre investment.

The full Inca player saved three choices in one strategy revision, delivered in
six subsequent turn feedbacks. At the second review the adviser recognized the
completed Colosseum, distinguished endpoint happiness from causal attribution,
and identified that the road work had not connected Ollantaytambo. This confirms
use of prior expectations across a native review–play–review cycle. It does not
prove correct causal reasoning throughout or better investment outcomes. Receipt
links were still absent, and city-name subject tags did not populate city facts.

Those findings produced interface fixes: intent fields now explain saved memory
IDs, unlinked references return nonblocking guidance without changing receipts,
and city subjects match known names as well as handles. They do not prescribe
spending, construction, warfare or a victory route.

## Repeated screen

The updated source is frozen as `build/decision-variants-v4`. Three pairs each on
Inca conversion and Greece culture were attempted in
`build/decision-repeated-screen-v4`, with seven turns per trial and rotated variant
order. Limits were 20 activations, 5,400 seconds and 20 million input-plus-output
tokens per trial, including cached input. The provider returned structured
`usageLimitExceeded` before the first pair finished; the local trial limits were
not reached. **None of the 12 attempts completed its evaluation.**

Both initial Inca trials committed six native handoffs. Full accounted for all
six, completed its second review, then failed during turn 181. Facts stopped in
the turn-180 review, leaving five turns with complete accounting. The other ten
attempts failed before any handoff. All attempts, including those immediate
failures, remain in `results.json`; `evaluation-summary.json` records their
structured provider-limit classifications. The two `first-five-turn-report.json`
files reconcile only the fully accounted prefix. Native endpoint comparisons
are omitted for partial trials, since an unfinished turn may include extra actions.
These prefixes do not replace the planned repeated screen.

The latest full trial did verify real receipt links across a changed choice:
the player saved an investment in Hanoi, linked its actions to the appropriate
choice revision, and received separate feedback for the active and ended versions.
At the second review the adviser assessed the alliance and happiness objective
against the depleted treasury and unchanged military capacity, recommending
against another gift. This is further evidence that the interface works, not
proof that the investment was good or that the full variant plays better.

The suite runner now stops launching further trials when structured gameplay or
review status reports `usageLimitExceeded`. It records unstarted cases separately,
lets already running cases finish, and prints completed/accounted turns alongside
process exit status. A regression exercises the complete scheduling path. New
attempts should use a fresh suite directory after provider capacity is available;
do not overwrite these failed attempts or silently switch the comparison's model.

Reconciliation exposed a separate runner defect: its token guard read the
scheduler's gameplay counter, which excludes adviser usage. The guard now sums
reported input and output over all model jobs, including reviews, communication,
failed jobs and cached input once. A regression checks those roles and replacement
of a job's earlier usage snapshot. The retained v4 attempts used the earlier guard;
none triggered that guard, and their reported all-role usage stayed below the
nominal cap. Future suite manifests identify the corrected accounting basis.
Reporting lag and an in-flight request can still overshoot the polling limit.

## Provenance and acceptance

| Source snapshot | Facts source hash prefix | Full source hash prefix | Retained trial directory |
| --- | --- | --- | --- |
| v1 | `3a9031b07090` | `1c18597259f5` | `build/decision-screen-v1` |
| v3 | `ceb4858fea1a` | `5b905dfc049f` | `build/decision-conversion-screen-v3` |
| v4 | `1a55c5704c0b` | `cf50809946cc` | `build/decision-repeated-screen-v4` |

Full SHA-256 hashes and actual imported source paths are retained in each source
manifest and run. v1 predates the happiness-level correction. v3 includes that
correction but predates the reference feedback and city-name fixes. Earlier
snapshots are immutable and their results are not relabeled as the current build.

Focused tests cover comparison arithmetic and native blockers; missing evidence;
gameplay ownership; choice revision identity; idempotent receipts; unknown intent
references; and restart/rewind isolation. A baseline-backup regression verifies
committed WAL capture, frozen content and refusal to overwrite evidence.
The Python suite passed 1,272 tests and 76 subtests, with ten environment/opt-in
skips; seven focused evaluation/backup tests passed after the quota-handling changes.
The pinned-engine host build passed all 160 tests in 53 Kotlin suites. Its initial
format check exposed an existing fixture-formatting issue, corrected with the
repository formatter. Ruff lint and
format checks and Pyrefly passed. Read-only dashboard rendering also passed on
the latest historical report from each of the 20 preserved player stores.

Longer 20–30-turn trials, tactical regressions, the held-out start and complete
games with rotated assignments remain acceptance gates. Evidence of stronger
native outcomes and proportionate model cost is needed before preferring the full
loop to facts alone. Saving more strategy records, spending more gold or producing
more advice does not satisfy that gate.
