# Competing strategy plans — September 30, 2026

The new report format passed the Python suite and four isolated live comparisons:
12 reviews and 36 player turns. It made alternatives more explicit and changed
late-game Rome's recommendation toward conquest. The short replay does **not**
establish stronger play or lower overall cost; early Rome also postponed a
settlement that its baseline completed.

## Implementation

- Reviews compare one to three concrete plans, normally continued development
  against the strongest plausible alternatives. Each gives an objective,
  investment, projection, relative outcome, and risks. Preparation, opportunity
  costs, rival responses, and comparable horizons belong in these comparisons.
- `preferredPlan` is a short one-based reference or null. `reconsiderWhen` states
  what would change the recommendation. An open choice can lead to information
  gathering or reversible investments.
- Brief `victoryOutlooks` cover every enabled victory exactly once. They replace
  the former detailed victory-by-victory projections and separate
  race/alternative/disruption sections.
- Diplomatic initiatives explain the behavior change sought, incentives,
  relative benefit, proposed terms, disclosures, and conditions for reconsidering.
  The existing limit of two initiatives and optional `END_TURN` attribution remain.
- Gameplay and communication receive the complete report once. The Strategy tab
  displays the recommendation, alternatives, reconsideration conditions, advice,
  diplomacy, and victory outlooks.

No additional adviser, gameplay restriction, or quota for war or deception was
introduced. The report remains advice. Old report records are unsupported; there
is no migration or compatibility reader. Use fresh game state with this format.

## Comparison setup

Each pair started from the same saved position with Rome active:

| Position | Review boundary | Player rounds | Final position |
| --- | ---: | --- | --- |
| Early | After round 39 | 40–42 | Turn 43, Rome |
| Late | After round 138 | 139–141 | Turn 142, Rome |

Rome and France used `gpt-5.6-luna`; Greece used `gpt-5.6-terra`, all with maximum
reasoning. Egypt remained built-in AI. Both conditions requested fast priority
and disabled the optional strategy guide. Provider records confirmed settings for
jobs with reported responses.

The baseline is the source immediately before this change. The two source copies
differ only in `player_prompts.py`, `strategic_review.py`, and `strategy_view.py`.
Both used the same installed native host. Each pair shared byte-identical frozen
review inputs, including genuine communication history. Fresh private stores
imported neither old recommendations nor old private notes; old message wakes
were consumed. The source matches were untouched.

The isolated runner seeded pending reviews using the preceding round's native
parent grants and marked the initial boundary unreviewed. Ordinary services then
ran concurrent reviews and normal gameplay. Initial setup attempts stopped before
any model invocation because the scheduler normally considers a newly started
midgame boundary reviewed. The correction was confined to the experiment runner.
There were no further reviews within the three-round window.

## Strategic results

| Position and player | Baseline recommendation | New recommendation |
| --- | --- | --- |
| Early Rome | Scientific | Science infrastructure; compare expansion/defense and culture |
| Early Greece | Scientific | College first; compare immediate expansion and an Egyptian campaign |
| Early France | Scientific | Science development; compare security and armed expansion |
| Late Rome | Conditional Scientific | Modernize, then conquer; compare a science sprint |
| Late Greece | Diplomatic | Diplomatic sprint; compare science and defensive rearmament |
| Late France | Scientific | Science race; compare culture and diplomatic containment |

All six revised reviews supplied two or three plans and selected a preference.
The nullable preference is covered deterministically but was not chosen here.
Every player activation received its condition's complete, correctly dated report.

### Actual follow-through

- Late revised Rome sought temporary Greek neutrality without naming its intended
  French target, accepted a negotiated peace through turn 159 with troop-movement
  notices, began reconnaissance, and improved gold income. Greece preserved
  independent city-state diplomacy in the terms. Baseline Rome also negotiated
  movement notices, so substantive diplomacy was present in both conditions.
- Both late Romes retained their Public Schools and queued Chemistry. Neither
  started a major war in the window. The revised recommendation changed the stated
  purpose and some preparatory actions; it has not demonstrated a successful
  military campaign.
- Both late Greek players took Electricity and Refrigeration through their free
  technologies. Revised Greece also spent a Great Scientist on Plastics to
  accelerate Research Labs; baseline Greece retained its Scientists.
