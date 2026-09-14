package uncivai

import com.badlogic.gdx.files.FileHandle
import com.unciv.logic.GameInfo
import com.unciv.logic.GameStarter
import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.files.MapSaver
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.MapParameters
import com.unciv.logic.map.MapSize
import com.unciv.logic.map.MapType
import com.unciv.logic.map.TileMap
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.models.metadata.BaseRuleset
import com.unciv.models.metadata.GameParameters
import com.unciv.models.metadata.GameSetupInfo
import com.unciv.models.metadata.Player
import com.unciv.models.ruleset.RulesetCache
import java.nio.file.Files

/** Map, cities and technologies for the trade-cycle exercise. Human slots preserve player control. */
internal object TradeCycleMap {
    fun create(seed: Long): GameInfo {
        val mapSeed = seed
        val parameters = GameParameters().apply {
            baseRuleset = BaseRuleset.Civ_V_GnK.fullName
            speed = "Quick"
            startingEra = "Ancient era"
            numberOfCityStates = 0
            noBarbarians = true
            shufflePlayerOrder = false
            victoryTypes = arrayListOf("Domination", "Time")
            maxTurns = 330
            players = arrayListOf(Player("Rome", PlayerType.Human), Player("Greece", PlayerType.Human))
        }
        val map = MapParameters().apply {
            baseRuleset = parameters.baseRuleset
            type = MapType.pangaea
            mapSize = MapSize.Tiny
            noRuins = true
            this.seed = mapSeed
        }
        val setup = GameSetupInfo(parameters, map)
        return base(setup).also { game ->
            for (player in game.civilizations.filter { it.isMajorCiv() }) {
                fun research(name: String) {
                    if (player.tech.isResearched(name)) return
                    game.ruleset.technologies.getValue(name).prerequisites.forEach(::research)
                    player.tech.addTechnology(name, false)
                }
                research("Education")
                research("Chivalry")
                player.tech.techsToResearch = arrayListOf("Machinery", "Printing Press")
                val position = if (player.civID == "Rome") HexCoord(-4, -4) else HexCoord(4, 4)
                val settler = checkNotNull(game.tileMap.placeUnitNearTile(position, "Settler", player))
                check(settler.currentTile.position == position)
                val city = checkNotNull(UnitSettlement.tryFoundCity(settler))
                city.name = if (player.civID == "Rome") "Antium" else "Sparta"
                city.population.setPopulation(3)
                for (owned in player.cities) {
                    owned.avoidGrowth = true
                    owned.cityConstructions.addBuilding("Granary")
                    owned.cityConstructions.constructionQueue.clear()
                    owned.cityConstructions.addToQueue("Gold")
                    owned.getTiles().filterNot { it.isCityCenter() }.filter { it.resource == null }.forEach { it.setImprovementBasic("Trading post") }
                    owned.reassignPopulation()
                    owned.cityStats.update()
                }
                player.popupAlerts.clear()
                player.notifications.clear()
                player.cache.updateCivResources()
                player.cache.updateViewableTiles()
                player.updateStatsForNextTurn()
                check(player.cities.size == 2 && !city.isCapital())
            }
        }.also { game ->
            check(game.currentPlayer == "Rome")
            check(game.civilizations.filter { it.isMajorCiv() }.all { it.isHuman() })
            check(game.gameParameters.mods.isEmpty())
        }
    }

    private fun base(setup: GameSetupInfo): GameInfo {
        val ruleset = RulesetCache.getComplexRuleset(setup.gameParameters)
        val map = TileMap(6, ruleset).apply {
            mapParameters = setup.mapParameters.apply {
                name = TradeCycleScenarioSetup.MAP_NAME
                mapSize = MapSize(6)
            }
            for (tile in values) {
                tile.baseTerrain = "Grassland"
                tile.setTerrainTransients()
            }
            addStartingLocation("Rome", this[HexCoord(-4, 0)])
            addStartingLocation("Greece", this[HexCoord(4, 0)])
        }
        val temporary = Files.createTempFile("trade-exercise-", ".json")
        val game = try {
            Files.writeString(temporary, MapSaver.mapToSavedString(map))
            setup.mapFile = FileHandle(temporary.toFile())
            GameStarter.startNewGame(setup)
        } finally {
            Files.deleteIfExists(temporary)
        }
        val players = listOf("Rome", "Greece").map(game::getCivilization)
        players.forEach { player -> player.units.getCivUnits().toList().forEach { it.destroy() } }
        for (tile in game.tileMap.values) players.forEach { tile.setExplored(it, false) }
        for ((index, player) in players.withIndex()) {
            val center = HexCoord(if (index == 0) -4 else 4, 0)
            val settler = checkNotNull(game.tileMap.placeUnitNearTile(center, "Settler", player))
            check(settler.currentTile.position == center)
            val city = checkNotNull(UnitSettlement.tryFoundCity(settler))
            city.name = if (index == 0) "Rome" else "Athens"
            player.addGold(1000 - player.gold)
            for (technology in listOf("Pottery", "Mining", "Calendar", "Bronze Working", "Iron Working")) player.tech.addTechnology(technology, false)
            player.tech.techsToResearch = arrayListOf("Writing", "The Wheel", "Mathematics")
            val luxury = if (player.civID == "Rome") "Silk" else "Gold Ore"
            val tiles = city.getTiles().filterNot { it.isCityCenter() }.sortedWith(compareBy({ it.position.x }, { it.position.y })).toList()
            tiles.forEach { it.setImprovementBasic("Trading post") }
            city.getCenterTile().setTileResource(luxury)
            tiles[0].setTileResource(luxury)
            tiles[0].setImprovementBasic(if (luxury == "Silk") "Plantation" else "Mine")
            tiles[1].setTileResource("Iron")
            tiles[1].resourceAmount = 4
            tiles[1].setImprovementBasic("Mine")
            city.population.setPopulation(5)
            city.avoidGrowth = true
            city.cityConstructions.addBuilding("Granary")
            city.cityConstructions.constructionQueue.clear()
            for (construction in listOf("Barracks", "Monument")) {
                check(ruleset.buildings.getValue(construction).isBuildable(city.cityConstructions))
                city.cityConstructions.addToQueue(construction)
            }
            city.clearWorkedTiles()
            city.reassignPopulation()
            city.cityStats.update()
            player.cache.updateCivResources()
            player.cache.updateViewableTiles()
            player.updateStatsForNextTurn()
            check((player.getCivResourcesByName()[luxury] ?: 0) >= 2) { "Missing $luxury: ${player.getCivResourcesByName()}" }
            check((player.getCivResourcesByName()["Iron"] ?: 0) >= 4)
            check(player.stats.statsForNextTurn.gold >= 4) { "Insufficient trade income for ${player.civID}: ${player.stats.statsForNextTurn}" }
            check(city.cityStats.currentCityStats.food >= 0) { "Trade exercise city must not starve: ${city.cityStats.currentCityStats}" }
        }
        players.first().diplomacyFunctions.makeCivilizationsMeet(players.last())
        players.forEach { player ->
            player.popupAlerts.clear()
            player.notifications.clear()
        }
        return game
    }
}
