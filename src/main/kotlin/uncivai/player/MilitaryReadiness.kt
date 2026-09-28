package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.models.ruleset.unit.BaseUnit
import kotlinx.serialization.Serializable

/** Descriptive roles from the selected ruleset, not combat effectiveness ratings. */
internal fun militaryRole(unit: BaseUnit): String = when {
    !unit.isMilitary -> "CIVILIAN"
    unit.unitType == "Scout" -> "RECON"
    unit.isAirUnit() -> "AIR"
    unit.isWaterUnit -> if (unit.isRanged()) "NAVAL_RANGED" else "NAVAL_MELEE"
    unit.isRanged() -> "RANGED"
    else -> "MELEE"
}

@Serializable
data class NearbyDefender(val unit: String, val distance: Int)

@Serializable
data class CityDefense(val city: String, val garrison: String?, val nearbyDefenders: List<NearbyDefender>)

@Serializable
data class ObservedBorderForce(
    val name: String,
    val civilization: String,
    val atWar: Boolean,
    val position: TilePosition,
    val health: Int,
    val strength: Int,
    val rangedStrength: Int,
    val observedTurn: Int,
    val distanceToTerritory: Int,
)

@Serializable
data class MilitaryRecruitment(val city: String, val units: List<String>)

@Serializable
data class MilitaryReadiness(
    val roleCounts: Map<String, Int>,
    val unitsExcludingRecon: Int,
    val cities: List<CityDefense>,
    val visibleBorderForces: List<ObservedBorderForce>,
    val recruitment: List<MilitaryRecruitment>,
    val limits: String = "Defenders exclude recon and are within 3 hexes of the city; distances are not response times. Border forces are currently visible military within 3 hexes of owned territory, including peaceful neighbors. Their presence does not establish hostile intent or reveal total armies. Inspect named construction or unit economy details for costs and upgrade requirements.",
)

internal fun observeMilitaryReadiness(player: Civilization, state: TurnSessionState, constructions: Map<String, List<String>>): MilitaryReadiness {
    val military = player.units.getCivUnits().filter { it.baseUnit.isMilitary }.sortedBy { it.id }.toList()
    val defenders = military.filter { militaryRole(it.baseUnit) != "RECON" }
    val territory = player.cities.flatMap { it.getTiles().filter { tile -> tile.getOwner() === player }.toList() }.distinct()
    val forces = player.gameInfo.tileMap.values.filter { it.isVisible(player) }.flatMap { tile ->
        val units = tile.getUnits().filter { it.civ !== player && it.baseUnit.isMilitary && it.isVisibleTo(player) }.toList()
        if (units.isEmpty()) return@flatMap emptyList()
        val distance = territory.minOfOrNull { tile.aerialDistanceTo(it) }
        if (distance == null || distance > 3) {
            emptyList()
        } else {
            units
                .map { unit ->
                    ObservedBorderForce(
                        unit.name, unit.civ.civName, player.isAtWarWith(unit.civ), TilePosition(tile.position.x, tile.position.y),
                        unit.health, unit.baseUnit.strength, unit.baseUnit.rangedStrength, player.gameInfo.turns, distance,
                    )
                }.toList()
        }
    }.sortedWith(compareBy({ it.civilization }, { it.position.x }, { it.position.y }, { it.name }))
    return MilitaryReadiness(
        military.groupingBy { militaryRole(it.baseUnit) }.eachCount().toSortedMap(),
        defenders.size,
        player.cities.sortedBy { it.name }.map { city ->
            val nearby = defenders.mapNotNull { unit ->
                val distance = city.getCenterTile().aerialDistanceTo(unit.currentTile)
                if (distance <= 3) NearbyDefender(state.unitHandles.getValue(unit.id), distance) else null
            }.sortedWith(compareBy({ it.distance }, { it.unit }))
            CityDefense(state.cityHandle(city.id), city.getCenterTile().militaryUnit?.takeIf { it.civ === player }?.let { state.unitHandles.getValue(it.id) }, nearby)
        },
        forces,
        player.cities.sortedBy { it.name }.map { city ->
            MilitaryRecruitment(state.cityHandle(city.id), constructions.getValue(city.id).filter { player.gameInfo.ruleset.units[it]?.isMilitary == true })
        },
    )
}
