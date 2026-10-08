# Benchmark play review — October 7, 2026

## Scope and evidence

Reviewed the four `benchmarks/all-luna` games using saved `exploration.json`
positions, native civilization ranking histories, saved assignments, monitoring
SQLite records, and the latest completed strategic advice in each player’s memory
database. All 20 model seats use `gpt-5.6-luna` at `max`, with general and
civilization guides enabled. This batch cannot compare different model families.

At inspection, g0002 was running; g0001, g0003 and g0004 were stopped for
inspection following turn-finalization/protocol, host timeout, and heartbeat JSON
errors respectively. These are unfinished games, not gameplay defeats.
Saved positions were at turns 175, 181, 231 and 191. To avoid comparing players
at different points in an unfinished round, score, population, production,
force, treasury and technology comparisons below use the last completed round’s
native ranking history: 174, 180, 230 and 190. Science and culture use the latest
stored turn outputs; queues and army composition use the saved position.

The batch uses Prince, Standard speed, Vanilla rules, one fixed terrain seed
(19238721), and randomized civilization assignments and starting-position seeds.
Four games with one native control each are diagnostic evidence, not a win-rate
estimate. Different starts and civilizations are confounders. Operator inspection
includes full saved state; advice supplied to players must use only information
they are permitted to observe.

## Position assessment

| Game | Completed round | Built-in control | Best model seat | Model mean score | Assessment |
| --- | ---: | --- | --- | ---: | --- |
| g0001 | 174 | France: 622 | Persia: 438 | 338 | Native AI clearly ahead in broad development |
| g0002 | 180 | Greece: 458 | France: 449 | 359 | Native AI narrowly leads score; France has a credible science advantage |
| g0003 | 230 | Inca: 491 | China: 566 | 476 | China and Arabia lead score; France ties the control |
| g0004 | 190 | Arabia: 533 | Greece: 629 | 413 | Greece leads on a coherent cultural route; other model seats trail |

Three model seats strictly exceed their control’s completed-round score, one
is tied, and sixteen trail. Every model seat has more technologies and higher
latest stored science output than its control. None has more population; four
have more production and thirteen have more military force.

| Measure | Model-seat average | Native-control average |
| --- | ---: | ---: |
| Population | 35.0 | 48.2 |
| Production | 57.8 | 78.0 |
| Technologies | 35.6 | 29.8 |
| Latest science output | 164.0 | 88.8 |
| Latest culture output | 25.4 | 44.0 |
| Treasury | 540 | 209 |
| Cities | 3.4 | 3.8 |

Current score is not victory probability. Wonders explain much of the native
score advantage in g0001 and Greece’s model advantage in g0004. Conversely,
the model technology advantage is real, but needs enough future research and
production to finish a victory.

### g0001: native France has the strongest developed economy

France has 38 population and 106 production. Model Persia has 33 and 63;
model Inca has 33 and 36. France’s seven wonders contribute 280 score, against
Persia’s three contributing 120. Removing those contributions still leaves
France ahead, 342 to 318. All models lead its 28 technologies, but no model
has its productive base. China has only two cities and 17 happiness; Persia
holds 1,625 gold. These are opportunities to evaluate investment, not proof
that every available city or purchase would pay off.

### g0002: the earlier French lead has been overtaken on score

The October 6 audit recorded model France at 304 versus native Greece at 248.
Now Greece leads 458 to 449. France still has 40 technologies and about 261
science against Greece’s 27 and 69. Production is virtually tied, 78 versus 79,
so France could be better placed for science despite its score deficit.
Greece has 42 population versus France’s 38 and greater latest growth output.
France runs Science in all three cities while holding about 1,310 gold.
The latest adviser explicitly recommends this, with an 800–1,000 emergency
reserve. Therefore the issue is partly the recommendation itself, not just
failure to execute advice. That reserve and the duration of Science conversion
need a concrete comparison with alternatives.

### g0003: competitive model leaders, with substantial unused military leverage

China has 43 technologies, about 313 science and 81 production versus native
Inca’s 34, 119 and 70. Arabia also leads score. However, Inca has 64 population
against China’s 46 and much greater latest growth output: the native economy
continues compounding. China’s four cities all run Science while it holds 1,770
gold. Keeping that gold for Labs, upgrades or resources can be sensible, but
should include purchase timing and the cost of waiting.

