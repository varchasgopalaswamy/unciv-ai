package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerOperations
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.models.ruleset.tile.ResourceType
import kotlinx.serialization.Serializable
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.util.Collections
import java.util.Locale

@Serializable
data class TilePosition(val x: Int, val y: Int)

@Serializable
data class VisibleUnit(val name: String, val civilization: String, val health: Int, val military: Boolean, val atWar: Boolean)

/** Proximity is a warning, not a prediction of enemy movement or an exhaustive danger map. */
@Serializable
data class NearbyHostile(val position: TilePosition, val unit: VisibleUnit, val distance: Int)

@Serializable
data class ObservedTile(
    val position: TilePosition,
    val visible: Boolean,
    val terrain: String,
    val features: List<String>,
    val naturalWonder: String?,
    val resource: String?,
    val resourceAmount: Int?,
    val owner: String?,
    val cityName: String?,
    val cityHealth: Int?,
    val improvement: String?,
    val improvementPillaged: Boolean?,
    val road: String,
    val roadPillaged: Boolean,
    val improvementInProgress: String?,
    val improvementTurns: Int?,
    val riverBottom: Boolean,
    val riverBottomLeft: Boolean,
    val riverBottomRight: Boolean,
    val units: List<VisibleUnit>,
    val yields: List<DisplayedYield>? = null,
)

@Serializable
data class UnitMoveOption(
    val destination: TilePosition,
    val path: List<TilePosition>,
    val movementCost: String,
    val speculative: Boolean,
)

@Serializable
data class OwnedUnit(
    val handle: String,
    val name: String,
    val displayName: String,
    val position: TilePosition,
    val health: Int,
    val military: Boolean,
    val nearbyHostiles: List<NearbyHostile>,
    val movement: String,
    val idle: Boolean,
    val movementSupported: Boolean,
    val moves: List<UnitMoveOption>,
    val canFoundCity: Boolean,
    val settlementPromises: List<String>,
    val worker: WorkerDevelopment? = null,
    val combat: UnitCombat? = null,
    val economy: UnitEconomy? = null,
    val greatPerson: GreatPersonUnit? = null,
    val cityStateGift: CityStateUnitGift? = null,
    val spaceshipAssembly: SpaceshipAssembly? = null,
    val movementOrder: MovementOrder? = null,
    val waterImprovement: WaterImprovement? = null,
)

/** Coordinates use Unciv's axial x/y grid. No tile data is returned for unexplored coordinates. */
@Serializable
data class MapObservation(
    val tiles: List<ObservedTile> = emptyList(),
    val frontier: List<TilePosition> = emptyList(),
    val units: List<OwnedUnit> = emptyList(),
    val wrapOffset: TilePosition? = null,
)

