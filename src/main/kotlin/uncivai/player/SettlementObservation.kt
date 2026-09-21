package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.map.mapunit.FoundingUnitRejection
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.logic.map.tile.SettlementRejection
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

internal fun observeSettlement(player: Civilization, unit: MapUnit, destination: TilePosition, active: Boolean): SettlementPreview? {
    if (!UnitSettlement.hasFoundingAbility(unit)) return null
    val tile = player.gameInfo.tileMap.getOrNull(destination.x, destination.y)
    val atDestination = tile === unit.currentTile
    val constants = player.gameInfo.ruleset.modOptions.constants
    val unitBlockers = UnitSettlement.unitBlockers(unit).map { reason ->
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
    if (!active) unitBlockers.add(SettlementBlocker("PLAYER_ACTION_UNAVAILABLE", "Founding is not currently an available player action."))

    val blockers = ArrayList<SettlementBlocker>()
    var uncertainty: String? = "The destination has not been observed."
    if (tile != null && tile.isExplored(player)) {
        val radius = maxOf(constants.minimalCityDistance, constants.minimalCityDistanceOnDifferentContinents)
        // Use visibility, never the presence of hidden cities, to decide whether a preview is uncertain.
        val fullyVisible = tile.getTilesInDistance(radius).all { it.isVisible(player) || it.getOwner() === player }
        uncertainty = if (fullyVisible) null else "Nearby tiles are not all visible. Scout or recheck on arrival."
        for (blocker in tile.settlementBlockers(player)) {
            when (blocker.reason) {
                SettlementRejection.WATER -> blockers.add(SettlementBlocker("WATER", "A city cannot be founded on water."))

                SettlementRejection.IMPASSABLE -> blockers.add(SettlementBlocker("IMPASSABLE", "A city cannot be founded on impassable terrain."))

                SettlementRejection.FOREIGN_TERRITORY -> if (tile.isVisible(player)) {
                    blockers.add(SettlementBlocker("FOREIGN_TERRITORY", "The tile belongs to another civilization."))
                }

                SettlementRejection.CITY_TOO_CLOSE -> {
                    val city = checkNotNull(blocker.city)
                    if (city.civ !== player && !city.getCenterTile().isVisible(player)) continue
                    blockers.add(
                        SettlementBlocker(
                            "CITY_TOO_CLOSE",
                            "City centers must be at least ${blocker.minimumDistance} hexes apart here.",
                            city.name,
                            TilePosition(city.location.x, city.location.y),
                            blocker.distance,
                            blocker.minimumDistance,
                        ),
                    )
                }
            }
        }
    }
    return SettlementPreview(
        destination, atDestination,
        if (atDestination) active && UnitSettlement.canFoundCity(unit) else null,
        when {
            blockers.isNotEmpty() -> "BLOCKED"
            uncertainty != null -> "UNKNOWN"
            else -> "ELIGIBLE"
        },
        Collections.unmodifiableList(blockers.sortedWith(compareBy({ it.code }, { it.position?.x }, { it.position?.y }))),
        Collections.unmodifiableList(unitBlockers), uncertainty,
        constants.minimalCityDistance + 1, constants.minimalCityDistanceOnDifferentContinents + 1,
    )
}
