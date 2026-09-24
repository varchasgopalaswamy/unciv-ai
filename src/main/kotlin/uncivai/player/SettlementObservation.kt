package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.map.mapunit.FoundingUnitRejection
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.mapunit.UnitSettlement
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

internal fun observeSettlement(player: Civilization, unit: MapUnit, destination: TilePosition, active: Boolean, knowledge: Map<TilePosition, KnownSettlementTile> = emptyMap()): SettlementPreview? {
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
        if (tile.isWater) blockers.add(SettlementBlocker("WATER", "A city cannot be founded on water."))
        if (tile.isImpassible()) blockers.add(SettlementBlocker("IMPASSABLE", "A city cannot be founded on impassable terrain."))
        val rememberedTile = knowledge[destination]
        val owner = if (tile.isVisible(player)) tile.getOwner()?.civName else rememberedTile?.owner
        if (owner != null && owner != player.civName) {
            blockers.add(SettlementBlocker("FOREIGN_TERRITORY", "The tile belongs to another civilization according to the latest observation.", observedTurn = rememberedTile?.observedTurn, remembered = !tile.isVisible(player)))
        }
        val centers = knowledge.values.filter { it.cityName != null }.associateBy { it.position }.toMutableMap()
        for (city in player.gameInfo.getCities().filter { it.civ === player || it.getCenterTile().isVisible(player) }) {
            val center = city.getCenterTile()
            val position = TilePosition(city.location.x, city.location.y)
            centers[position] = KnownSettlementTile(position, player.gameInfo.turns, true, false, center.getContinent(), city.civ.civName, city.name, false, false, false, null, null, 0f)
        }
        for (known in centers.values) {
            val center = player.gameInfo.tileMap.getOrNull(known.position.x, known.position.y) ?: continue
            val minimum = (if (tile.getContinent() == known.continent) constants.minimalCityDistance else constants.minimalCityDistanceOnDifferentContinents) + 1
            val distance = tile.aerialDistanceTo(center)
            if (distance < minimum) {
                blockers.add(SettlementBlocker("CITY_TOO_CLOSE", "City centers must be at least $minimum hexes apart here.", known.cityName, known.position, distance, minimum, known.observedTurn, !center.isVisible(player) && known.owner != player.civName))
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
