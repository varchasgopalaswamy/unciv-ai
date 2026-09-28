# Strategy and handoff evaluation — September 25, 2026

The comparison completed 27 player turns without a rejected memory edit or a
service crash. The revised contract preserves useful continuity without treating routine
handoff updates as reaffirmation of an old strategy. These trials do **not**
establish an improvement in strategic play.

## What changed

- One provisional `strategy` assessment and one next-turn `handoff` replace
  persistent plans and principles. Notes, beliefs and historical evidence remain.
- Strategy and handoff updates replace their complete content. Updating the
  handoff neither refreshes the strategy's date nor acknowledges its notices.
- Writes retain their authoring turn during background work. Handoffs appear on
  the next gameplay turn, survive retries, and then leave automatic context.
- Current observations precede historical memory in the changing preload.
  Stable instructions and capabilities remain first for caching. Active strategy
  is no longer repeated as an automatic unresolved obligation in turn summaries.
- Consequential commands carry a current reason; an optional historical strategy
  reference cannot substitute for that reason. Strategy notices remain advisory.
- Memory continues in the gameplay conversation after handoff. There is no added
  mandatory strategic-review model call or host-imposed victory route.

[Memory format 4](../../architecture/Structured-memory.md) requires fresh stores
in a new match directory. Older stores/checkpoints are rejected without changing
them. The source match was read only; there is no compatibility runtime in
production.

## Comparison design

Four saved positions each received two trials of three variants, giving **24
comparable first-turn samples**. The original war pilot additionally ran a second
turn in each variant, giving **27 actual player turns**. The added trials used
one turn to bound cost; this does not measure long-term execution or win rate.

| Variant | Initial memory |
| --- | --- |
| Old plan | Historical plan, with the separately preserved old Python package |
| Strategy + handoff | Historical objective as a provisional assessment; historical approach as the next-turn handoff |
| Knowledge only at start | Retained notes and beliefs; no inherited strategy or handoff |

There were no retained notes/beliefs to import in these cases, so the knowledge
control began with an empty notebook. It could write its own strategy during
play. Removing the plan also removes factual claims embedded in its prose; this
is not a perfect separation of knowledge from intentions.

All runs used GPT-6 Luna, max reasoning, fast priority and the optional strategy
guide. Variants share an identical prepared native save for each case and the
corrected engine. Subject players use the model; other major civilizations use
native AI. Evaluation criteria were not supplied as agent instructions. The
driver retained every trial and stopped on incomplete gameplay; none failed.
The repeated peace/settlement/construction suites rotated variant order. The
separately launched war repetition used the default variant order again.

## Decisions and outcomes

| Position | Old plan | Strategy + handoff | Knowledge only at start |
| --- | --- | --- | --- |
| Rome at war, turn 71 | Compass → Astronomy in both trials | Compass in both; Astronomy queued in one | Bronze Working in both |
| Peaceful Rome, turn 60 | Kept research and construction queues in both | Kept queues in both | Kept queues in both |
| Settlement journey, Rome 69 | No founding; scouting/routes varied | No founding; scouting/routes varied | No founding; scouting/routes varied |
| Empty production queue, France 70 | Porcelain Tower / Stone Works | Science / Porcelain Tower | Stone Works / Porcelain Tower |

Construction choices are listed as first trial / second trial.

In the war case, both trials without inherited plans chose Bronze Working to
unlock Spearmen. Pikemen were already buildable in both cities because Civil
Service had been researched, and this was visible to the player. Changing
research direction is not, by itself, evidence of better play.

Five of the six war trials lost the exposed Archer during Egypt's next turn.
The remaining trial, without inherited plans, preserved that Archer but lost a
Worker: Egypt's Horseman captured it at the chosen retreat destination. This
provides no consistent improvement in protection from removing inherited memory.

All six settlement trials began with a Settler that had no movement remaining;
none founded that turn. They exercised scouting, standing-route choices and
handoff writing. They cannot establish that settlement delays have improved.

All six France trials made Lyon's food surplus positive before ending the turn;
five also corrected Paris's deficit. The second old-plan trial left Paris at
−3 food per turn. Production varied within every variant. The revised trial that
chose Science explicitly intended to use it while Lyon completed its University and
opened the Oxford path; it projected 5.9 science per turn. A temporary Science
choice is not automatically bad, and building a wonder is not automatically
good. One turn does not establish their relative payoff.

### Two-turn war follow-through

The initial pilot ran Rome turns 71–72 once per variant:

| Variant | Research | Added construction | Other decisions |
| --- | --- | --- | --- |
| Old plan | Compass → Astronomy | None | Retreated Worker; shot Horseman; directed Settler toward Dyes/Gems near Egypt |
| Strategy + handoff | Compass → Astronomy | University in Rome; Archer in Antium | Retreated Worker/Archer; corrected food deficit; retained uncertain settlement site |
| Knowledge only at start | Bronze Working → Iron Working | University in Rome; Archer in Antium | Used Food focus; moved remaining Archer toward front; redirected Settler west |

