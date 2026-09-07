# Exploration and settlement

This slice lets an authenticated player observe the explored map, move ordinary units
within the current turn, found a city, choose production and research, and hand off
the turn. It adds no agent supervisor or filesystem isolation.

## Try a fresh game

For automatic credential setup and per-civilization Codex sessions, use the
[local launchers](Local-launchers.md). The lower-level commands below also remain
available.

From the repository directory, with the `players.json` containing Rome/Greece tokens
from the earlier manual test, stop the previous host and run:

```bash
export UNCIV_HOST_DIR="$HOME/.local/state/unciv-host/demo"
./gradlew run --console=plain --args="\
--new-duel \
--data-dir $UNCIV_HOST_DIR/exploration-data \
--checkpoint $UNCIV_HOST_DIR/exploration.json \
--mcp-config $UNCIV_HOST_DIR/players.json"
```

The server prints `MCP-READY:http://127.0.0.1:8765/mcp`. Use the same client connection
and token as before. In that client, try:

> Use only the civ MCP tools for game information and actions. Inspect the map and
> units in player_turn. Move a military unit using one of its offered destinations,
> then found the first city with the settler. Choose research and production, resolve
> supported decisions, and end my turn once. Re-observe after each command. Report
> any unsupported blocker.

This creates a new game without replacing the earlier demo checkpoint. To resume it:

```bash
./gradlew run --console=plain --args="\
--resume-checkpoint $UNCIV_HOST_DIR/exploration.json \
--data-dir $UNCIV_HOST_DIR/exploration-data \
--mcp-config $UNCIV_HOST_DIR/players.json"
```

Rome acts first. The fixed setup uses Vanilla, Ancient era, Quick speed, a Tiny
Pangaea map, domination and time victories (330 turns), and no city-states, barbarians,
ruins or mods. Natural wonders remain possible. `--seed` controls the engine's map
seed, while the engine also uses the random game ID for setup choices; repeating
that argument does not reproduce the entire match.

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
corresponding API review. Yield estimates and tile tooltip breakdowns remain absent.

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

## Slice inventory and evidence

| Behavior | UI/engine source | Validation |
| --- | --- | --- |
| Explored tiles, fog, resources and remembered improvements | `TileView`, `TileDescription`, `Tile.getShownImprovement`, `Civilization.canSeeResource` | `ExplorationProbe`: hidden terrain/resources leave response and revision unchanged; fog improvement and resource queries compared with UI Views |
| Unit visibility and displayed movement | `MapUnit.isVisibleTo`, `MapUnit.getMovementString` | Engine tests vary unseen enemy units and zones of control; host tests vary unseen unit details |
| Single-turn movement previews and execution | `PlayerUnitOperations`, `MapUnitView`, `WorldMapHolder`, `UnitMovement`, `MovementCost` | `PlayerUnitOperationsTest`: UI View equivalence, actual costs, stacking transit, river costs, hidden obstacles, ownership, spectator mode and exhausted movement |
| Settlement eligibility, confirmation and effects | `UnitSettlement`, `UnitActionsFromUniques`, `PlayerUnitOperations` | Engine tests compare picker eligibility, consumption, city spacing and promise confirmation; host tests reject foreign, expired and out-of-turn handles |
| Fresh creation and first turn handoff | `DuelSetup`, `Main`, `HeadlessRuntime` | `ExplorationIntegrationTest`: CLI creates game, scripted MCP clients move/settle/research/build/end turn, separate process restores references and settlement receipt |
| Settlement durability | `CheckpointStore`, `TurnSession` | `SettlementCrashProbe`: process killed before/after atomic replacement; recovery retries consume exactly one settler and create exactly one city |

The broader engine movement, pathfinding and formation regression suites also run.
These are scripted tests; the new slice has not yet had a live Codex playthrough or
a graphical human-versus-agent acceptance run. Desktop game ownership remains separate
from the headless executor. Multi-turn path previews outside this slice still use
the existing engine pathfinder and require further visibility review.
