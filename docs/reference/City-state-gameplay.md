# City-state gameplay

This sprint exposes ordinary native city-state interactions to player sessions.
The engine remains authoritative for costs, influence, quests, alliances, gifts,
tribute, protection, war and peace. Social promises remain advisory.

## Delivery and acceptance

- [x] Shared engine operations used by desktop controls and the host, including
  stale-action revalidation and native unit gifts.
- [x] Known city-state observations: relationships, influence and decay, allies,
  protectors, resources, bonuses, assigned quests and offered actions.
- [x] Host commands, receipts, compact observations, model instructions and memory;
  enable the existing Great Merchant trade mission operation.
- [x] Native and HTTPS integration regressions, privacy, exact retries, restart
  and interrupted checkpoint recovery.
- [x] Visual desktop comparison.
- [x] Guided live Rome–Greece–France exercise with two city-states using
  `gpt-5.6-luna` with medium reasoning: alliance competition, quests, gifts,
  protection, tribute and a trade mission, including restart.

## Native inventory

`CityStateDiplomacyTable` offers Gold gifts of 250/500/1000, eligible resource
improvements for 200 Gold, protection/revocation, Gold/Worker tribute, war and
direct city-state peace. Peace obeys the native declared-war cooldown and wars
with the city-state's ally. Unlike peace with a major player, there is no
negotiating counterparty or editable trade offer; the native button enacts the
bilateral peace treaty immediately.

`UnitActions` gifts eligible units standing in city-state territory; ordinary
civilian units and transported units are excluded by the native controls.
`PlayerGreatPersonOperations` already implements trade missions.

Native diplomatic marriage is conditional on a civilization unique; expansion
espionage, religion and mod content remain outside this Vanilla sprint. Existing
major-player responses to city-state incidents are reused.

## Player contract

`player_details(section="diplomacy")` exposes `cityStates` for met city-states.
Each entry includes influence, its resting point and decay, relationship and
alliance, protection and cooldowns, relationship bonuses and revealed resources,
the player's assigned quests, contest standings, defense requests, and native
tribute willingness. Quest text retains its native meaning; the host does not
create objectives or adjudicate completion. Compact observations summarize
relationships and quests; detailed observations supply action names and reasons.

| Operation | Discovery and command |
| --- | --- |
| Gold gifts | `CITY_STATE_ACTION`, city-state ID as `target`, offered `giftGold250`, `giftGold500`, or `giftGold1000` as `selection` |
| Resource improvement | Same command with the exact offered `giftImprovement:…` name; the engine checks native eligibility and the 200 Gold cost |
| Protection or revocation | Same command with `pledgeProtection` or `revokeProtection`; requires `confirmAction` when confirmation text is present |
| Gold or Worker tribute | Same command with `goldTribute` or `workerTribute`; native military pressure, influence effects and cooldowns apply |
| City-state peace | Same command with `makePeace`; native immediate bilateral treaty, declared-war cooldown and allied-war restrictions |
| City-state popup response | `RESPOND_CITY_STATE_ALERT`, owned alert handle as `target`, and offered `nativeDiplomacy.options.name` as `selection`; the overview supplies copyable commands |
| War | Existing `DECLARE_WAR`, city-state ID as `target`, native confirmation |
| Unit gift | `GIFT_UNIT` with the owned unit handle as `target`; inspect `unit_details.gift` first; recipient comes from its current territory. The same field covers eligible major recipients. |
| Great Merchant mission | Existing `ACTIVATE_GREAT_PERSON` with the owned unit handle and the offered mission name |
| Diplomatic marriage | Conditional native `diplomaticMarriage` action when the civilization has the required unique; native annex/puppet decision follows, or destroy under one-city challenge |

All commands use the ordinary epoch, revision and request ID contract. Actions
recheck native eligibility when executed. `receipt.cityState` reports actual Gold
and influence changes, alliance/protection/war changes, removed or completed
quests, gained unit handles, consumed or transferred unit handles, and any funded
improvement. Great Merchant receipts also retain `greatPerson` effects. Retries
return the saved receipt; interrupted checkpoint replacement recovers the complete
old or new state without spending twice.

