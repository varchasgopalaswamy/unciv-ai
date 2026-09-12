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

## Launch the exercise

Use a fresh directory. Run the first three commands in separate terminals, waiting
for `MCP-READY` and both `AGENT-READY` messages before starting the match:

```sh
uv run civ-agent host start --state-dir /tmp/civ-great-people-1 --scenario great-people --port 8776
uv run civ-agent agent-server start Rome --state-dir /tmp/civ-great-people-1 --port 8899
uv run civ-agent agent-server start Greece --state-dir /tmp/civ-great-people-1 --port 8900
uv run civ-agent match start --state-dir /tmp/civ-great-people-1 --model gpt-5.6-luna --reasoning-effort medium --max-turns 6 --max-invocations 16 --max-seconds 3600 --turn-timeout 600
```

Model and reasoning settings are optional operator choices. Agent prompts receive
the exercise instructions through `player_turn.scenario`; no extra pasted gameplay
prompt is needed. Supplying `--scenario great-people` on resume verifies the actual
checkpoint scenario. A directory's name does not select a scenario.

Both players start with one capital, seven prepared Great People, a valid National
College queue, a staffed University, Scientist points just below generation, and
one remaining Liberty policy. The exercise asks each player to:

1. Inspect the Mathematics/Currency research path and hurry research.
2. Hurry National College and verify its completion after a turn advances.
3. Construct a Manufactory, Customs house, Landmark and Citadel.
4. Start a Golden Age, then complete Liberty, select a free General and use its
   Golden Age ability too. Read each resulting popup and decision.
5. Observe the naturally generated Scientist, the increased threshold and the
   remaining points; use that new person to construct an Academy.
6. Continue ordinary production and research through scheduled turns.

## Verification

`PlayerGreatPersonOperationsTest` checks shared desktop execution, effects,
consumption, stale actions, ownership/turn restrictions, immutable pure queries
and hidden-resource invariance. It also checks the culture, wonder-hurry and
city-state mission engine paths independently of this repository's fixture.

`GreatPeopleRunIntegrationTest` launches the CLI and scripted HTTPS clients. It
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
