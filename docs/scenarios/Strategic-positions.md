# Saved strategic positions

The [fixture manifest](../../tests/fixtures/strategy_positions/manifest.json) and
adjacent native saves preserve situations from two Vanilla games. The manifest
records each source seed and checkpoint hash. They are regression/evaluation
inputs, not scripted solutions.

| Position | Player / start | Rounds | What to assess |
| --- | --- | --- | --- |
| `idle-workers` | Rome, 32 | 2 | Whether workers inspect and reach useful jobs beyond their current tile |
| `exploration-loop` | France, 16 | 3 | Actual new terrain, repeated positions and response to visible threats |
| `slow-economy` | France, 26 | 2 | Research, luxury improvement timing, growth and the young city's slow Library |
| `civilian-route` | France, 25 | 3 | Two settlers, incomplete scouting, protection, city sites and subsequent native demands |
| `injured-military` | Greece, 26 | 2 | Survival, retreat or defensible restraint under visible pressure |
| `scout-survival` | France, 41 | 3 | 24-HP Scout with a potentially lethal attack, retreat choices and a stacked Worker |
| `settler-escort` | Greece, 33 | 4 | Settler journey, military coordination, scouting and survival or founding |
| `civilian-exposure` | France, 39 | 3 | Settler/Worker exposure before historical barbarian captures |
| `stalled-city` | Rome, 35 | 5 | Antium's zero food surplus, population, worked tiles and construction progress |

The last four cases come from seed `1283182`, reviewed on September 23. The first
five retain seed `10923811242`. Saved positions have no prescribed winning move:
retreat, defense, continued expansion or a justified delay can all be reasonable.

The preparation changes player control only. The subject remains a human slot
controlled by a model; other majors use native AI, alongside the existing city
states. Where necessary, the existing source player's turn ends without commands
and native turns advance to the subject. The retained files already include that
advance, so subsequent runs start from identical saves. Terrain, units, resources,
technology and treasury receive no fixture-authored edits.

Ordinary fixture runs start with fresh private memory and a fresh host ledger. Historical
social conversations and plans are not imported. The agent's objective is the
normal objective to win. Evaluation criteria are not added to its instructions.
This tests decisions from the board position, not exact replay of historical
reasoning. Opponent actions and model choices can vary after the saved start.
The memory comparisons below describe explicit historical seeds for that separate experiment.

## Prepare and run

Use a new output directory for each preparation and run. Preparation validates
save hashes (excluding trailing whitespace), the controlling civilization, load/reload, and relevant native
opportunities. For example, the worker case must have visible, owned, reachable
unimproved tiles with a currently legal improvement; the combat case must offer
movement to an injured unit near a visible hostile.

```bash
./gradlew prepareStrategyPositions \
  -PstrategyOutput="$PWD/build/strategy-positions"

.venv/bin/python -m tests.live_strategy_positions \
  --prepared "$PWD/build/strategy-positions" \
  --state-dir "$PWD/build/strategy-high" --port 11381 \
  --model gpt-6-luna --reasoning-effort high --fast --strategy-guide

.venv/bin/python -m tests.live_strategy_positions \
  --prepared "$PWD/build/strategy-positions" \
  --state-dir "$PWD/build/strategy-max" --port 11481 \
  --model gpt-6-luna --reasoning-effort max --fast --strategy-guide
```

The suite runs at most two isolated games at once, with 20-minute activation
timeouts, 60 activations and four hours per case by default. Override these with
`--turn-timeout`, `--max-invocations` and `--max-seconds`. `--parallel 1` runs sequentially; repeat `--case ID`
to select cases. It retains logs and checkpoints and verifies completed rounds:
a clean driver exit after an agent stops prematurely is an incomplete evaluation.
Infrastructure completion does not mean the strategy was good.