Marriage receipts also report `gainedCities` and `eliminated`. `influenceAfter` is
null when the minor no longer exists. The transferred city's decision remains
available through `RESOLVE_CAPTURED_CITY` after checkpoint recovery; resolving it
does not reconquer the city or repeat population losses.

### Funding and achievable objectives

A 250 Gold gift requires at least 250 Gold **when executed**. The same rule applies
to the other gift amounts and the 200 Gold improvement. A quest can remain visible
while a player saves up; its existence never authorizes spending unavailable Gold.
Tests check rejection at 249 Gold, success at 250, native quest completion and an
empty treasury afterward. Native improvement tests similarly check 199 versus 200
Gold and a previously offered action after funds change.

Trade continues through the existing negotiation flow. Acceptance rechecks current
Gold and resource availability. The full regression includes intervening spending
and loss of an offered resource, both of which prevent settlement.

The prepared live fixture gives each major at least 5,000 Gold, places a giftable
Warrior and Great Merchant inside Geneva, supplies an eligible Gems improvement,
and places sufficient French military pressure near Sidon. Read-only fixture
checks verify funds, native gift/mission availability, the assigned quests,
protection eligibility and tribute willingness both before saving and after
reloading. This preparation is confined to test fixtures. An ordinary game uses
normal native starting funds and units.

New ordinary games accept `--city-states NUMBER` (default 0). For example:

```sh
uv run civ-agent host start --state-dir /tmp/civ-with-city-states --new --city-states 2
```

The option is rejected for prepared scenarios and resumed games; resume retains
the saved city-states. Native AI controls city-state turns.

## Privacy boundary

Only met city-states and the viewer's assigned quests are exposed. Other players'
private quests, ballots and units are excluded. Unknown allies, protectors and
quest competitors are unnamed. Improvement offers additionally require explored
tiles and a resource visible to the viewer. Observations must not refresh native
alliances or otherwise mutate the game.

A quest that explicitly names another civilization, such as a recent bully in a
gift/protection request, retains that supplied name even if the player has not met
it. This does not establish contact or expose an actionable civilization ID or
location. Other concealed relationship identities remain concealed.

Native notifications also redact unmet civilization identities and icons,
including nested quest-winner names. Explicit native quest locations, such as a
requested barbarian camp, remain available; unrelated hidden improvement tiles
and resource names do not become action offers.

## Evidence

The earlier live acceptance recorded three rounds, nine gameplay
activations, three chat jobs, a planned restart, all 24 artifact checks, and the
13-screen desktop comparison. All model jobs used `gpt-5.6-luna` with medium
reasoning. All services stopped after acceptance.

The full regression passed 92 host JVM tests, 181 related engine tests, and 435
Python tests with 90 subtests before the final focused checks. The final focused
run passed 11 host tests, 16 engine tests, and 23 Python tests with 18 subtests.
It covers the ordinary-game city-state option, nested quest-notification privacy,
fixture preflight, diplomatic marriage and detached contest standings. Formatting,
Ruff and Pyrefly checks pass. Routine validation uses source; no package build is
needed.

Primary test sources are `CityStateIntegrationTest`/`CityStateProbe`,
`PlayerCityStateOperationsTest`, `QuestTests`, `tests/test_city_states.py`, and the
launcher tests. Engine tests stand independently of host fixtures.

This is scoped native city-state coverage. It does not establish every quest's
completion path, every civilization-specific bonus, expansion espionage/religion,
major-player unit gifts, embedded human play, or an Ancient-to-victory game.
Engine commit `10f810a7b3b7700df6f35850e8572d0a44e1a898` is merged and
published on `experimental-changes`. The host records that revision as its
submodule pin. The default `engineInfo build` passed against that published
revision, including all 94 host JVM tests and formatting checks.
