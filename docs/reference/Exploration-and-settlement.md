# Exploration and settlement

This slice lets an authenticated player observe the explored map, move ordinary units
within the current turn, found a city, choose production and research, and hand off
the turn. It adds no agent supervisor or filesystem isolation.

## Try a fresh game

Use the [ordinary-game launcher](../guides/Vanilla-game.md) for a fresh game with automatic
credentials for Rome, Greece, and France:

```sh
uv run civ-agent host start --state-dir /tmp/civ-vanilla-1 --new
```

The setup uses Vanilla, Ancient era, Quick speed, Prince difficulty, a Small Pangaea
map, all native victories, and a 330-turn time limit. Barbarians, ruins, and natural
wonders use ordinary settings; city-states and mods are absent. Each player starts
with a Settler and Warrior. Agent services and the scheduler follow committed turns
in Rome/Greece/France order; each agent aims to win.

The host prints `MCP-READY:https://localhost:8765/mcp`. In an interactive agent
session, a focused first-turn check can use:

> Inspect the map and units in player_turn. Move a military unit using one of its
> offered destinations, then found the first city with the settler. Choose research
> and production, resolve supported decisions, and end my turn once. Re-observe
> after each command. Report any unsupported blocker.

Omit `--new` to resume the same checkpoint. `--seed` controls terrain generation;
the engine also uses its random game ID for setup choices, so repeating a seed
does not reproduce the entire match. The Kotlin entrypoint exposes the same new
game through `--new-game --checkpoint PATH --data-dir PATH`; the Python launcher
also provisions credentials and HTTPS configuration.

## Observation and commands

`player_turn.map.tiles` contains explored tiles only. `visible` distinguishes tiles
currently in sight from fog. Resource names require the player's reveal ability;
resource amounts are included only for revealed strategic resources. Units require
both sight and detection of invisibility. Foreign unit IDs, movement points and
orders are omitted. Improvement progress and city health require current sight.

Terrain, features, rivers and roads follow the current map UI. Ownership and city
names use current sight or the host's saved last-seen observations. Improvement
names use `getShownImprovement(player)`, including remembered
values in fog. Other fields are not a historical copy: for example, the UI currently
exposes road and pillage state on explored tiles. Changes to these UI rules require
corresponding API review. Visible tile yields are now available through the [development slice](City-development.md);
full tile tooltip breakdowns remain outstanding.

`map.frontier` contains unexplored neighbor coordinates, with no terrain attached.
Coordinates are Unciv axial x/y coordinates. Each owned unit has an opaque `handle`,
its location and displayed movement, plus `moves` containing destination, path,
displayed movement cost, `unexploredTiles`, and `tilesOutsideCurrentSight` counts. Paths exclude the starting tile.
They can include unknown coordinates because the UI permits movement into the unknown.

Preview pathfinding assumes unexplored tiles cost one movement point and ignores
unseen units and their zones of control. Execution follows the previewed path using
actual movement costs, passability, stacking and movement budgets. It can stop early,
including without displacement; acceptance means the order was processed, not that
the unit reached the requested tile. Re-observe the resulting position and movement.
Movement costs use at most one decimal, matching the movement display's precision.

Submit `MOVE_UNIT` with the latest epoch/revision, the unit handle as `target`, and
an offered `destination: {x, y}`. No future-turn order is installed. Movement eligibility
is rechecked at execution. Special aircraft relocation and paradrop modes are excluded.

Submit `FOUND_CITY` with a unit whose `canFoundCity` is true. If `settlementPromises`
lists leaders, explicitly accept them with `confirmBreakPromise: true`, or choose
another site. Founding consumes the ordinary settler through the same engine effects
as the UI. A retry with the same request ID and payload returns its original receipt,
including after the settler disappears and after restart.

### Settlement diagnostics

For units with a founding ability, `movement_preview(target, destination)` includes
`settlement` alongside the ordinary route. The route's `available` field describes
movement. A reachable tile can still be an unsuitable city site.