For constructed early military opportunities and defended-city controls, generate
the native positions with `generateDominationPositions`. Its manifest includes
`persia-opportunity` (damaged capital and existing army),
`persia-strong-defense` (healthy walled capital and strong defenders),
`greece-damaged-capital` (Hoplites and Archers with ordinary movement), and
`greece-hill-fortress` (the same Greek force against a fortified hill city).
The fixtures validate visible facts and checkpoint reload; they do not supply
desired decisions to the player.

```bash
./gradlew generateDominationPositions -PdominationOutput="$PWD/build/military-positions"

.venv/bin/python -m tests.live_strategy_positions \
  --prepared "$PWD/build/military-positions" \
  --manifest "$PWD/build/military-positions/manifest.json" \
  --state-dir "$PWD/build/military-live" --port 19841 \
  --case persia-opportunity --case persia-strong-defense \
  --case greece-damaged-capital --case greece-hill-fortress \
  --rounds 2 --model gpt-6-luna --reasoning-effort medium
```

Use `--variant NAME=SOURCE_ROOT` for each frozen Python source directory containing
`civ_agent`, and `--repetitions N` for repeated trials. Keep model settings and
native positions identical across source variants. Record military recommendations,
Main's decisions, declaration and capture turns, and actual losses separately;
a declaration alone does not demonstrate useful aggression. Short trials diagnose
opportunity recognition and execution, not overall win rate.

### Campaign initiative comparisons

`generateCampaignInitiativePositions` prepares constructed native positions for
longer military decisions. These compare the current proactive military manager
with changes to wartime initiative and campaign persistence. The archived v0–v2
games predate both; their outcomes are motivation for these cases, not the
comparison's baseline.

| Position | What to assess |
| --- | --- |
| `campaign-reconnaissance` | Whether peacetime reconnaissance resolves missing target information and leads to a concrete decision |
| `campaign-siege` | Whether a campaign continues or adapts when useful progress outlasts its initial review date |
| `campaign-counteroffensive` | Whether an existing defensive objective is reconsidered when the current board permits an advance |
| `campaign-counteroffensive-control` | Whether stronger opposing defenses still produce justified restraint |

The player is Greece and France is its neighboring opponent. The native board
and ordinary opponents determine subsequent outcomes. Reconnaissance and the
counteroffensive run seven own turns, the sustained siege nine, and the stronger
defense control four. The siege starts with two Catapults against a healthy walled
city; the other positions use one. Peace is available in all wartime starts.
The wartime cases add
explicit operator-authored prior goals and operation records to otherwise fresh
private stores. These represent prior intentions to reconsider, not instructions
to pass the evaluation. They do not supply the expected test outcome or hidden
defender information. Current permitted observations take precedence over prior
plans.

A case's optional `memorySeed` is resolved relative to its manifest. The suite
copies and hashes the seed before launching trials and shares those exact bytes
across implementation variants. The seed's civilization and current native
opponents are checked; prior records and war start dates must precede the evaluated
turn. This seeds the existing war-goal and operation mechanisms rather than adding
a production behavior switch. Inspect the retained `memory-seed.json` when judging
how inherited goals affected a decision.

```bash
./gradlew generateCampaignInitiativePositions \
  -PcampaignOutput="$PWD/build/campaign-positions"

./gradlew probeCampaignInitiative \
  -PcampaignSource="$PWD/build/campaign-positions" \
  -PcampaignProbeOutput="$PWD/build/campaign-native-probe"

.venv/bin/python -m tests.live_strategy_positions \
  --prepared "$PWD/build/campaign-positions" \
  --manifest "$PWD/build/campaign-positions/manifest.json" \
  --state-dir "$PWD/build/campaign-comparison" --port 20041 \
  --variant before="$PWD/build/campaign-before-src" \
  --variant after="$PWD/build/campaign-after-src" \
  --model gpt-6-luna --reasoning-effort medium \
  --strategy-guide --no-civilization-guide --review-interval 5 \
  --parallel 2
```

