package uncivai

import com.unciv.logic.GameInfo
import com.unciv.logic.city.CityFocus
import com.unciv.logic.city.managers.CityFounder
import com.unciv.logic.civilization.PlayerCityOperations
import com.unciv.logic.map.HexCoord

/** Symmetric, peaceful fixture for the city-management regression tests. */
internal object CityManagementScenarioSetup {
    const val MAP_NAME = "City management exercise"

    fun arrange(game: GameInfo) {
        game.tileMap.mapParameters.name = MAP_NAME
        val players = listOf("Rome", "Greece").map(game::getCivilization)
        players.forEach { player ->
            player.units.getCivUnits().toList().forEach { it.destroy() }
            player.cities.filterNot { it.isOriginalCapital }.toList().forEach { it.destroyCity() }
        }
        players.first().getDiplomacyManager(players.last())!!.makePeace()
        for (tile in game.tileMap.values) {
            tile.baseTerrain = "Plains"
            tile.removeTerrainFeatures()
            tile.improvement = "Farm"
            tile.setTileResource(null)
            tile.setTerrainTransients()
        }
        for ((index, player) in players.withIndex()) {
            val sign = if (index == 0) -1 else 1
            val neighbor = CityFounder().foundCity(player, HexCoord(sign * 4, sign * 3))
            neighbor.name = if (index == 0) "Antium" else "Sparta"
            player.addGold(1000 - player.gold)
            player.popupAlerts.clear()
            player.notifications.clear()
            player.tech.techsToResearch = arrayListOf("Writing", "The Wheel", "Bronze Working")
            for (city in player.cities) {
                city.health = 200
                city.population.setPopulation(if (city.isOriginalCapital) 4 else 3)
                city.setCityFocus(CityFocus.FoodFocus)
                city.avoidGrowth = true
                city.manualSpecialists = true
                for (building in listOf("Granary", "Library", "University", "Market", "Colosseum")) city.cityConstructions.addBuilding(building)
                city.cityConstructions.constructionQueue = arrayListOf("The Pyramids")
                city.population.specialistAllocations.clear()
                city.clearWorkedTiles()
                city.population.foodStored = city.population.getFoodToNextPopulation() - 1
            }
            val capital = player.cities.single { it.isOriginalCapital }
            val shared = capital.getWorkableTiles().filter { it in neighbor.tilesInRange && !it.isCityCenter() }
                .sortedWith(compareBy({ it.position.x }, { it.position.y })).first()
            check(neighbor.workTile(shared))
            neighbor.lockedTiles.add(shared.position)
            capital.cityStats.update()
            neighbor.cityStats.update()
            player.cache.updateCivResources()
            player.cache.updateViewableTiles()
            player.updateStatsForNextTurn()
            check(!PlayerCityOperations(player).isAssignableTile(capital, shared))
        }
        check(game.victoryData == null)
        check(players.none { it.isAtWar() })
    }
}
