package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerWaterImprovementOperations
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class WaterImprovement(
    val name: String,
    val title: String,
    val resource: String,
    val available: Boolean,
    val reasons: List<String>,
    val consumesUnit: Boolean,
)

@Serializable
data class WaterImprovementOutcome(
    val unit: String,
    val position: TilePosition,
    val improvementBefore: String?,
    val improvementAfter: String?,
    val unitConsumed: Boolean,
    val resource: String,
    val availableResourceBefore: Int,
    val availableResourceAfter: Int,
    val movementBefore: String? = null,
    val movementAfter: String? = null,
)

internal fun observeWaterImprovement(player: Civilization, unit: MapUnit, active: Boolean): WaterImprovement? = PlayerWaterImprovementOperations(player).option(unit)?.let {
    WaterImprovement(it.name, it.title.tr(hideIcons = true), it.resource, active && it.available, Collections.unmodifiableList(it.reasons.map { reason -> reason.tr() }), it.consumesUnit)
}

internal fun waterImprovementOutcome(request: TurnCommandRequest, before: PlayerTurnSnapshot, after: PlayerTurnSnapshot): WaterImprovementOutcome {
    val unit = before.map.units.single { it.handle == request.target }
    val resource = unit.waterImprovement!!.resource
    return WaterImprovementOutcome(
        unit.handle,
        unit.position,
        before.map.tiles.single { it.position == unit.position }.improvement,
        after.map.tiles.single { it.position == unit.position }.improvement,
        after.map.units.none { it.handle == unit.handle },
        resource,
        before.economy?.resources?.singleOrNull { it.name == resource }?.available ?: 0,
        after.economy?.resources?.singleOrNull { it.name == resource }?.available ?: 0,
        unit.movement,
        after.map.units.singleOrNull { it.handle == unit.handle }?.movement,
    )
}