Use fresh output directories and frozen source directories containing `civ_agent`.
The manifest supplies each case's duration; overriding all cases with a short
`--rounds` can cut off a siege before the behavior under study occurs. Native
probes check that a candidate sequence can work from the position. They neither
provide that sequence to the models nor establish that all other decisions are
wrong.

Evaluate the sequence of decisions: military's recommendation, Main's response
and accepted goals, actual reconnaissance/staging/attacks, then territorial
results, losses and peace. A formal goal revision is evidence of changed intent,
not evidence of a successful invasion. An absent revision alone does not establish
inaction. Keep consultation recommendations and Main's exact-terms peace decisions
with the action trace. The stronger-defense control matters: increased attack
frequency is not by itself an improvement. Single paired runs diagnose behavior;
they do not establish a reliable effect size or overall win rate.

Select the September 23 failures with:

```bash
.venv/bin/python -m tests.live_strategy_positions \
  --prepared "$PWD/build/strategy-positions" \
  --state-dir "$PWD/build/tactical-evaluation" --port 11681 \
  --case scout-survival --case settler-escort \
  --case civilian-exposure --case stalled-city \
  --model gpt-6-luna --reasoning-effort max --fast --strategy-guide
```

Run a three-round opening (nine agent turns, plus native Egypt and two city states):

```bash
.venv/bin/python -m tests.live_saved_position \
  --state-dir "$PWD/build/opening-max" --port 11571 \
  --seed 10923811242 --model gpt-6-luna --reasoning-effort max \
  --fast --strategy-guide --max-turns 3 --max-invocations 15 \
  --max-seconds 7200 --turn-timeout 1200
```

For exact opening comparisons, use `--checkpoint ROUND_0_JSON --source-state
ORIGINAL_STATE_DIR` in place of `--seed`. Only an opening checkpoint without prior
jobs/handoffs is accepted. The original files are read and copied, never resumed.

## Inspect results

```bash
.venv/bin/python -m tests.report_strategy_positions \
  build/strategy-max/idle-workers --output build/worker-report.json
```

The report reads SQLite through SQLAlchemy in read-only mode. It reconciles
provider trace tokens against agent and host records, counts model requests,
reports startup size, API calls, memory edits and gameplay/finalization time, and
preserves commands with movement-discovery feedback. Operator-only final native
facts include consequences of the last opponent turns. The report never sends
hidden state to an agent. Use `--rounds 3` to compare the first three completed
rounds of a historical game; its much later final checkpoint is then omitted.

New position runs retain `initial-game.json`. Reports compare native unit identity,
health and location, city population/food/buildings, new cities, exploration and
treasury against that start. Missing units are not automatically called casualties:
founding, upgrades, disbanding and other consumption also change the roster. Inspect
receipts and combat reports for the cause. Partially rejected memory batches appear
in the error details as well as the memory-edit counts.

Interpret outcomes with the available information. A risky route under fog is
different from knowingly moving into visible danger. A loss is not automatically
a bad decision, and waiting can be sensible. Inspect native legality and the
agent's actual reads before assigning a failure. Short, single-sample runs cannot
establish overall win rate or separate every prompt/model/opponent effect.

## Decision-process comparisons

The [fixed manifest](../../tests/fixtures/decision_positions/manifest.json) records
the October 7 baseline: g0001 turn 175, g0002 turn 200, g0003 turn 231 and g0004
turn 191. Game 2 advanced after the audit's turn 180 comparison. Preserve those
dates when comparing the two reports. All 20 memory histories verified against
their retained recovery boundaries. The full private bundle is at
`build/decision-baseline-2026-10-07`; it includes credentials and must stay outside Git.
`tests.preserve_benchmark_baseline` can capture a new, separately named bundle,
without opening services or modifying source games. SQLite online backup includes
the WAL; do not copy a live `.sqlite3` file alone.

