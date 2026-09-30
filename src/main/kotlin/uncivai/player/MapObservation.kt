package uncivai.player

import com.unciv.logic.battle.BattleUnitCapture
import com.unciv.logic.battle.CityCombatant
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerOperations
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.models.ruleset.tile.ResourceType
import com.unciv.models.ruleset.unique.GameContext
import com.unciv.models.ruleset.unique.UniqueType
import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.Serializable
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.util.Collections
import java.util.Locale

@Serializable
data class TilePosition(val x: Int, val y: Int)

@Serializable
data class CivilianCapture(val isOurCapturedUnit: Boolean, val unitIfKept: String?)

@OptIn(ExperimentalSerializationApi::class)
@Serializable
data class VisibleUnit(
    val name: String,
    val civilization: String,
    val health: Int,
    val military: Boolean,
    val atWar: Boolean,
    @EncodeDefault(EncodeDefault.Mode.NEVER) val capture: CivilianCapture? = null,
)

/** Preview the kept unit type without disclosing another player's original ownership. */
private fun observeCivilianCapture(player: Civilization, unit: MapUnit): CivilianCapture? {
    if (unit.civ === player || !unit.isCivilian() || !unit.isVisibleTo(player)) return null
    if (BattleUnitCapture.destroysCivilianOnCapture(unit, player)) return null
    val ours = unit.originalOwningCiv === player
    // Native original-owner recapture preserves the current unit type, including Settlers.
    val keptType = if (ours) unit.baseUnit else BattleUnitCapture.keptCivilianUnitType(unit, player)
    return CivilianCapture(ours, keptType?.name)
}

/** Proximity is a warning, not a prediction of enemy movement or an exhaustive danger map. */
@Serializable
data class NearbyHostile(val position: TilePosition, val unit: VisibleUnit, val distance: Int)

@Serializable
data class ObservedTile(
    val position: TilePosition,
    val visible: Boolean,
    val terrain: String?,
    val features: List<String>?,
    val naturalWonder: String?,
    val resource: String?,
    val resourceAmount: Int?,
    val owner: String?,
    val cityName: String?,
    val cityHealth: Int?,
    val improvement: String?,
    val improvementPillaged: Boolean?,
    val road: String?,
    val roadPillaged: Boolean?,
    val improvementInProgress: String?,
    val improvementTurns: Int?,
    val riverBottom: Boolean,
    val riverBottomLeft: Boolean,
    val riverBottomRight: Boolean,
    val units: List<VisibleUnit>,
    val yields: List<DisplayedYield>? = null,
    val observedTurn: Int? = null,
    val cityIsOriginalMajorCapital: Boolean? = null,
    val cityPopulation: Int? = null,
    val cityMaxHealth: Int? = null,
    val cityDefensiveStrength: Int? = null,
)

@Serializable
data class UnitMoveOption(
    val destination: TilePosition,
    val path: List<TilePosition>,
    val movementCost: String,
    val unexploredTiles: Int,
    val tilesOutsideCurrentSight: Int,
    val afterMove: MovementConsequences? = null,
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
    val air: UnitAirStatus? = null,
    val settlement: SettlementPreview? = null,
    val escort: EscortFormation? = null,
    val protection: SettlerProtection? = null,
)

/** Coordinates use Unciv's axial x/y grid. No tile data is returned for unexplored coordinates. */
@Serializable
data class MapObservation(
    val tiles: List<ObservedTile> = emptyList(),
    val frontier: List<TilePosition> = emptyList(),
    val units: List<OwnedUnit> = emptyList(),
    val wrapOffset: TilePosition? = null,
    val recentBarbarianSightings: List<BarbarianSighting> = emptyList(),
)