- Both early pairs completed the same Greece–France luxury exchange. Both late
  pairs completed Wine-for-Cotton between Rome and Greece. Revised Rome additionally
  accepted Egypt's Incense for Wine plus 158 gold. More trades are not, by
  themselves, evidence of better terms or winning chances.
- Early baseline Rome founded its fourth city on turn 42, purchased its Library
  to avoid delaying National College, and used city bombardment against a nearby
  Galley. Revised Rome deferred the unhappy fourth city and fortified its injured
  escort. The latter leaves the expansion and civilian protection problem open;
  the new review format did not reliably improve this decision.

The reports still contain questionable assumptions. For example, early revised
Greece described the existing peace terms as making Rome and France unavailable
as targets, despite instructions allowing broken social promises. Those words
did not create a host restriction. This remains model interpretation to assess in
longer play, not a reason to require aggression.

## Recorded usage

Counts include review, gameplay, and communication jobs. Tool calls exclude
service preloads and polling, and include reviewer reads. Input includes cached
input; output includes reasoning.

| Measurement | Early baseline | Early new | Late baseline | Late new |
| --- | ---: | ---: | ---: | ---: |
| Completed player turns | 9 | 9 | 9 | 9 |
| Completed jobs | 20 | 18 | 23 | 21 |
| Interrupted chat jobs at stop | 0 | 0 | 2 | 2 |
| Recorded model requests | 125 | 126 | 165 | 191 |
| Model-facing tool calls | 145 | 132 | 156 | 175 |
| Input tokens | 5,084,463 | 4,849,919 | 6,829,003 | 8,587,137 |
| Cached input tokens | 4,363,264 | 4,113,408 | 6,052,864 | 7,684,096 |
| Uncached input tokens | 721,199 | 736,511 | 776,139 | 903,041 |
| Output tokens | 128,159 | 115,737 | 142,683 | 148,587 |
| Reasoning output tokens | 101,359 | 86,433 | 106,074 | 120,376 |
| New messages | 6 | 4 | 12 | 8 |
| Gameplay/communication tool errors | 1 | 0 | 3 | 1 |
| Mean report characters | 8,898 | 7,581 | 7,731 | 8,485 |
| Wall time, seconds | 1,057 | 957 | 1,038 | 1,129 |

Reported usage reconciled with provider records for all 82 completed jobs and
the two interrupted chats that had reported responses. Two other interrupted
chats stopped before their first usage record. The four interrupted jobs have
incomplete accounting; late-game totals exclude any unreported in-flight spend.
They were stopped by the ordinary three-round limit, not by a gameplay failure.

Review generation itself used fewer requests in both new runs: 23→21 early and
34→29 late. Overall requests were nearly unchanged early and 16% higher late;
uncached input rose 2% and 16%, respectively. Different actions and negotiations,
one sample per condition, and overlapping runs prevent an efficiency or latency
claim.

Report size fell 15% early and rose 10% late. Only one revised report met the
suggested 6,000-character target. The new instructions and schema add 1,184
characters; factual preload sizes are unchanged. Conciseness remains uneven.

Recoverable errors comprised one early baseline stale-preview observation,
two late baseline unavailable trades, and one invalid commitment source in each
late condition. The source-reference mistakes were corrected. No report failed
schema validation, and no player turn stopped on an error.

## Validation and artifacts

- Full Python suite through `codex-log`: **1,058 passed, 76 subtests passed,
  two opt-in checks skipped**.
- Ruff, formatting, Pyrefly, and whitespace checks passed.
- Tests cover plan limits/references, null preferences, exact victory coverage,
  single report delivery to gameplay and communication, and complete TUI rendering.
- Live verification checked source/engine/input hashes, all 36 completed turns,
  report validity and delivery, actual model settings, and available provider usage.

Artifacts are under `/tmp/unciv-plan-comparison-20261001/`: `protocol.json`, frozen
sources and positions, `run.py`, `launch.py`, four isolated match directories,
`results.json`, `summary.json`, and the collection/verification scripts. The Python
suite log is `/tmp/codex-output.Z6TpW3`.