| Case | Player | Evaluation purpose |
| --- | --- | --- |
| `conversion-capacity` | g0001 Inca | Active Science conversion, weak productive capacity and the cost of changing investment |
| `science-capacity` | g0001 China | Retained initial screen: urgent defensive pressure, currently building units rather than Science |
| `treasury-reserve` | g0003 China | All four cities convert Science; timing and opportunity cost of a large reserve |
| `military-objective` | g0003 France | Strong army and an unresolved operation |
| `cultural-strength` | g0004 Greece | Protect the successful cultural specialization |
| `held-out-start` | g0002 France | Different starting assignment, excluded from tuning |

The first China screen exposed a selection mismatch. Its original manifest stays
in the retained suite; do not relabel its results as a pure conversion test.
The runner now verifies active conversion and the cultural-output threshold
where requested by the case manifest. Evaluation conditions are never player instructions.

Prepare isolated saves and immutable implementation snapshots in fresh directories:

```bash
uv run --no-sync python -m tests.prepare_decision_positions \
  --baseline build/decision-baseline-2026-10-07 \
  --output build/decision-sources-new --variants-output build/decision-variants-new

./gradlew prepareStrategyPositions -PstrategyMode=capture \
  -PstrategySource="$PWD/build/decision-sources-new" \
  -PstrategyOutput="$PWD/build/decision-prepared-new"

uv run --no-sync python -m tests.live_strategy_positions \
  --prepared build/decision-prepared-new \
  --manifest tests/fixtures/decision_positions/manifest.json \
  --state-dir build/decision-comparison-new --port 12501 \
  --case conversion-capacity --case cultural-strength \
  --variant facts=build/decision-variants-new/facts/src \
  --variant full=build/decision-variants-new/full/src \
  --model gpt-5.6-luna --reasoning-effort max \
  --strategy-guide --civilization-guide --review-interval 5 \
  --rounds 7 --repetitions 3 --parallel 2 \
  --max-invocations 20 --max-seconds 5400 --max-tokens 20000000
```

`facts` preserves revision `89a93c0`'s prompts, review and memory contracts, adding
the current factual projections and construction-comparison tools. `full` uses
the current implementation. Both variants start with fresh memory and the same
prepared opponents, model and engine. Each run records the actual imported Python
source hash, prepared-save hash, guide settings and prompts. The baseline games
used older revision `a5358d5` and model opponents. Their retained trajectories are
historical evidence, not a controlled counterpart to these fresh-memory,
native-opponent trials. The full refactor also shortens prompt guidance, so it is
not a schema-only ablation.

Trial order rotates between repetitions. All attempts, including incomplete
ones, remain in `results.json`. Limits apply per trial; the token guard polls
reported input plus output across all model jobs (gameplay, reviews and chat),
including cached input once, every two seconds and requests
shutdown on exhaustion. Reporting lag and an in-flight request can overshoot it.
Structured provider `usageLimitExceeded` in gameplay or a review stops further
launches; remaining cases are recorded as unstarted and already running cases
finish normally. A process exit of zero does not establish completed rounds or
complete accounting. Keep failed attempts and use a fresh suite directory to retry.
Seven turns from the conversion position cover reviews after rounds 175 and 180.
Use the preserved benchmark's interval of ten in later matched trials, and increase
rounds to 20–30 for realized investment outcomes. Explicitly set suitable token,
time and invocation caps for those longer runs.

`report_strategy_positions` now includes chosen-decision revisions, delivered
feedback, named investment comparisons, adviser reports, observed output excluding
conversion, and native technology/policy/population changes. Interpret these with
the case's preregistered measures. Missing evidence is not failure; receipt
acceptance is not an achieved objective. Classify findings manually as information,
mechanics, investment comparison, execution, reconsideration or service failures.
Reports suppress final native comparisons for unfinished turns, since their
checkpoints may include actions outside the completed/accounted interval.
Do not promote a variant solely for spending more, fighting more, producing more
plan records or using fewer tokens. Baseline variance and tolerable regression
remain unknown until repeated outcomes establish them. Longer trials, tactical
regressions, the held-out start and full games remain acceptance gates.