internal fun observeMap(player: Civilization, state: TurnSessionState, canAct: Boolean = true): MapObservation {
    val tiles = player.gameInfo.tileMap.tileList.filter { it.isExplored(player) }
        .sortedWith(compareBy({ it.position.x }, { it.position.y }))
    val operations = PlayerUnitOperations(player)
    val active = canAct && PlayerOperations(player).canAct()
    val format = DecimalFormat("0.#", DecimalFormatSymbols(Locale.ROOT))
    val visibleHostiles = tiles.flatMap { tile ->
        tile.getUnits().filter { it.isVisibleTo(player) && it.baseUnit.isMilitary && player.isAtWarWith(it.civ) }
            .map { tile to VisibleUnit(it.name, it.civ.civName, it.health, military = true, atWar = true) }.toList()
    }
    return MapObservation(
        wrapOffset = player.gameInfo.tileMap.takeIf { it.mapParameters.worldWrap }?.getUnwrappedPosition(HexCoord.Zero)?.dto(),
        tiles = detached(
            tiles.map { tile ->
                val visible = tile.isVisible(player)
                val resource = tile.tileResource?.takeIf { player.canSeeResource(it) }
                val improvement = tile.getShownImprovement(player)
                val city = tile.getCity()?.takeIf { tile.isCityCenter() }
                ObservedTile(
                    position = tile.position.dto(),
                    visible = visible,
                    terrain = tile.baseTerrain,
                    features = detached(tile.terrainFeatures),
                    naturalWonder = tile.naturalWonder,
                    resource = resource?.name,
                    resourceAmount = tile.resourceAmount.takeIf { resource?.resourceType == ResourceType.Strategic },
                    owner = tile.getOwner()?.civName,
                    cityName = city?.name,
                    cityHealth = city?.health?.takeIf { visible },
                    improvement = improvement,
                    improvementPillaged = tile.improvementIsPillaged.takeIf { improvement != null },
                    road = tile.roadStatus.name,
                    roadPillaged = tile.roadIsPillaged,
                    improvementInProgress = tile.improvementInProgress.takeIf { visible },
                    improvementTurns = tile.turnsToImprovement.takeIf { visible && tile.improvementInProgress != null },
                    riverBottom = tile.hasBottomRiver,
                    riverBottomLeft = tile.hasBottomLeftRiver,
                    riverBottomRight = tile.hasBottomRightRiver,
                    yields = observeTileYields(player, tile),
                    units = detached(
                        tile.getUnits().filter { it.civ === player || it.isVisibleTo(player) }
                            .map { VisibleUnit(it.name, it.civ.civName, it.health, it.baseUnit.isMilitary, player.isAtWarWith(it.civ)) }
                            .sortedWith(compareBy({ it.civilization }, { it.name }, { it.health })).toList(),
                    ),
                )
            },
        ),
        frontier = detached(
            tiles.flatMap { it.neighbors.filter { neighbor -> !neighbor.isExplored(player) }.map { it.position.dto() } }
                .distinct().sortedWith(compareBy({ it.x }, { it.y })),
        ),
        units = detached(
            player.units.getCivUnits().filter { operations.owns(it) }.sortedBy { it.id }.map { unit ->
                val canFound = active && operations.canFoundCity(unit)
                OwnedUnit(
                    handle = state.unitHandles.getOrPut(unit.id) { state.identifiers.next(PlayerIdentifier.UNIT) },
                    name = unit.name,
                    displayName = unit.displayName(),
                    position = unit.currentTile.position.dto(),
                    health = unit.health,
                    military = unit.baseUnit.isMilitary,
                    nearbyHostiles = detached(
                        visibleHostiles.mapNotNull { (tile, hostile) ->
                            val distance = unit.currentTile.aerialDistanceTo(tile)
                            if (distance <= 2) NearbyHostile(tile.position.dto(), hostile, distance) else null
                        },
                    ),
                    movement = unit.getMovementString(),
                    idle = unit.isIdle(),
                    movementSupported = operations.supportsMovement(unit),
                    moves = detached(
                        (if (active) operations.moves(unit) else emptyList()).map { move ->
                            UnitMoveOption(
                                destination = move.destination.position.dto(),
                                path = detached(move.path.map { it.position.dto() }),
                                movementCost = format.format(move.movementCost.toDouble()),
                                speculative = move.path.any { !it.isVisible(player) },
                            )
                        }.sortedWith(compareBy({ it.destination.x }, { it.destination.y })),
                    ),
                    canFoundCity = canFound,
                    settlementPromises = detached(if (canFound) UnitSettlement.promisesBroken(unit) else emptyList()),
                    worker = observeWorkerDevelopment(player, unit, active),
                    combat = observeUnitCombat(player, unit, active),
                    economy = observeUnitEconomy(player, unit, active),
                    greatPerson = observeGreatPerson(player, unit, active),
                    cityStateGift = observeCityStateGift(player, unit, active),
                    spaceshipAssembly = observeSpaceshipAssembly(player, unit, active),
                    movementOrder = observeMovementOrder(player, unit, active),
                    waterImprovement = observeWaterImprovement(player, unit, active),
                )
            }.toList(),
        ),
    )
}

private fun HexCoord.dto() = TilePosition(x, y)
private fun <T> detached(values: Collection<T>): List<T> = Collections.unmodifiableList(ArrayList(values))