The exposed Archer was lost in all three trials. Both queued replacement Archers
followed a Garden with one turn remaining; that queue order is not evidence of a
long delay. No trial founded another city.

The revised trial delivered strategy dated 36 and handoff dated 70 on turn 71.
It replaced only the handoff that turn. Turn 72 still delivered strategy dated
36, followed by handoff dated 71. After the Archer loss, the model explicitly
replaced its strategy, retaining science while adding defense as the immediate
priority. This demonstrates the intended separation between strategic assessment
and routine continuity; it does not prove that the new assessment was optimal.

## Cost and accounting

The following table compares **eight first-turn gameplay activations per variant**,
including their background memory. It excludes communication jobs and the three
extra second turns from the pilot. Input includes cached input; output includes
reasoning. Times are sums, and finalization may overlap other work.

| Variant | Model requests | Input | Uncached input | Output | Gameplay seconds | Finalization seconds | Memory edits accepted/rejected |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Old plan | 118 | 6,563,306 | 524,010 | 127,885 | 1,703.3 | 244.3 | 8/0 |
| Strategy + handoff | 120 | 7,152,443 | 533,051 | 135,844 | 1,709.3 | 245.3 | 8/0 |
| Knowledge only at start | 105 | 5,538,174 | 504,190 | 145,713 | 1,803.9 | 248.9 | 14/0 |

All **27 gameplay jobs** completed their background memory and accounting.
Across all jobs, **36 memory edits were accepted and none rejected**. One END_TURN
attempt returned TURN_BLOCKED because the model treated zero free policies as
no affordable policy. It inspected policy details, adopted Oligarchy and recovered.

There were 8 completed and 19 interrupted off-turn communication jobs. The
interruptions occurred when bounded one-turn tests shut down after their scheduled
gameplay and memory finished. Their records correctly retain
`usageComplete=false`. Every **recorded** provider/service/host token total matches, including
these partial jobs. That agreement does not imply that an in-flight request's
final usage was available.

Across the entire batch, the retained records contain 492 model responses with
usage, 24,482,755 input tokens (2,148,035 uncached) and
531,199 output tokens. These include the extra pilot turns and measured
communication usage, so they differ from the matched first-turn table. They are
recorded totals, not a claim of complete billing for interrupted requests.

The original two-turn war pilot, including all its completed communication jobs,
used 38/37/45 model requests for old/revised/knowledge respectively, with
44,276/47,275/69,395 output tokens. Summed gameplay took 510.0/515.0/802.7 seconds.
The repeated matrix is a better breadth check, but two samples per case still
cannot isolate cache warmth, concurrent load, model variation and action choices
as causes of timing differences.

## Validation and limits

- Full Python suite: **731 passed**, two opt-in checks skipped, 90 subtests passed.
  Replacement/read conflicts, format-3 rejection, verifier naming and seed
  metadata also passed in subsequent targeted runs (55 checks plus three seed
  checks). Ruff, formatting, Pyrefly and diff checks passed.
- Saved-position preparation/reload passed. The production-preview fix had
  already passed nine host checks and one native test. The shared corrected
  engine avoids rebinding live tile ownership during a construction preview.
- Source saves retain historical consequences of that earlier preview bug.
  These trials compare decisions from the same board, not an uncorrupted replay
  of the original game.
- Seed text preserves historical intent, but does not import the full old
  evidence ledger or original model conversations. Old and revised packages
  differ in memory semantics and preload ordering, not just record names.
- The current implementation was reliable in this batch. Inherited strategic
  direction still influenced decisions, and the knowledge control did not
  consistently choose better actions. A general strategic benefit is unproven;
  further gameplay should be evaluated on outcomes without requiring a change
  of victory goal.

## Retained evidence

Artifacts are under `build/memory-evaluation/`:

- `matrix-progress.json` and `matrix-summary.json`: 24 matched first-turn samples
  and aggregate metrics. Each trial has a `first-turn-report.json` containing
  commands, action reasons, frozen observations and token reconciliation.
- `all-job-accounting.json`: all gameplay and communication jobs, including
  interrupted jobs with incomplete usage explicitly retained.
- `first-turn-native-outcomes.json`: matched outcomes after the first player turn
  and intervening native turns. The original pilots use their turn-72 checkpoint,
  so their second turn does not contaminate this comparison. Captures/destruction
  are checked against native notifications, not inferred solely from roster size.
- `pilot/`, `war-baseline/`, `war-knowledge/`: the original two-turn runs and full
  reports. `war-repeat/`, `settlement-repeat/`, `continuity-and-options/`: added
  trials, logs, checkpoints and completion records.
- `source/`, `prepared/`, `baseline-src/`: historical seeds, prepared saves,
  source hashes and the isolated old-package snapshot. `position-hashes.json`
  verifies one starting board per case across all variants.

See the [comparison procedure](../../scenarios/Strategic-positions.md#memory-comparisons)
for reproduction and cost controls. The source match and its databases were not
resumed, rewritten or migrated.