## Memory comparisons

The decision-process comparison above uses fresh memory on both sides. Historical
memory seeds in this section belong to the earlier notebook experiment.

`tests.live_memory_comparison` runs a paired comparison on prepared positions,
with fresh stores and logs for every trial. It never resumes the source match.

| Variant | Initial memory / implementation |
| --- | --- |
| `baseline` | Historical plan, using a separately preserved old Python source snapshot |
| `knowledge` | Retained notes and beliefs only, using the current implementation |
| `revised` | The same historical assessment split into provisional strategy and next-turn handoff |

The knowledge control omits the **initial** strategy/handoff; agents may create
their own during play. This measures the effect of inherited memory, not enforced
amnesia. If the source has no retained notes or beliefs, this control starts with
an empty notebook. All variants use the same prepared native save and corrected
engine. An old runtime is supplied only to this evaluation subprocess; production
has no compatibility mode.

The original plan's freeform prose can also contain factual claims. Omitting it
removes those claims alongside its intentions; shared current observations do
not make this a perfect separation of historical facts from strategic advice.

The operator manifest contains `positions`, each with `id`, `civilization`,
`rounds` and `seeds` mapping the three variants to seed JSON paths relative to the
manifest. Each seed contains `civilization` and `records`, with an authoring
`turn` and typed `content` for each record. Seed dates must precede the evaluated
turn. Historical text is preserved without importing hidden game state, future
memory, or obsolete evidence handles. `--memory-seed` is accepted only with a
fresh isolated `--position`.

For the retained September 25 preparation:

```bash
.venv/bin/python -m tests.live_memory_comparison \
  --manifest build/memory-evaluation/source/single-turn.json \
  --prepared build/memory-evaluation/prepared \
  --baseline-source build/memory-evaluation/baseline-src \
  --state-dir build/memory-evaluation/comparison --port 11871 \
  --case war-response --repetitions 2 \
  --model gpt-6-luna --reasoning-effort max --fast --strategy-guide
```

The prepared cases cover Rome at war (turn 71), peaceful continuity (60), an
unfinished settlement journey (69), and France's construction choices (70).
The source saves retain historical consequences of the production-preview bug;
the corrected engine is shared by every trial. They compare decisions from the
same board, not the original game's uncorrupted trajectory.

Use `--case` and `--variant` to bound a batch. The driver runs sequentially,
rotates variant order across repetitions, verifies completed rounds, and stops
on the first incomplete trial. Timeouts allow 15 minutes per service startup,
20 minutes per activation and two hours per trial. It records source hashes,
seeds, model settings and completion status. Preserve the suite directory and
baseline snapshot alongside reports. `single-turn.json` schedules one player turn
per trial: the full four-case/two-repeat matrix contains 24 turns. The retained
`manifest.json` schedules two turns per trial, giving 48 turns for the full matrix.
Use a bounded case before spending tokens on the larger batch.

Assess actual choices and outcomes, not just changed prose. Compare defense,
production/research changes, settlement progress and peaceful continuity, together
with confirmed losses and costs. Continuing science can be justified; merely
changing victory labels is not success. Use repeated trials before attributing
behavior to memory rather than model variation. The existing reporting command
reconciles usage and preserves decision evidence for each trial.

### September 25 comparison

The comparison completed all 24 trials: four positions, three memory variants
and two repetitions. The first war trial in each variant also ran a second turn,
giving 27 player turns in total. Matched metrics use only the first turn of every
trial. All runs used GPT-6 Luna at max reasoning, fast priority and the optional
guide, with identical prepared native saves within each case.

All gameplay and background memory completed; 36 memory edits succeeded and none
were rejected. Recorded provider, service and host usage agrees. Nineteen off-turn
communication jobs were interrupted at the bounded-test shutdown; their usage
remains explicitly incomplete. One rejected END_TURN attempt recovered after the
agent selected an affordable policy.

