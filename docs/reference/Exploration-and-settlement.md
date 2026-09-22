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

Terrain, features, rivers, roads, ownership and explored city names follow the current
map UI. Improvement names use `getShownImprovement(player)`, including remembered
values in fog. Other fields are not a historical copy: for example, the UI currently
exposes road and pillage state on explored tiles. Changes to these UI rules require
corresponding API review. Visible tile yields are now available through the [development slice](City-development.md);
full tile tooltip breakdowns remain outstanding.

`map.frontier` contains unexplored neighbor coordinates, with no terrain attached.
Coordinates are Unciv axial x/y coordinates. Each owned unit has an opaque `handle`,
its location and displayed movement, plus `moves` containing destination, path,
displayed movement cost and a `speculative` flag. Paths exclude the starting tile.
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
That section includes the same diagnostics for the unit's current tile:

| Field | Meaning |
| --- | --- |
| `siteStatus` | `BLOCKED` when a player-visible native site restriction applies; `ELIGIBLE` when the surroundings are fully visible and no site restriction applies; otherwise `UNKNOWN`. |
| `siteBlockers` | Native water, impassable terrain, foreign ownership, or city-spacing restrictions. Visible blocking cities include their name, position, distance, and required minimum distance. |
| `unitBlockers` | Separate current requirements such as movement, a usable founding ability, or One City Challenge. |
| `canFoundNow` | Native action availability at the occupied destination; `null` for other destinations. |
| `minimumCityDistance`, `minimumCityDistanceOnDifferentContinents` | Required center-to-center distances from the selected engine's rules. Vanilla normally requires four hexes on the same continent and three across continents. |
| `uncertainty` | Explains incomplete visibility. An unknown site can still permit the native founding action once occupied. |

Waiting can restore movement; it does not remove city-spacing or foreign-ownership
restrictions. Own territory and adjacent foreign settlers do not themselves forbid
founding. Agent guidance recommends reconsidering repeatedly blocked sites and
scouting or selecting another destination. It adds no movement or founding gate;
Unciv revalidates the actual action normally. The existing settlement inspection
and native promise-confirmation flow remain in place.

Prospective previews include only owned or currently visible blocking cities.
Incomplete surrounding visibility produces uncertainty regardless of whether an
unseen city actually exists, preventing eligibility queries from becoming a hidden
city detector. Unexplored and nonexistent destinations return unknown site details.
The current-tile `canFoundNow` mirrors the existing native action availability;
it does not identify an unseen cause. These filters apply to this diagnostic
surface; the broader map-UI visibility limitations described above still apply.

`UnitSettlementTest` covers native blockers, distances across continents, movement
recovery, ownership, adjacent settlers, and retained UI actions.
`SettlementPreviewIntegrationTest` checks authenticated previews and commands,
read-only behavior, founding/retry effects, and hidden-city non-disclosure.
Python selective-read and movement-order tests cover discovery and advisory-only
behavior through the model-facing gateway.

## Slice inventory and evidence

| Behavior | UI/engine source | Validation |
| --- | --- | --- |
| Explored tiles, fog, resources and remembered improvements | `TileView`, `TileDescription`, `Tile.getShownImprovement`, `Civilization.canSeeResource` | `ExplorationProbe`: hidden terrain/resources leave response and revision unchanged; fog improvement and resource queries compared with UI Views |
| Unit visibility and displayed movement | `MapUnit.isVisibleTo`, `MapUnit.getMovementString` | Engine tests vary unseen enemy units and zones of control; host tests vary unseen unit details |
| Single-turn movement previews and execution | `PlayerUnitOperations`, `MapUnitView`, `WorldMapHolder`, `UnitMovement`, `MovementCost` | `PlayerUnitOperationsTest`: UI View equivalence, actual costs, stacking transit, river costs, hidden obstacles, ownership, spectator mode and exhausted movement |
| Settlement eligibility, confirmation and effects | `UnitSettlement`, `UnitActionsFromUniques`, `PlayerUnitOperations` | Engine tests compare picker eligibility, consumption, city spacing and promise confirmation; host tests reject foreign, expired and out-of-turn handles |
| Fresh creation and first turn handoff | `VanillaGameSetup`, `Main`, `HeadlessRuntime` | `GameLaunchIntegrationTest` and `VanillaGameProbe`: ordinary three-player setup, HTTPS settlement, scheduled rounds and restart; `ExplorationIntegrationTest`: movement and settlement privacy/recovery |
| Settlement durability | `CheckpointStore`, `TurnSession` | `SettlementCrashProbe`: process killed before/after atomic replacement; recovery retries consume exactly one settler and create exactly one city |

The broader engine movement, pathfinding and formation regression suites also run.
These are scripted tests; the new slice has not yet had a live Codex playthrough or
a graphical human-versus-agent acceptance run. Desktop game ownership remains separate
from the headless executor. Multi-turn path previews outside this slice still use
the existing engine pathfinder and require further visibility review.
