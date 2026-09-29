# Military objectives in strategic reviews — September 28, 2026

## Change

The adviser compares development, expansion, diplomacy and military action by
their effects on the player's position relative to rivals. Military action can
support any victory path. It identifies intermediate objectives, likely rival
responses and uncertainties relevant to the next decision.

Victory paths lead with objective, investment and payoff before remaining
requirements in the schema and dashboard. The disruption field asks for costs and
conditions for pursuing or ending an operation. The optional guide explains
limited wars, including denial of civilian development, and the costs and benefits
to uninvolved rivals. The main adviser instruction grows from 919 to 957 characters;
the report has the same fields and limits.

This comparison predates the follow-up wording that explicitly allows an open
victory choice and describes long-term options, commitments and other promising
approaches. Those later edits are not included in the six reviews below.

## Method

Three controlled native Vanilla positions were created and saved at turn 40.
Each was reloaded through the engine and exported through the ordinary permitted
player observations. These are constructed positions, not turns from the current
running match. They have empty private history and social context.

Each position receives one review with the old prompt/schema from
`28b41651c448909dcd130a41c68700bf1ae1afdb` and one with the revised prompt/schema.
Both use GPT-5.6 Luna, max reasoning, requested fast priority (reported as
`priority`), and no optional strategy guide, matching the current game's model
settings. The guide's new text is therefore not part of this behavioral comparison.
Before/after order alternates between positions.

The ordinary review runtime, output validation and frozen read tools are used.
The standalone evaluator supplies concise native read-tool descriptions and grants
its local heartbeat; it has no live host connection. Input hashes must match
within each pair. Reports, provider usage and tool events are retained. Reviews
cannot execute game actions, so this measures recommendations rather than combat
outcomes or wins. One sample per prompt/position cannot establish reliability or
separate the prompt's effect from model variation.

## Decisions observed

| Position | Before | After |
| --- | --- | --- |
| Exposed Greek Settler; no foreign capital discovered | Demand that Greece not settle nearby; develop and scout; make war conditional on finding a reachable capital. | Recommend capturing or blocking the Settler immediately, citing denial of Greek expansion; scout while developing the operation. |
| Athens visible, defense 14, no visible field defenders; Roman force rating 173 versus Greece 21 | Concentrate and attack Athens, then repair the economy and seek the remaining capital. | Convert the existing army into an Athens attack within 5–10 turns, with happiness and recovery planning. |
| Athens with Walls, defense 20, four visible Swordsmen and an Archer; Greek force rating 234 versus Rome 173 | Prepare a conditional Athens campaign around turns 55–70, described as the only credible near-term victory step; remain at peace and expand if Greece retains the advantage. | Reject an immediate assault; pursue expansion and science, use the army defensively, and revisit Athens if the local balance changes. |

All six reviews completed and the input hashes match within each pair. The revised
prompt produced different recommendations for a civilian raid, a lightly defended
capital and a stronger opponent. Both prompts already favored the weak-capital
attack; the changed recommendations are in the other two cases.

The exposed-Settler result contains an important error: the revised review expects
to use the captured foreign Settler to found a Roman city. The native engine
converts this capture into a Worker. Denying Greece a settlement is a real benefit,
but the review overstates Rome's direct gain. Its scouting cutoff and the weak-city
review's suggestion to pivot if the final capital is still undiscovered around
turn 50 also remain poorly justified.

The observed change in considering a limited operation does not establish that
the operation is worthwhile or that the broader strategic problems are resolved.

## Usage

Provider usage was complete for all six conversations. Input totals include cached
tokens; output totals include reasoning tokens.

| Position | Prompt | Seconds | Tool calls | Rejected reads | Input tokens | Cached input | Output tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Exposed Settler | Before | 268.4 | 15 | 2 | 327,370 | 282,112 | 20,915 |
| Exposed Settler | After | 208.3 | 17 | 1 | 363,508 | 270,592 | 15,574 |
| Weak capital | Before | 241.7 | 32 | 2 | 664,458 | 580,608 | 17,045 |
| Weak capital | After | 225.7 | 16 | 0 | 299,793 | 239,616 | 16,621 |
| Strong opponent | Before | 110.6 | 25 | 3 | 398,578 | 345,600 | 16,783 |
| Strong opponent | After | 237.4 | 17 | 1 | 210,510 | 131,840 | 17,041 |

Total: 2,264,217 input tokens, including 1,850,368 cached (413,849 uncached);
103,979 output tokens, including 84,185 reasoning tokens. The rejected reads were
recovered within the model conversations. These samples do not establish an
efficiency improvement: cache warmth and different inspection choices affect both
spending and duration.

## Validation and artifacts

- 50 focused Python tests passed, including review lifecycle, frozen inputs,
  dashboard ordering at both terminal sizes, and optional-guide preload behavior.
- Ruff checks, formatting and `git diff --check` passed.
- The native position generator reloaded all three saves and verified the intended
  capital visibility. The Settler is visible while Athens remains undiscovered in
  the first case; Athens is visible in the other two.

Artifacts are under `build/strategic-review-comparison/`: `fixtures/`,
`prepare.gradle`, `positions/`, `baseline.py`, `compare.py`, and `run-3/`.
The first two startup attempts used no model tokens and are retained as `run-1/`
and `run-2/`; an evaluator-relative TLS certificate path caused their failure.
The current running game was not changed.
