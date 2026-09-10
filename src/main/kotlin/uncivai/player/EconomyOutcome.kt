package uncivai.player

import kotlinx.serialization.Serializable
import java.util.Collections

/** Actual changes to the acting player's treasury and units, never projected costs or rewards. */
@Serializable
data class EconomyOutcome(
    val goldChange: Int,
    val units: List<EconomyUnitOutcome>,
    val construction: String? = null,
    val tile: TilePosition? = null,
)

@Serializable
data class EconomyUnitOutcome(
    val handle: String,
    val name: String,
    val position: TilePosition,
    val health: Int,
    val movement: String,
    val created: Boolean,
    val removed: Boolean,
)

internal fun economicOutcome(request: TurnCommandRequest, before: PlayerTurnSnapshot, after: PlayerTurnSnapshot): EconomyOutcome {
    val oldUnits = before.map.units.associateBy { it.handle }
    val newUnits = after.map.units.associateBy { it.handle }
    val changes = (oldUnits.keys + newUnits.keys).mapNotNull { handle ->
        val old = oldUnits[handle]
        val new = newUnits[handle]
        if (old != null && new != null && old.name == new.name && old.position == new.position && old.health == new.health && old.movement == new.movement) {
            return@mapNotNull null
        }
        val unit = new ?: checkNotNull(old)
        EconomyUnitOutcome(handle, unit.name, unit.position, unit.health, unit.movement, old == null, new == null)
    }
    return EconomyOutcome(
        goldChange = after.economy!!.treasury - before.economy!!.treasury,
        units = Collections.unmodifiableList(changes),
        construction = request.selection.takeIf { request.kind == TurnCommandKind.BUY_CONSTRUCTION },
        tile = request.destination.takeIf { request.kind == TurnCommandKind.BUY_TILE },
    )
}
