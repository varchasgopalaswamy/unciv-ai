# Benchmark agent play audit — October 6, 2026

This audit compares the five model-controlled civilizations with the one built-in
AI control in each of the four live `benchmarks/all-luna` games. The snapshot was
taken at approximately 21:04 America/New_York on October 6 while the games were
still running. Scores and current-turn details will continue to change.

The comparison is diagnostic rather than a controlled estimate of win rate. Each
game has only one built-in control, and civilization and starting-position
assignments differ. Current score is also not identical to victory probability:
a specialized scientific or cultural position can be stronger than its broad
empire score suggests.

## Snapshot

| Game | Turn | Built-in control | Control rank | Best agent | Agent mean |
| --- | ---: | --- | ---: | --- | ---: |
| `g0001` | 116 | France, 374 | 1 | Persia, 309 | 219 |
| `g0002` | 105 | Greece, 248 | 2 | France, 304 | 208 |
| `g0003` | 189 | Inca, 410 | 3 | China, 483 | 403 |
| `g0004` | 184 | Arabia, 524 | 2 | Greece, 616 | 397 |

The control outscored 16 of the 20 agent positions. The best agent outscored the
control in three games, so the ceiling is competitive, but performance is not
consistent.

Across the 20 within-game agent/control comparisons, agents were ahead in:

| Measure | Agent positions ahead of their control |
| --- | ---: |
| Current science output | 17 of 20 |
| Technologies | 13 of 20 |
| Military force | 13 of 20 |
| Total score | 4 of 20 |
| Production | 4 of 20 |
| Cities | 2 of 20 |
| Population | 1 of 20 |
| Wonders | 2 of 20 |

The agent positions averaged approximately 3.1 cities, 28 population, 46
production, and 1.1 wonders. The four controls averaged 3.8 cities, 34 population,
62 production, and 2.5 wonders. Agent treasuries averaged about 691 gold, versus
93 for the controls. Agents usually preserved positive happiness while the
controls accepted temporary unhappiness to build larger empires.

The recurring failure is therefore not research or force accumulation. It is
conversion: technology, armies, happiness, and gold are not reliably turned into
population, production, territory, wonders, city-state support, or concrete
victory progress.

## Per-game assessment

### Game 1

Built-in France was decisively ahead. It had four cities, three wonders, and 65
production; no agent had more than three cities or two wonders. France gained 93
score during the preceding 20 turns, faster than any agent.

Greece declared war on France on turn 74 and made peace on turn 85. Greece had
created four cities but owned three at the snapshot, and the surviving diplomacy
state credited France with capturing Greek cities. The missing city no longer
existed, consistent with France capturing and razing it. Greece had since founded
or reacquired a third city but remained far behind.

### Game 2

Agent France was the clear leader: 304 score versus 248, 24 technologies versus
15, and approximately 107 current science versus 16 for built-in Greece. The
control nevertheless had four cities and 58 production versus France's three and
38, and its recent score growth was much faster.

Built-in Greece had only about 27 military force and three military units, while
every agent had substantially more force. No agent exploited that vulnerability.
Persia instead fought Arabia from turns 87–97 and accepted a white peace without
territorial gains.

### Game 3

This was the most competitive collective showing. China had a balanced lead:
four cities, 36 technologies, three wonders, 70 production, and 717 force, versus
the Incan control's three cities, 29 technologies, two wonders, 58 production,
and 574 force. Arabia was narrowly ahead of the control, and Persia was nearly
tied. Every agent had researched more technologies than the control.

France had 1,804 force and 102 production against Inca's 574 and 58. Its first
war, from turns 161–176, captured no city but extracted 154 gold in the peace.
France declared war again when the treaty expired on turn 186. Its current army
was melee-heavy and had little ranged or siege support, despite its overwhelming
nominal strength. Persia's separate war with Greece had only just begun.

### Game 4

Greece was the standout agent, with seven wonders, 105 production, and roughly 84
culture per turn. Its lead was victory-specific: without wonder score, built-in
Arabia still had the stronger broad empire because of its greater population and
territory. Greece's culture and production nevertheless made the wonder advantage
part of a coherent cultural path.

The other four agents trailed Arabia by 158–203 points despite all five agents
having more technologies. Persia attacked Arabia on turn 151, obtained 94 gold in
the turn-162 peace, and gained no territory. Persia had also remained at war with
Tyre since turn 60 without capturing the city-state.

