package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.map.mapunit.FoundingUnitRejection
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.logic.map.tile.SettlementRejection
import com.unciv.logic.map.tile.SettlementBlocker as NativeSettlementBlocker
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class SettlementBlocker(
    val code: String,
    val message: String,
    val city: String? = null,
    val position: TilePosition? = null,
    val distance: Int? = null,
    val minimumDistance: Int? = null,
    val observedTurn: Int? = null,
    val remembered: Boolean = false,
)

/** Site eligibility is independent of remaining movement and is not a route or arrival guarantee. */
@Serializable
data class SettlementPreview(
    val destination: TilePosition,
    val atDestination: Boolean,
    val canFoundNow: Boolean?,
    val siteStatus: String,
    val siteBlockers: List<SettlementBlocker>,
    val unitBlockers: List<SettlementBlocker>,
    val uncertainty: String?,
    val minimumCityDistance: Int,
    val minimumCityDistanceOnDifferentContinents: Int,
)

/** Destination advice excludes the unit's current movement and action availability. */
@Serializable
data class SettlementSiteSummary(
    val siteStatus: String,
    val siteBlockers: List<SettlementBlocker>,
    val uncertainty: String?,
)

internal fun observeSettlement(player: Civilization, unit: MapUnit?, destination: TilePosition, active: Boolean, knowledge: Map<TilePosition, KnownSettlementTile> = emptyMap()): SettlementPreview? {
    if (unit != null && !UnitSettlement.hasFoundingAbility(unit)) return null
    val tile = player.gameInfo.tileMap.getOrNull(destination.x, destination.y)
    val atDestination = unit != null && tile === unit.currentTile
    val constants = player.gameInfo.ruleset.modOptions.constants
    val unitBlockers = (unit?.let { UnitSettlement.unitBlockers(it) } ?: emptyList()).map { reason ->
        SettlementBlocker(
            reason.name,
            when (reason) {
                FoundingUnitRejection.NO_MOVEMENT -> "No movement remains. Recover movement next turn; this does not remove site restrictions."
                FoundingUnitRejection.ONE_CITY_CHALLENGE -> "One City Challenge prevents founding another city."
                FoundingUnitRejection.DESTROYED -> "This unit no longer exists."
                FoundingUnitRejection.FOUNDING_ABILITY_UNAVAILABLE -> "The unit's founding ability is unavailable."
                FoundingUnitRejection.ACTION_REQUIREMENTS_NOT_MET -> "The native founding ability's action requirements are not met."
            },
        )
    }.toMutableList()
    if (unit != null && !active) unitBlockers.add(SettlementBlocker("PLAYER_ACTION_UNAVAILABLE", "Founding is not currently an available player action."))

    // The settlement lens deliberately exposes native eligibility on explored tiles,
    // including restrictions caused by undiscovered cities. City identities remain private.
    val observed = tile != null && tile.isExplored(player)
    val blockers = if (observed) tile!!.settlementBlockers(player)
        .map { observeSiteBlocker(player, it, knowledge) }
        .distinct().sortedWith(compareBy({ it.code }, { it.position?.x }, { it.position?.y })).toList()
    else emptyList()
    return SettlementPreview(
        destination, atDestination,
        if (atDestination) active && UnitSettlement.canFoundCity(unit!!) else null,
        when {
            !observed -> "UNKNOWN"
            blockers.isNotEmpty() -> "BLOCKED"
            else -> "ELIGIBLE"
        },
        Collections.unmodifiableList(blockers),
        Collections.unmodifiableList(unitBlockers), if (observed) null else "The destination has not been explored.",
        constants.minimalCityDistance + 1, constants.minimalCityDistanceOnDifferentContinents + 1,
    )
}

private fun observeSiteBlocker(player: Civilization, blocker: NativeSettlementBlocker, knowledge: Map<TilePosition, KnownSettlementTile>): SettlementBlocker {
    val code = blocker.reason.name
    when (blocker.reason) {
        SettlementRejection.WATER -> return SettlementBlocker(code, "A city cannot be founded on water.")
        SettlementRejection.IMPASSABLE -> return SettlementBlocker(code, "A city cannot be founded on impassable terrain.")
        SettlementRejection.FOREIGN_TERRITORY -> return SettlementBlocker(code, "The tile belongs to another civilization.")
        SettlementRejection.CITY_TOO_CLOSE -> Unit
    }
    val city = checkNotNull(blocker.city)
    val center = city.getCenterTile()
    val position = TilePosition(center.position.x, center.position.y)
    val visible = city.civ === player || center.isVisible(player)
    val known = knowledge[position]?.takeIf { it.cityName != null }
    if (!visible && known == null) return SettlementBlocker(code, "City-spacing rules prevent founding here.")
    return SettlementBlocker(
        code, "City centers must be at least ${blocker.minimumDistance} hexes apart here.",
        if (visible) city.name else known!!.cityName, position, blocker.distance, blocker.minimumDistance,
        if (visible) player.gameInfo.turns else known!!.observedTurn, remembered = !visible,
    )
}