internal fun observeMap(player: Civilization, state: TurnSessionState, canAct: Boolean = true): MapObservation {
    val tiles = player.gameInfo.tileMap.tileList.filter { it.isExplored(player) }
        .sortedWith(compareBy({ it.position.x }, { it.position.y }))
    val operations = PlayerUnitOperations(player)
    val active = canAct && PlayerOperations(player).canAct()
    val format = DecimalFormat("0.#", DecimalFormatSymbols(Locale.ROOT))
    val contexts = PositionContexts(player, state)
    val visibleHostiles = tiles.flatMap { tile ->
        tile.getUnits().filter { it.isVisibleTo(player) && it.baseUnit.isMilitary && player.isAtWarWith(it.civ) }
            .map { tile to VisibleUnit(it.name, it.civ.civName, it.health, military = true, atWar = true) }.toList()
    }
    return MapObservation(
        recentBarbarianSightings = detached(recentBarbarianSightings(player, state)),
        wrapOffset = player.gameInfo.tileMap.takeIf { it.mapParameters.worldWrap }?.getUnwrappedPosition(HexCoord.Zero)?.dto(),
        tiles = detached(
            tiles.map { tile ->
                val visible = tile.isVisible(player)
                val resource = tile.tileResource?.takeIf { player.canSeeResource(it) }
                val known = state.settlementKnowledge[tile.position.dto()]
                val surface = known?.surface
                // Native memory also covers imported saves and permitted remote
                // discoveries, such as Honor's barbarian encampment notifications.
                val improvement = if (visible) tile.improvement else tile.getShownImprovement(player)
                val city = tile.getCity()?.takeIf { tile.isCityCenter() }
                val visibleCity = city?.takeIf { visible }
                ObservedTile(
                    position = tile.position.dto(),
                    visible = visible,
                    terrain = surface?.terrain,
                    features = surface?.features?.let(::detached),
                    naturalWonder = tile.naturalWonder,
                    resource = resource?.name,
                    resourceAmount = tile.resourceAmount.takeIf { resource?.resourceType == ResourceType.Strategic },
                    owner = if (visible) tile.getOwner()?.civName else state.settlementKnowledge[tile.position.dto()]?.owner,
                    cityName = if (visible) city?.name else state.settlementKnowledge[tile.position.dto()]?.cityName,
                    cityHealth = city?.health?.takeIf { visible },
                    cityIsOriginalMajorCapital = if (visible) city?.let { if (it.isOriginalCapital) it.foundingCivObject?.isMajorCiv() else false } else known?.cityIsOriginalMajorCapital,
                    cityPopulation = visibleCity?.population?.population,
                    cityMaxHealth = visibleCity?.let { CityCombatant(it).getMaxHealth() },
                    cityDefensiveStrength = visibleCity?.let { CityCombatant(it).getDefendingStrength(null) },
                    improvement = improvement,
                    improvementPillaged = surface?.takeIf { improvement != null && it.improvement == improvement }?.improvementPillaged,
                    road = surface?.road,
                    roadPillaged = surface?.roadPillaged,
                    improvementInProgress = tile.improvementInProgress.takeIf { visible },
                    improvementTurns = tile.turnsToImprovement.takeIf { visible && tile.improvementInProgress != null },
                    riverBottom = tile.hasBottomRiver,
                    riverBottomLeft = tile.hasBottomLeftRiver,
                    riverBottomRight = tile.hasBottomRightRiver,
                    yields = observeTileYields(player, tile),
                    observedTurn = known?.observedTurn,
                    units = detached(
                        tile.getUnits().filter { it.civ === player || it.isVisibleTo(player) }
                            .map { VisibleUnit(it.name, it.civ.civName, it.health, it.baseUnit.isMilitary, player.isAtWarWith(it.civ), observeCivilianCapture(player, it)) }
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
                val combat = observeUnitCombat(player, unit, active, contexts)
                val escort = observeEscort(player, unit, state)
                OwnedUnit(
                    handle = state.unitHandle(unit),
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
                                unexploredTiles = move.path.count { !it.isExplored(player) },
                                tilesOutsideCurrentSight = move.path.count { !it.isVisible(player) },
                                afterMove = movementConsequences(
                                    player, unit, move.destination, move.movementCost, contexts, combat.attacks, state.settlementKnowledge,
                                    escortHandle = escort?.takeIf { it.active }?.partner, escortCost = move.escortMovementCost,
                                ),
                            )
                        }.sortedWith(compareBy({ it.destination.x }, { it.destination.y })),
                    ),
                    canFoundCity = canFound,
                    settlementPromises = detached(if (canFound) UnitSettlement.promisesBroken(unit) else emptyList()),
                    worker = observeWorkerDevelopment(player, unit, active),
                    combat = combat,
                    economy = observeUnitEconomy(player, unit, active),
                    greatPerson = observeGreatPerson(player, unit, active),
                    cityStateGift = observeCityStateGift(player, unit, active),
                    spaceshipAssembly = observeSpaceshipAssembly(player, unit, active),
                    movementOrder = observeMovementOrder(player, unit, active, state.settlementKnowledge),
                    waterImprovement = observeWaterImprovement(player, unit, active),
                    air = observeUnitAir(player, unit, state, active),
                    settlement = observeSettlement(player, unit, unit.currentTile.position.dto(), active, state.settlementKnowledge),
                    escort = escort,
                    protection = if (unit.isCivilian() && (unit.hasUnique(UniqueType.FoundCity, GameContext.IgnoreConditionals) || unit.hasUnique(UniqueType.FoundPuppetCity, GameContext.IgnoreConditionals))) {
                        observeSettlerProtection(player, unit, state, contexts, active)
                    } else {
                        null
                    },
                )
            }.toList(),
        ),
    )
}

private fun HexCoord.dto() = TilePosition(x, y)
private fun <T> detached(values: Collection<T>): List<T> = Collections.unmodifiableList(ArrayList(values))
