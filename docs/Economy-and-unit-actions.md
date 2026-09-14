# Economy and ordinary unit actions

Players can inspect their empire's finances and resource balances, buy units,
buildings and city tiles with gold, and upgrade, pillage or disband land units.
The ordinary desktop purchase and unit-action controls call the same validated
engine operations. This adds a bounded economy slice; it does not complete UI parity.

## Observe and act

Read these fields selectively from `player_turn`:

| Field | Information and UI source |
| --- | --- |
| `economy` | Treasury, gold per turn, expense/income rows, unit maintenance, happiness and supply; `StatsOverviewTab` and the player's civilization statistics |
| `economy.resources` | Net available resources, use, imports, exports, outgoing offers, source rows, unimproved sources and city luxury demands/celebrations; `ResourcesOverviewTab` and the player's own trade agreements |
| `economy.cityConnections` | Own city names/positions, capitals and connection status; `City.isConnectedToCapital` |
| `cities[].purchases.constructions` | Displayed and queued constructions, descriptions, gold costs, required technologies/resources and unavailable reasons; `BuyButtonFactory` / `CityConstructionsView` |
| `cities[].purchases.tiles` | Visible unowned tiles within working range, prices, contiguity and other unavailable reasons; `CityScreen` / `CityView` |
| `map.units[].economy` | Ordinary land-unit upgrade targets and benefits, additional resource requirements, pillage movement/healing, and disband refunds; `UnitActionsUpgrade`, `UnitActionsPillage` and `UnitActions` |

Empire statistics use the overview's rounded display values. Row rounding can
make their sum differ from the rounded total. Resource availability is a net
balance: imports, exports, unit/building use and pending offers are shown
separately. Pending offers are not deducted as consumption. Unrevealed map
resources stay hidden. No opponent treasury, upkeep or private resource inventory
is included. Resource names use the bundled ruleset names, such as `Gold Ore`.

Each command requires an owned handle, a newly read `player_turn` at the submitted
revision, and an available offered option. Read the complete `confirmation` and
send `confirmAction: true` when it is present. A revision from a command receipt
alone does not acknowledge newly offered prices or warnings.

| Command | Arguments beyond request ID, epoch and revision |
| --- | --- |
| `BUY_CONSTRUCTION` | `target`: city handle; `selection`: name from `purchases.constructions` |
| `BUY_TILE` | `target`: city handle; `destination`: position from `purchases.tiles`; omit selection |
| `UPGRADE_UNIT` | `target`: unit handle; `selection`: `targetName` from `economy.upgrades` |
| `PILLAGE` | `target`: unit handle; omit selection and destination |
| `DISBAND_UNIT` | `target`: unit handle; omit selection and destination |

Gold purchases follow native placement and queue validation. Purchases do not use
stored production as a discount. Paid upgrades retain the unit's handle, custom name,
health, experience and promotions where the normal upgrade rules preserve them,
and consume its remaining movement. The additional resource requirement accounts
for resources released by the old unit. Upgrade options explain missing technology,
gold, resources, movement or territory requirements.

Pillaging affects the unit's current tile and never implicitly declares war.
An improvement can heal the unit; pillaging a road does not. Loot follows the
engine's ordinary rules. Disbanding can refund gold in owned territory and reduce
upkeep. Read `receipt.economy` for actual treasury and unit changes, then query the
new snapshot for maintenance, resource balances, buildings and tile state. Turn
reviews also record the confirmed outcomes. Predicted costs and healing are not
substituted for actual receipts.

## Verification

`PlayerPurchaseOperationsTest` covers unit/building/tile purchases, selected queue
semantics, stale prices, insufficient gold/resources, blocked placement, foreign
ownership, inactive players, spectators, puppets, resistance and hidden placement.
It invokes `CityView` and `CityConstructionsView` to check desktop delegation.
`PlayerUnitEconomyOperationsTest` covers paid upgrade identity and resource use,
desktop upgrade execution, stale eligibility, replacement placement, pillage loot,
healing/movement, war restrictions, disband refunds and passenger evacuation.

`EconomyObservationIntegrationTest` checks UI rounding, resource reveal and trade
accounting, immutable detached observations, and paired worlds differing only in
hidden opponent data. Observation must not refresh mutable engine caches or
change serialized game state.

`EconomyRunIntegrationTest` uses authenticated HTTPS in separate host processes.
Rome orders a Worker to mine Iron and waits through scheduled turns. It then buys
a tile, Granary and Archer, upgrades a Warrior, pillages and heals, and disbands
reserve units until maintenance decreases. Assertions read actual engine state.
The test checks unread confirmations, hidden coordinates, wrong credentials,
stale requests and exact receipt retries. It restarts with a partially completed
turn, rolls back to that activation's starting save, repeats the actions, completes
both players' turns and restarts again. Receipt replay must leave checkpoint bytes
unchanged, and scheduled turns must continue.

```sh
./gradlew build :Unciv:tests:test
```

The live `/tmp/civ-economy-1` run was separately audited against native saves and
73 persisted command receipts. Rome completed all economy objectives by turn 5;
scheduling reached turn 12 with 24 handoffs. Greece completed purchases and repaired
its Farm, but the fixture did not give it symmetric upgrade/raid opportunities.
This is bounded live evidence, not a completed duel or visual desktop acceptance.

## Regression fixtures

The economy setup is maintained inside `EconomyRunProbe` for scripted purchase,
resource, upgrade, pillage and disband checks. It has no separate gameplay
launcher or agent prompt. Use [trade-cycle](Trade-cycle-scenario.md) for the
current gameplay exercise.

## Remaining coverage

Purchases with faith or other currencies and constructions requiring an improvement
placement picker remain unsupported by these commands. Those desktop workflows
retain their existing paths. Land-unit operations do not cover naval/air upgrades,
domain-changing or relocating upgrades, special/free upgrade effects, gifting,
renaming or automation. [City management](City-management.md) covers focus,
specialists, growth, building sales and shared tiles. Full overview tooltips,
general trade and notifications still have coverage gaps.
See the [coverage ledger](Player-api-coverage.md) for the broader inventory.
