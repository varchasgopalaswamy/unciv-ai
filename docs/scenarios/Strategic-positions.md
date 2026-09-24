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

Each case starts with fresh private memory and a fresh host ledger. Historical
social conversations and plans are not imported. The agent's objective is the
normal objective to win. Evaluation criteria are not added to its instructions.
This tests decisions from the board position, not exact replay of historical
reasoning. Opponent actions and model choices can vary after the saved start.

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
timeouts and an hour per case. `--parallel 1` runs sequentially; repeat `--case ID`
to select cases. It retains logs and checkpoints and verifies completed rounds:
a clean driver exit after an agent stops prematurely is an incomplete evaluation.
Infrastructure completion does not mean the strategy was good.

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
.venv/bin/python -m tests.live_vanilla \
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

## September 23 tactical and memory evaluation

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
