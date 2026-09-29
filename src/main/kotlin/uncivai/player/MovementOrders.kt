package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.civilization.PlayerUnitOrders
import com.unciv.logic.map.mapunit.MapUnit
import kotlinx.serialization.Serializable
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.util.Collections
import java.util.Locale

@Serializable
data class MovementPreviewRequest(val target: String, val destination: TilePosition)

@Serializable
data class EscortFormation(val partner: String, val active: Boolean)

internal fun observeEscort(player: Civilization, unit: MapUnit, state: TurnSessionState): EscortFormation? =
    PlayerUnitOrders(player).escortPartner(unit)?.let {
        EscortFormation(state.unitHandles.getOrPut(it.id) { state.identifiers.next(PlayerIdentifier.UNIT) }, unit.isEscorting())
    }

/** Turn stops are estimates; the engine replans as terrain and other units become visible. */
@Serializable
data class MovementPreview(
    val epoch: String,
    val revision: String,
    val turn: Int,
    val target: String,
    val destination: TilePosition,
    val available: Boolean,
    val reasons: List<String>,
    val estimatedTurns: Int?,
    val turns: List<UnitMoveOption>,
    val settlement: SettlementPreview? = null,
    val escort: String? = null,
)

@Serializable
data class MovementOrder(
    val destination: TilePosition,
    val canCancel: Boolean,
    val settlement: SettlementSiteSummary? = null,
)

@Serializable
data class MovementOutcome(
    val unit: String,
    val requestedDestination: TilePosition?,
    val start: TilePosition,
    val end: TilePosition?,
    val movementBefore: String,
    val movementAfter: String?,
    val orderBefore: MovementOrder?,
    val orderAfter: MovementOrder?,
)

internal fun MovementOrder.detached(): MovementOrder = copy(
    settlement = settlement?.copy(siteBlockers = Collections.unmodifiableList(settlement.siteBlockers.toList())),
)

internal fun observeMovementOrder(player: Civilization, unit: MapUnit, active: Boolean, knowledge: Map<TilePosition, KnownSettlementTile> = emptyMap()): MovementOrder? {
    val position = PlayerUnitOperations(player).movementDestination(unit)?.position ?: return null
    val destination = TilePosition(position.x, position.y)
    val settlement = observeSettlement(player, unit, destination, active, knowledge)?.let {
        SettlementSiteSummary(it.siteStatus, it.siteBlockers, it.uncertainty)
    }
    return MovementOrder(destination, active && PlayerUnitOperations(player).canCancelMovement(unit), settlement)
}

internal fun observeMovementPreview(player: Civilization, unit: MapUnit, turn: PlayerTurnSnapshot, request: MovementPreviewRequest, knowledge: Map<TilePosition, KnownSettlementTile> = emptyMap()): MovementPreview {
    val active = turn.phase == "ACTIVE" && turn.result == null && turn.alerts.none { it.capture != null }
    val destination = player.gameInfo.tileMap.getOrNull(request.destination.x, request.destination.y)
    val route = if (active && destination != null) PlayerUnitOperations(player).route(unit, destination) else null
    val format = DecimalFormat("0.#", DecimalFormatSymbols(Locale.ROOT))
    val contexts = PositionContexts(player)
    val observed = turn.map.units.single { it.handle == request.target }
    val attacks = observed.combat?.attacks.orEmpty()
    val escort = observed.escort?.takeIf { it.active }?.partner
    val turns = route?.turns?.mapIndexed { index, segment ->
        UnitMoveOption(
            TilePosition(segment.destination.position.x, segment.destination.position.y),
            Collections.unmodifiableList(segment.path.map { TilePosition(it.position.x, it.position.y) }),
            format.format(segment.movementCost.toDouble()),
            segment.path.count { !it.isExplored(player) },
            segment.path.count { !it.isVisible(player) },
            movementConsequences(player, unit, segment.destination, segment.movementCost, contexts, attacks, knowledge,
                laterTurn = index > 0, escortHandle = escort, escortCost = segment.escortMovementCost),
        )
    } ?: emptyList()
    return MovementPreview(
        turn.epoch, turn.revision, turn.turn, request.target, request.destination,
        route != null, if (route == null) listOf("No movement route is currently offered") else emptyList(),
        turns.size.takeIf { route != null }, Collections.unmodifiableList(turns),
        observeSettlement(player, unit, request.destination, active, knowledge),
        escort,
    )
}

internal fun movementOutcome(request: TurnCommandRequest, before: PlayerTurnSnapshot, after: PlayerTurnSnapshot): MovementOutcome {
    val prior = before.map.units.single { it.handle == request.target }
    val next = after.map.units.singleOrNull { it.handle == request.target }
    return MovementOutcome(prior.handle, request.destination, prior.position, next?.position, prior.movement, next?.movement, prior.movementOrder, next?.movementOrder)
}
