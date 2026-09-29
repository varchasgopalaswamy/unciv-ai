package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.map.mapunit.MapUnit
import kotlinx.serialization.Serializable

@Serializable
data class SettlementDestinationChange(val turn: Int, val from: TilePosition?, val to: TilePosition?)

/** Observation history, not an inferred production date or a claim that staying put was a mistake. */
@Serializable
data class SettlerHistory(
    val firstObservedTurn: Int,
    val lastMovedTurn: Int,
    val position: TilePosition,
    val destination: TilePosition?,
    val destinationChanges: List<SettlementDestinationChange> = emptyList(),
)

@Serializable
data class SettlementRoute(val status: String, val estimatedTurns: Int? = null, val unexploredTiles: Int? = null, val tilesOutsideCurrentSight: Int? = null)

@Serializable
data class SettlementTarget(
    val position: TilePosition,
    val score: Int?,
    val site: SettlementSiteSummary,
    val route: SettlementRoute,
)

@Serializable
data class SettlerReview(
    val unit: String,
    val firstObservedTurn: Int,
    val turnsObserved: Int,
    val turnsWithoutMovement: Int,
    val recentDestinationChanges: List<SettlementDestinationChange>,
    val currentSite: SettlementPreview,
    val currentSiteScore: Int?,
    val destination: SettlementTarget?,
)

internal fun observeSettlerHistory(player: Civilization, unit: MapUnit, state: TurnSessionState): SettlerHistory {
    val turn = player.gameInfo.turns
    val position = TilePosition(unit.currentTile.position.x, unit.currentTile.position.y)
    val destination = if (unit.isMoving()) unit.getMovementDestination().position.let { TilePosition(it.x, it.y) } else null
    val old = state.settlerHistory[unit.id] ?: SettlerHistory(turn, turn, position, destination)
    val changed = old.destination != destination && !(destination == null && position == old.destination)
    val next = old.copy(
        lastMovedTurn = if (old.position != position) turn else old.lastMovedTurn,
        position = position,
        destination = destination,
        destinationChanges = if (changed) (old.destinationChanges + SettlementDestinationChange(turn, old.destination, destination)).takeLast(5) else old.destinationChanges,
    )
    state.settlerHistory[unit.id] = next
    return next
}

internal fun settlementRoute(player: Civilization, unit: MapUnit, position: TilePosition): SettlementRoute {
    val destination = player.gameInfo.tileMap.getOrNull(position.x, position.y) ?: return SettlementRoute("NO_ROUTE_OFFERED")
    if (destination === unit.currentTile) return SettlementRoute("AT_DESTINATION", 0, 0, 0)
    if (!unit.hasMovement()) return SettlementRoute("NO_MOVEMENT_REMAINING")
    val route = PlayerUnitOperations(player).route(unit, destination) ?: return SettlementRoute("NO_ROUTE_OFFERED")
    val path = route.turns.flatMap { it.path }.distinct()
    return SettlementRoute("ROUTE_OFFERED", route.turns.size, path.count { !it.isExplored(player) }, path.count { !it.isVisible(player) })
}
