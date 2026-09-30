package uncivai.player

import com.unciv.Constants
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.tile.Tile
import kotlinx.serialization.Serializable
import java.util.Collections

internal const val BARBARIAN_SIGHTING_TURNS = 5
internal const val CIVILIAN_KNOWLEDGE_RADIUS = 5

/** A past observation, never the position of a unit currently hidden by fog. */
@Serializable
data class BarbarianSighting(val position: TilePosition, val name: String, val lastSeenTurn: Int)

/** Record intermediate movement discoveries as well as ordinary turn observations. */
internal fun captureBarbarianSightings(player: Civilization, state: TurnSessionState, tiles: Iterable<Tile>) {
    val visible = tiles.filter { it.isVisible(player) }
    val positions = visible.map { TilePosition(it.position.x, it.position.y) }.toSet()
    state.barbarianSightings.entries.removeIf {
        player.gameInfo.turns - it.value.lastSeenTurn > BARBARIAN_SIGHTING_TURNS || it.value.position in positions
    }
    for (tile in visible) {
        for (unit in tile.getUnits().filter { it.civ.isBarbarian && it.isMilitary() && it.isVisibleTo(player) }) {
            state.barbarianSightings[unit.id] = BarbarianSighting(TilePosition(tile.position.x, tile.position.y), unit.name, player.gameInfo.turns)
        }
    }
}

internal fun recentBarbarianSightings(player: Civilization, state: TurnSessionState): List<BarbarianSighting> = state.barbarianSightings.values.filter {
    player.gameInfo.turns - it.lastSeenTurn <= BARBARIAN_SIGHTING_TURNS &&
        !player.gameInfo.tileMap[it.position.x, it.position.y].isVisible(player)
}.sortedWith(compareByDescending<BarbarianSighting> { it.lastSeenTurn }.thenBy { it.position.x }.thenBy { it.position.y }.thenBy { it.name })

/** Position lists refer to tacticalReview's current threats, known camps and recent sightings. */
@Serializable
data class CivilianExposure(
    val protection: String,
    val military: String?,
    val captureAssessment: String,
    val visibleCaptureThreats: List<TilePosition>,
    val unseenTilesWithinTwoHexes: Int,
    val nearbyKnownCamps: List<TilePosition>,
    val nearbyBarbarianSightings: List<TilePosition>,
)

@Serializable
data class EscortCandidate(
    val unit: String,
    val distanceInHexes: Int,
    val atSettler: Boolean,
    val canReachThisTurn: Boolean,
    val estimatedTurns: Int?,
    val routeStatus: String,
)

@Serializable
data class SettlerNextStop(val position: TilePosition, val movementRemaining: Float, val exposure: CivilianExposure)

@Serializable
data class SettlerProtection(
    val current: CivilianExposure,
    val escortCandidates: List<EscortCandidate>,
    val nextStop: SettlerNextStop?,
)

internal fun knownBarbarianCamps(player: Civilization): List<Tile> = player.gameInfo.tileMap.values.filter {
    it.isExplored(player) && it.getShownImprovement(player) == Constants.barbarianEncampment
}

internal fun observeSettlerProtection(
    player: Civilization,
    unit: MapUnit,
    state: TurnSessionState,
    contexts: PositionContexts,
    active: Boolean,
): SettlerProtection {
    val operations = PlayerUnitOperations(player)
    val candidates = player.units.getCivUnits().filter {
        it.isMilitary() && operations.supportsMovement(it) &&
            (!it.isEscorting() || it.getOtherEscortUnit() === unit) &&
            (it.currentTile === unit.currentTile || it.movement.canMoveTo(unit.currentTile, forPlanning = true))
    }.sortedWith(compareBy<MapUnit> { it.currentTile.aerialDistanceTo(unit.currentTile) }.thenBy { it.id }).take(2).map { military ->
        val sameTile = military.currentTile === unit.currentTile
        val route = if (active && !sameTile) operations.route(military, unit.currentTile) else null
        EscortCandidate(
            state.unitHandle(military),
            military.currentTile.aerialDistanceTo(unit.currentTile),
            sameTile,
            active && (sameTile || route?.turns?.size == 1),
            if (sameTile) 0 else route?.turns?.size,
            when {
                sameTile -> "AT_SETTLER"
                !active -> "CANNOT_ACT_NOW"
                !military.hasMovement() -> "NO_MOVEMENT_REMAINING"
                route == null -> "NO_ROUTE_OFFERED"
                else -> "ROUTE_AVAILABLE"
            },
        )
    }
    val destination = operations.movementDestination(unit)
    val stop = if (active && destination != null) operations.route(unit, destination)?.turns?.firstOrNull() else null
    return SettlerProtection(
        checkNotNull(contexts.civilianExposure(unit, unit.currentTile)),
        frozen(candidates.toList()),
        stop?.let {
            SettlerNextStop(
                TilePosition(it.destination.position.x, it.destination.position.y),
                (unit.currentMovement - it.movementCost).coerceAtLeast(0f),
                checkNotNull(contexts.civilianExposure(unit, it.destination)),
            )
        },
    )
}

private fun <T> frozen(values: Collection<T>): List<T> = Collections.unmodifiableList(values.toList())