Both inherited-memory variants retained science research in both war trials;
the knowledge control chose Bronze Working in both. Its stated aim of unlocking
Spearmen overlooked already-buildable Pikemen. Five war trials lost the exposed
Archer; the remaining knowledge-control trial saved the Archer but lost a Worker.
France's builds varied within every memory variant, and all six peaceful trials
kept the existing research/construction queues. None of the settlement trials
founded a city; the Settler started with no movement remaining.

The revised two-turn pilot preserved the strategy's turn-36 date when only its
handoff changed, then explicitly replaced that strategy on turn 72. This validates
the new semantics without establishing better strategic play. Matched gameplay
time was almost unchanged: 1,703 seconds with old plans versus 1,709 with revised
memory across eight first-turn activations each.

See the [dated evaluation](../archive/reports/Strategy-handoff-evaluation-2026-09-25.md)
for decisions, losses, costs and limitations. Detailed evidence and source
snapshots remain under `build/memory-evaluation/`.

## September 23 tactical and memory evaluation

The subsequent [notebook redesign comparison](../archive/reports/Notebook-redesign-2026-09-23.md)
reran the civilian and stalled-city cases. France finished all three turns;
Rome stopped during turn 38 after an inaccurate combat projection and a confirmed
Warrior loss. All seven memory edits succeeded, including two communication
contributions. The incomplete Rome case is retained for the combat follow-up.

The four new failure positions completed all 15 scheduled turns with GPT-6 Luna
at max reasoning. The Scout survived and healed, and Greece preserved its Settler
and reached a guarded site. France lost its Settler in the separate civilian
case; Antium remained population 1 with zero food surplus. All token totals
reconciled. See the [dated report](../archive/reports/Tactical-memory-evaluation-2026-09-23.md)
for usage, memory failures, validation and remaining limits. Artifacts are retained
in `build/tactical-evaluation-20260923/`.

## September 22 evaluation

Artifacts are retained in `build/strategy-evaluation-20260922/`. Results and the
high/max comparison use GPT-6 Luna, fast priority and the optional guide. All token
totals reconcile between provider traces, agent records and host telemetry.

The implementation removes mandatory plan categories, automatic five-turn review
dates, compulsory end-turn explanations, prescribed action ordering and the default
bias toward existing production. The guide moves to advisory user context before
changing state. Movement reports actual exploration, including zero; worker reads
state their current-tile scope and link nearby map data. Memory instructions arrive
at handoff, and flagged-plan summaries no longer repeat every plan's objective.

The first civilian run stopped after one completed turn because the agent could
not find the existing native demand-response route. Available major-player
responses now carry a copyable `chat_send`/`RESPOND_DIPLOMACY` template. The repaired
high run refused Greece's demand and completed all three turns. The suite also
now checks completed rounds instead of mistaking a clean driver exit for success.
The original incomplete attempt remains in `positions/civilian-route`;
`high-response-retest/civilian-route` contains the repaired run.

### Opening benchmark

All runs below completed nine player turns from the same saved opening. Times
are summed per-turn gameplay/finalization time; finalization can overlap gameplay.

| Metric | Historical high | Streamlined high | Max |
| --- | ---: | ---: | ---: |
| Model requests | 81 | 71 | 79 |
| Model-issued player tool calls | 83 | 70 | 87 |
| Input tokens, including cached | 2,201,174 | 1,810,312 | 2,291,198 |
| Uncached input tokens | 220,246 | 194,184 | 263,934 |
| Output tokens, including reasoning | 25,864 | 14,266 | 52,804 |
| Reasoning output tokens | 16,039 | 9,404 | 46,007 |
| Gameplay seconds | 307.4 | 261.5 | 618.5 |
| Finalization seconds | 165.2 | 27.4 | 137.9 |
| Accepted memory edits | 17 | 0 | 5 |
| API errors / rejected memory edits | 0 / 0 | 0 / 0 | 0 / 0 |