The agent overview includes a compact `settlementStatus` and a link to
`unit_details(target, sections=["settlement"])`, even when `canFoundCity` is false.
`settlementStatus` describes the unit's **current tile**. For a standing destination
order, `movementOrder.settlement` describes the **destination** using `siteStatus`,
`siteBlockers`, and `uncertainty`. It is refreshed from the native site rules on
every snapshot, without computing a new route. A scout identifying a blocking city
updates the settler's row in the scout action's `stateUpdate`, even if
the settler has not moved. Unchanged rows are omitted by the ordinary delta logic.

The focused settlement section includes both the active destination summary and
the full diagnostics for the unit's current tile:

| Field | Meaning |
| --- | --- |
| `siteStatus` | On explored tiles, `BLOCKED` or `ELIGIBLE` according to the current native site rules. `UNKNOWN` is reserved for unexplored or nonexistent destinations. |
| `siteBlockers` | Native water, impassable terrain, foreign ownership, or city-spacing restrictions, including restrictions caused by undiscovered cities. Blocking city names and locations require current sight or remembered knowledge; undiscovered cities receive a generic reason without identity, location or distance. |
| `unitBlockers` | Separate current requirements such as movement, a usable founding ability, or One City Challenge. |
| `canFoundNow` | Native action availability at the occupied destination; `null` for other destinations. |
| `minimumCityDistance`, `minimumCityDistanceOnDifferentContinents` | Required center-to-center distances from the selected engine's rules. Vanilla normally requires four hexes on the same continent and three across continents. |
| `uncertainty` | Explains that the destination itself is unexplored; null for explored sites, even with fog nearby. |

Waiting can restore movement; it does not remove city-spacing or foreign-ownership
restrictions. Own territory and adjacent foreign settlers do not themselves forbid
founding. Agent guidance recommends reconsidering repeatedly blocked sites and
scouting or selecting another destination. It adds no movement or founding gate;
Unciv revalidates the actual action normally. The existing settlement inspection
and native promise-confirmation flow remain in place.

The settlement lens deliberately exposes current eligibility on explored tiles,
including effects of unseen founding, razing and border changes. This is an explicit
exception to ordinary map privacy. It does not reveal undiscovered city names,
locations, owners or distances, and it does not refresh remembered map facts.
Known blocking city details retain `observedTurn` and `remembered:true` under fog;
eligibility itself always follows current native rules. Unexplored and nonexistent
destinations return unknown site details. The same policy applies to standing-order
summaries. Destination summaries omit
current unit requirements such as remaining movement. They disappear when the
native order clears on arrival or cancellation, and units without a founding
ability have no destination settlement summary. Site advice never cancels or
redirects an order.
The current-tile `canFoundNow` mirrors native action availability, including separate
unit requirements. Movement, combat and other map observations retain their existing
visibility policy.

The turn-start `settlement` overview searches explored tiles, reports `eligibleSites`
and `blockedSites`, and provides up to three scored eligible suggestions. Its advisory
heuristic uses Unciv's yield weights and settlement preferences with permitted
current/remembered inputs. It does not apply native AI diplomatic preferences as
legality restrictions. Status is `SITES_AVAILABLE` or `NO_SITES_IN_EXPLORED_TERRITORY`;
unexplored land may contain more sites. `player_details(section="settlement")` exposes the full
ranked list. Hex distance is not a path estimate; inspect the native route before
issuing a destination order. See [compact observations](Compact-observations.md).

The former uncertain-site counters and candidate uncertainty fields were removed
without compatibility aliases. Checkpoint version 8 stores durable knowledge,
including recent barbarian sightings, and receipts, and rebuilds turn observations
after loading. Earlier checkpoint formats
cannot be resumed with this build; start a new game/checkpoint.

`UnitSettlementTest` covers native blockers, distances across continents, movement
recovery, ownership, adjacent settlers, and retained UI actions.
`SettlementPreviewIntegrationTest` checks authenticated previews and commands,
read-only behavior, founding/retry effects, eligibility under fog without city identity
disclosure, and a coastal settler choosing an eligible neighboring site.
Python selective-read and movement-order tests cover discovery and advisory-only
behavior through the model-facing gateway. State-update tests cover new blockers
revealed by another unit, selective inspection invalidation, unchanged-row
suppression, and clearing an order from the delivered overview.

