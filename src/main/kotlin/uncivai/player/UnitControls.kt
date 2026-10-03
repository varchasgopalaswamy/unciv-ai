package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerUnitManagementOperations
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.copyForPreview
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.tile.Tile
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class UnitControls(
    val rename: CityControl,
    val savePromotions: CityControl,
    val promotionCity: String?,
    val canConnectRoad: Boolean,
    val roadDestination: TilePosition?,
    val swaps: List<UnitSwap>,
)

@Serializable
data class UnitSwap(val destination: TilePosition, val partners: List<String>, val captureRisks: List<UnitSwapRisk>)

@Serializable
data class UnitSwapRisk(val unit: String, val risk: CivilianMovementRisk)

@Serializable
data class DefaultUnitPromotions(val unitType: String, val enabled: Boolean, val promotions: List<String>, val action: CityControl)

internal fun observeUnitControls(player: Civilization, unit: MapUnit, active: Boolean, state: TurnSessionState): UnitControls {
    val management = PlayerUnitManagementOperations(player)
    val city = management.promotionCity(unit)
    fun control(permitted: Boolean, reason: String) = CityControl(
        active && permitted,
        frozen(
            if (!active) {
                listOf("This player cannot act now")
            } else if (!permitted) {
                listOf(reason)
            } else {
                emptyList()
            },
        ),
    )
    val operations = PlayerUnitOperations(player)
    val contexts = PositionContexts(player)
    return UnitControls(
        control(true, ""),
        control(city != null, "Move into an owned non-puppet city to save this unit's promotions"),
        city?.let { state.cityHandle(it.id) },
        active && operations.canConnectRoad(unit),
        unit.automatedRoadConnectionDestination?.let { TilePosition(it.x, it.y) },
        frozen(
            if (!active) {
                emptyList()
            } else {
                operations.swapDestinations(unit).map { tile ->
                    val partner = if (unit.isCivilian()) tile.civilianUnit else tile.militaryUnit
                    val risks = if (unit.isEscorting()) {
                        listOfNotNull(
                            escortSwapRisk(player, unit, tile)?.let {
                                UnitSwapRisk(state.unitHandle(checkNotNull(tile.civilianUnit)), it)
                            },
                        )
                    } else {
                        listOfNotNull(
                            contexts.captureRisk(unit, tile, preserveEscort = false)?.let { UnitSwapRisk(state.unitHandle(unit), it) },
                            partner?.let { other -> contexts.captureRisk(other, unit.currentTile, preserveEscort = false)?.let { UnitSwapRisk(state.unitHandle(other), it) } },
                        )
                    }
                    val partners = if (unit.isEscorting()) listOfNotNull(tile.civilianUnit, tile.militaryUnit) else listOfNotNull(partner)
                    UnitSwap(TilePosition(tile.position.x, tile.position.y), frozen(partners.map { state.unitHandle(it) }), frozen(risks))
                }
            },
        ),
    )
}

private fun escortSwapRisk(player: Civilization, unit: MapUnit, destination: Tile): CivilianMovementRisk? {
    // A formation always arrives guarded. A lone civilian swapped back to its
    // old tile can lose that guard, though. Other formation swaps leave both
    // endpoints guarded or have no returning civilian.
    if (destination.civilianUnit == null || destination.militaryUnit != null ||
        (unit.currentTile.isCityCenter() && unit.currentTile.getOwner() === player) ||
        player.viewableTiles.none { tile -> tile.getUnits().any { it.isMilitary() && it.isVisibleTo(player) && player.isAtWarWith(it.civ) } }
    ) {
        return null
    }
    val copy = player.gameInfo.copyForPreview()
    val owner = copy.getCivilization(player.civID)
    val from = copy.tileMap[unit.currentTile.position]
    val to = copy.tileMap[destination.position]
    val guard = checkNotNull(from.militaryUnit)
    val escorted = checkNotNull(from.civilianUnit)
    val returning = checkNotNull(to.civilianUnit)
    // Forecast occupancy only. Do not execute movement or reveal the future
    // formation's sight; threat inspection keeps the current player's knowledge.
    from.militaryUnit = null
    from.civilianUnit = returning
    returning.currentTile = from
    to.militaryUnit = guard
    to.civilianUnit = escorted
    guard.currentTile = to
    escorted.currentTile = to
    owner.viewableTiles = player.viewableTiles.map { copy.tileMap[it.position] }.toSet()
    return PositionContexts(owner).captureRisk(returning, from, preserveEscort = false)
}

internal fun defaultUnitPromotions(city: com.unciv.logic.city.City, active: Boolean): List<DefaultUnitPromotions> = frozen(
    city.unitToPromotions.toSortedMap().map { (name, promotions) ->
        DefaultUnitPromotions(
            name,
            city.unitShouldUseSavedPromotion[name] == true,
            frozen(promotions.promotions.sorted()),
            CityControl(active && !city.isPuppet, frozen(if (active && !city.isPuppet) emptyList() else listOf("City management is unavailable"))),
        )
    },
)

private fun <T> frozen(values: List<T>): List<T> = Collections.unmodifiableList(values.toList())