Max used 2.37 times the gameplay time, 36% more uncached input and 3.70 times the
output tokens of streamlined high. It made different opening choices, including
moving Rome's Settler for one turn before founding beside Silver. Three rounds
cannot establish the longer-term payoff of that site choice.

These are single samples. Cache warmth, opponent behavior and model variation
affect comparisons. The historical run also predates the current fixes. The
streamlined high opening preceded the small native-response template and optional
end-turn wording correction; max includes them. The high civilian case was rerun
with those corrections. No native demand occurred in either opening benchmark.
The reduction in developer-message characters includes moving the guide to user
context: initial total prompt text fell by about 5%, not the 31% suggested by
looking only at the developer message.

Detailed artifacts: `opening-report.json`, `historical-opening-report.json` and
`max-opening-report.json` under the evaluation directory. Provider request counts
come from retained traces because the live terminal currently reports them as
unavailable.

### Saved-position outcomes

Both reasoning levels completed all five cases (12 player turns each), using the
repaired high civilian run described above. Effective activation settings confirm
`gpt-6-luna` with `high` or `max`; all runs used fast priority and the guide.

| Position | High | Max | Assessment |
| --- | --- | --- | --- |
| Workers | Two workers stayed idle; the third started a Mine over an existing Camp | Relocated all three workers and started two Mines and a Camp on unimproved tiles | Clear improvement in finding and carrying out useful work |
| Exploration | Revealed five new tiles | Also revealed five new tiles, after two turns without new terrain | No improvement in exploration progress |
| Economy | Orleans's Library still needed 48 turns; founded Lyon and reduced happiness to −6 | Bought a production tile, reducing the Library estimate to 24 turns; delayed Lyon and kept happiness at −2 | Better production and expansion coordination, but still delayed Trapping and the Ivory improvement |
| Civilian route | Founded Orleans; the second Settler survived at `(-4, -3)` | Founded Orleans; Barbarians captured the second Settler, which remained captured at the final checkpoint | Max did not solve route safety |
| Injured military | Retreated the Warrior; it survived at 37 health | Delayed retreat; the Warrior survived at 13 health | Worse health outcome; both Scouts finished at 44 health |

The civilian loss followed movement through incompletely scouted terrain. The
nearby Barbarians became visible when the Settler had no movement left; this is
not evidence that the model knowingly chose a destination beside visible enemies.
The max run also chose a Library for the new population-one Orleans, whereas high
chose a Worker. The economy improvement in its separate fixture did not reliably
transfer to the new-city decision.

Across these 12 player turns, max took 36.0 minutes of summed gameplay time versus
11.0 minutes for high, and used 190,794 versus 44,493 output tokens, including
reasoning. High made one unavailable movement attempt and recovered. Max had no
top-level API errors, but one memory edit was rejected with `SOURCE_NOT_DELIVERED`.
The rejection is included in memory telemetry; all turns finalized and accounting
reconciled. There were no service crashes in either completed comparison.

These results support retaining high as the default for now. Max can improve
planning in particular positions, but additional reasoning did not consistently
improve exploration, combat or civilian safety. Repeated paired runs would be
needed to estimate reliability. The retained positions allow those comparisons
without another long opening game.

Reports are named `<position>-report.json` and `max-<position>-report.json`; use
`high-response-retest-report.json` for the completed high civilian case. Each
report retains frozen observations at handoff and final native outcomes, so
consequences after opponent turns are distinguishable from what the agent saw.

### Validation

Focused Python tests cover prompt placement, advisory review behavior, worker
scope, exploration updates, native response templates and incomplete evaluation
detection. Native turn-review integration tests and saved-position load/reload
checks passed. The installed Codex wire test confirmed the stable guide/user
context layout. Ruff, type checking and provider/agent/host token reconciliation
also passed. These checks validate the implementation and reporting; the outcome
table records the remaining gameplay weaknesses.