## Recommended changes

### 1. Add an empire-conversion audit to strategic review

Every review should compare cities, population, production, treasury, happiness,
military investment, recent growth, and measurable victory progress against known
rivals. It should explicitly state:

> A technology or military-strength lead is not a winning position unless it is
> being converted into population, production, territory, wonders, city-state
> support, or concrete victory progress.

This comparison must use only information the player is natively permitted to
know. It is advisory and must not change native legality.

### 2. Produce a compact persistent execution plan

Strategic review should leave a small plan for later turns:

```text
Primary victory:
Next 10-turn objectives:
Expansion objective:
Treasury reserve and planned purchases:
Military objective:
Current target:
Conditions for changing strategy:
```

Gameplay should assess actions against these objectives instead of rebuilding a
new high-level strategy every turn. The plan remains model-authored advice; it
must not prohibit native-legal actions.

### 3. Turn unused gold and happiness into explicit decisions

- Spend gold on a useful building, unit, upgrade, settler, or agreement unless a
  specific near-term obligation justifies the reserve.
- When happiness is positive and the empire is smaller than known rivals,
  actively evaluate founding or acquiring another city.
- Do not preserve a large happiness buffer solely to avoid temporary
  unhappiness.
- A review with a large treasury should name either a purchase or a reason and
  deadline for retaining the gold.

These are decision prompts, not host-enforced economic rules.

### 4. Require an executable war plan

Before declaring war, identify a target city or other concrete objective, staging
location, force composition, ranged or siege requirement, five-turn operational
goal, and conditions for peace or withdrawal.

While at war, request an additional review after approximately five turns without
city damage, a capture, or meaningful destruction of enemy forces. The review
should diagnose positioning, force concentration, ranged and siege support, and
whether the target remains realistic. It should compare a peace offer with the
original objective rather than accepting merely because ten turns have passed.
It should also avoid redeclaring with the same composition after an unchanged
war.

### 5. Search explicitly for temporary opportunities

Reviews should identify known rivals with unusually weak militaries, exposed
cities, or production committed elsewhere, and decide whether the window is
actionable. This would have made game 2's nearly undefended Greek control a
deliberate strategic question instead of an ignored fact.

### 6. Make victory paths era-appropriate and measurable

- Science requires population, sustained science, and eventual project-building
  capacity, not ancient or medieval speculation about Apollo.
- Domination requires city or capital captures, accessible targets, and suitable
  force composition, not just a large army.
- Culture requires culture rate, policies, and relevant wonders.
- Diplomacy requires actual city-state influence and alliances.

A chosen path that makes no measurable progress across two reviews should be
reconsidered. Score itself is diagnostic, not the objective.

### 7. Limit unproductive diplomacy

Communication should support trades, coalitions, intelligence, peace terms,
deception, or another concrete objective. Repeated acknowledgements, milestone
status reports, and broad non-aggression promises should not consume attention or
close strategic options without a corresponding benefit. Social commitments
remain nonbinding advice and never prevent a native-legal action.

### 8. Supply a compact permitted comparative snapshot

Strategic review would benefit from a summary of permitted own and known-rival
information: 20-turn changes, comparative rankings, city and production state,
available purchases, army composition by role, war duration and consequences,
and victory-path progress. This should summarize existing permitted information,
not reveal fogged or private opponent state.

## Validation plan

Use fixed checkpoints so prompt variants see the same starting state. Suitable
cases from this batch are:

- Game 2 near turn 100: does an agent recognize built-in Greece's military
  vulnerability and assess an actionable attack?
- Game 3 near turns 160–176: does France formulate a city-capture plan, obtain
  suitable ranged or siege support, and evaluate peace against its objective?
- Game 4 near turn 60: does Persia either prosecute the Tyre war effectively or
  disengage instead of sustaining an indefinite war?
- Any checkpoint with high gold, positive happiness, and a city deficit: does the
  agent convert resources into expansion or production, or record a concrete
  reason not to?

Compare the current prompt with the proposed conversion audit and structured plan
using the same model, reasoning effort, save, and bounded number of turns. Inspect
native actions and consequences rather than judging only the prose or declared
victory label. Repeat trials before attributing differences to the prompt.

The first implementation should be limited to the comparative conversion audit,
compact plan, treasury and expansion decisions, and five-turn wartime progress
review. Additional general prose about taking domination seriously is unlikely to
help without these operational checks.