France has 133 production and 2,770 force against Inca’s 70 and 290, yet ties
its score. All four French cities were founded by France; its current territory
shows no retained conquest gains. Its army now has two Artillery and seven
Foreign Legions, a material improvement over yesterday’s melee-heavy composition.
The latest advice identifies Corinth and an occupying unit. Operational play
has improved; whether that plan achieves useful results remains unresolved.

### g0004: the cultural exception should be preserved

Model Greece has seven wonders, 109 latest culture output and 83 production.
Native Arabia has four wonders, 49 population and 57 production versus Greece’s
37 population. Removing wonder points reverses their score order, but Greece’s
culture, Opera Houses and planned Hermitage make its specialization meaningful.
It is a credible cultural position, not proof of an eventual cultural victory.
Other model seats have technology leads but much weaker production. Persia has
848 force and about 1,018 gold, against Arabia’s 511 force; the proposed Baghdad
operation remains a prospective opportunity rather than a demonstrated gain.

## Why the models are usually behind

The recurring weakness is converting advantages into sustained development or
victory progress. Roughly 38 of 67 model-owned cities currently run perpetual
Science; none of the 15 native-control cities does. Direct conversion may be
optimal for a timed research unlock, but can also postpone growth, Workshops,
Factories, culture, happiness or an operational military objective. High current
science partly reflects this choice; it should not automatically be interpreted
as a stronger underlying economy.

The latest advice repeatedly forecasts very large remaining space-research costs,
then continues Science conversion and distant Apollo/Aluminum reporting. Many
reports acknowledge inadequate future output without quantifying the return of
changing today’s investments. Several seats hold significant gold, but this is
not universal: g0002 Persia has only 23 in its completed-round history.

Military force also exceeds useful demonstrated outcomes in several positions.
Force totals cannot establish city-taking capability: travel, concentration,
siege, survival, occupation and retaliation matter. France’s improved g0003
composition is evidence of adaptation, so the earlier audit’s diagnosis should
not be treated as permanent.

## Improvements that preserve playstyle choice

1. **Compare investment returns over the same horizon.** At each review, compare
   continuing the current plan with one or two credible alternatives. Include
   expected gains, cost, time to benefit and uncertainty. For Science conversion,
   compare the next research milestone with the return of an available building,
   purchase, city, policy or limited operation. Allow Science to win that comparison.
2. **Give continuing choices a reason and a revisit point.** Record the purpose of
   a reserve, when it will be spent, and what changes that decision. Likewise,
   identify when perpetual production should be reconsidered. Avoid fixed treasury
   limits, city counts or mandatory spending.
3. **Connect strategy to a few observable outcomes.** Retain the existing advisory
   competing-plan structure. At the next review, compare the preferred plan’s
   expected outcomes with actual changes. Did the intended building finish, science
   unlock pay off, frontier improve or rival slow down? Revise assumptions when it
   did not. Do not require one permanent victory choice.
4. **Evaluate military operations by attainable objectives.** Use city damage,
   captures, pillaging, enemy losses, defensive security or a useful peace settlement
   as appropriate to the model’s aim. Review stalled operations and repeated wars;
   neither conquest nor peace should be mandatory. Use army roles and routes rather
   than force rank alone.
5. **Make intelligence requests decision-relevant.** Ask what current observation
   would change an investment, deployment or diplomatic decision. Prefer that over
   routine reporting of absent, distant terminal projects. Diplomacy should remain
   available for bargaining, coalitions, deception and peace as the player judges.
6. **Improve factual context before adding more instruction prose.** Provide compact
   permitted development trends, city build/purchase choices, construction benefits,
   army roles and completed-plan outcomes. Audit what the pinned running images
   actually supply; current source already contains competing-plan advice and
   development-change support, so proposing these as wholly new would be redundant.

The highest-value experiment is a short investment comparison plus outcome review,
not a fixed build order or a stronger instruction to attack. Test it from identical
checkpoints with the same model and settings for 20–30 turns, using repeated trials.
Include a weak-production Science empire, a military opportunity, and Greece’s
successful cultural specialization. Measure realized development, useful military
outcomes and victory precursors alongside score and model cost. A good change
should improve weak positions without erasing the cultural exception. Keep host
reliability failures separate from gameplay quality when interpreting outcomes.
