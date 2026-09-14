# Great People, research and policies

The Vanilla exercise covers Great Person abilities together with the information
needed to choose research and policies.

## Player API

`player_turn.map.units[].greatPerson` provides the unit's public description and
its abilities. Each ability includes a short, unit-local `name`, its title and
effects, availability and reasons, whether it consumes the unit, and applicable
science, culture, production, Golden Age duration or improvement fields. The
General's passive combat bonus is described on the unit and remains part of the
ordinary combat calculations.

After reading a fresh `player_turn`, send `ACTIVATE_GREAT_PERSON` with the owned
unit's handle as `target` and the offered ability's `name` as `selection`. The
ability acts at the unit's current tile; move first when necessary. Arbitrary
targets, extra destinations, unsupported abilities and stale observations reject.
A research/policy detail query alone does not acknowledge unit abilities or popups.

`receipt.greatPerson` records unit consumption, improvement changes, gained visible
territory, research and production changes, gold/culture changes and Golden Age
duration before and after. Re-observe to inspect tile yields, construction queues,
new technology messages and any new choices. Identical successful requests replay
their saved receipts, including after the original unit has disappeared.

The shared engine operations implement science/culture boosts, construction hurry,
Golden Ages and instant improvements. The supported Vanilla duel covers Academy,
Manufactory, Customs house, Landmark and Citadel, alongside Scientist research,
Engineer construction and Artist/General Golden Ages. Ordinary building hurry
leaves **at least one production** for normal construction; it need not finish the
building immediately. Scientist output depends on the civilization's recent
science history and can be zero early in a game. Read the offered amount.

Merchant city-state trade missions are explicitly unavailable in the player API:
city-state play remains outside the supported duel. The shared engine operation
and desktop validation cover the mission's existing behavior. Religion, other
expansion abilities and complete overview/UI parity remain separate coverage gaps.

## On-demand decision details

`research_details` and `policy_details` accept an optional `names` array. Omitting
it returns a catalogue without long descriptions; requesting up to 16 exact names
returns those entries with their desktop descriptions.

- Research includes prerequisites, costs, current progress, immediate research
  and free-choice eligibility. Named entries also include the engine's ordered
  `prerequisiteQueue` and its availability. An unavailable immediate choice may
  still have a legal queue through its prerequisites. Apply the whole offered path
  with `SET_RESEARCH_QUEUE`; free technology choices still need prerequisites.
- Policies include stored culture, culture per turn, next adoption cost, free
  choices, branches, era/prerequisite requirements and unavailable reasons. Named
  branches include completion effects. Branch completion entries are automatic;
  adopt the remaining member policies instead of issuing a command for the finisher.

These queries contain public rules and the bound player's permitted state. They
do not choose research, adopt policies, expose another player's counters or enlarge
the ordinary turn snapshot with full rule descriptions.

## Regression fixtures

Internal fixtures give both players a capital, prepared Great People, a staffed
University, a National College queue, near-threshold Scientist points and one
remaining Liberty policy. Scripted tests exercise research, improvements, Golden
Ages, construction hurry, policy completion and natural generation. They are
independent of the [trade-cycle gameplay scenario](Trade-cycle-scenario.md).

## Verification

`PlayerGreatPersonOperationsTest` checks shared desktop execution, effects,
consumption, stale actions, ownership/turn restrictions, immutable pure queries
and hidden-resource invariance. It also checks the culture, wonder-hurry and
city-state mission engine paths independently of this repository's fixture.

`GreatPeopleRunIntegrationTest` launches scripted HTTPS clients. It
checks both players against native state, including the five improvements, science,
construction completion, Golden Ages, Liberty's free choice and natural Scientist
generation. Separate processes check mid-turn persistence, complete turn-start
rollback, committed handoffs and exact retries. Observation checks cover hidden
rival counters, waiting players, detached data and the on-demand descriptions.

```sh
./gradlew :Unciv:tests:test --tests com.unciv.logic.civilization.PlayerGreatPersonOperationsTest test --tests uncivai.GreatPeopleRunIntegrationTest
```

This scripted acceptance does not establish a completed live-model duel or visual
desktop acceptance. Audit a live run against its native checkpoint and persisted
receipts, rather than relying on agent summaries.