### Focused live exercise

Generate an isolated fixture with a Roman settler already heading toward a site
blocked by an undiscovered city, a scout, and two built-in AI rivals. One
`gpt-5.6-luna`/medium agent chooses an eligible replacement destination and founds
a city; scouting is optional. The driver issues no gameplay commands. The default limits
are three rounds, twelve model activations, thirty minutes overall, and ten
minutes per activation; the ordinary scheduler limit flags can override these.

Choose a new absolute directory for each run:

```sh
./gradlew generateSettlementLiveFixture -PsettlementLiveDir=/tmp/settlement-check
.venv/bin/python -m tests.live_settlement --state-dir /tmp/settlement-check --port 9121
.venv/bin/python -m tests.verify_settlement_live /tmp/settlement-check
```

The verifier reads the durable model-call journal through a read-only SQLAlchemy
connection and checks native command receipts. It records destination transitions,
turns with settler movement, stationary turns before founding, redirects, and
turns from availability to founding in `settlement-verification.json`. The settler
is supplied at turn zero; the exercise does not measure production time or provide
a controlled before/after speed comparison. A turn-limit stop is expected after
the three completed turns; check the verifier for the settlement result.

Historical exercise, before the settlement lens change: on September 22, 2026,
all three Luna/medium turns completed in
`/tmp/unciv-settlement-live-20260922`; all six verifier checks passed. The scout
revealed Orleans blocking `(4,0)`, and its action update delivered that fact while
the settler remained at `(0,2)`. Rome issued one replacement order to `(2,4)` on
turn 0 and founded Antium on turn 1. There were no stationary turns before the
founding turn and no tile-by-tile settler commands. The native city action remained
available even though surrounding visibility left the site's status `UNKNOWN`.

That run used the former surrounding-visibility policy and is not acceptance evidence
for the current lens. The current fixture expects an immediate blocked-site result
and a replacement destination. The retained `settlement-live.json` and per-turn
histories record the prompts used in the historical run.

## Slice inventory and evidence

| Behavior | UI/engine source | Validation |
| --- | --- | --- |
| Explored tiles, fog, resources and remembered improvements | `TileView`, `TileDescription`, `Tile.getShownImprovement`, `Civilization.canSeeResource` | `ExplorationProbe`: hidden terrain/resources leave response and revision unchanged; fog improvement and resource queries compared with UI Views |
| Unit visibility and displayed movement | `MapUnit.isVisibleTo`, `MapUnit.getMovementString` | Engine tests vary unseen enemy units and zones of control; host tests vary unseen unit details |
| Single-turn movement previews and execution | `PlayerUnitOperations`, `MapUnitView`, `WorldMapHolder`, `UnitMovement`, `MovementCost` | `PlayerUnitOperationsTest`: UI View equivalence, actual costs, stacking transit, river costs, hidden obstacles, ownership, spectator mode and exhausted movement |
| Settlement eligibility, confirmation and effects | `UnitSettlement`, `UnitActionsFromUniques`, `PlayerUnitOperations` | Engine tests compare picker eligibility, consumption, city spacing and promise confirmation; host tests reject foreign, expired and out-of-turn handles |
| Fresh creation and first turn handoff | `VanillaGameSetup`, `Main`, `HeadlessRuntime` | `GameLaunchIntegrationTest` and `VanillaGameProbe`: ordinary three-player setup, HTTPS settlement, scheduled rounds and restart; `ExplorationIntegrationTest`: movement and settlement privacy/recovery |
| Settlement durability | `CheckpointStore`, `TurnSession` | `SettlementCrashProbe`: process killed before/after atomic replacement; recovery retries consume exactly one settler and create exactly one city |

For multi-turn orders and their live acceptance evidence, see
[naval gameplay](Naval-gameplay.md). Desktop human-versus-agent acceptance and
broader pathfinding visibility review remain separate work.
