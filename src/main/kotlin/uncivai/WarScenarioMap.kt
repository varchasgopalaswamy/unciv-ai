package uncivai

import com.badlogic.gdx.files.FileHandle
import com.unciv.logic.GameInfo
import com.unciv.logic.GameStarter
import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.files.MapSaver
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.MapParameters
import com.unciv.logic.map.MapSize
import com.unciv.logic.map.TileMap
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.models.metadata.BaseRuleset
import com.unciv.models.metadata.GameParameters
import com.unciv.models.metadata.GameSetupInfo
import com.unciv.models.metadata.Player
import com.unciv.models.ruleset.RulesetCache
import java.nio.file.Files

/** Peaceful triangular opening with a capital, frontier city and army per human player. */
internal object WarScenarioMap {
    val civilizations = listOf("Rome", "Greece", "France")

    fun create(seed: Long, mapName: String): GameInfo {
        val parameters = GameParameters().apply {
            this.baseRuleset = BaseRuleset.Civ_V_Vanilla.fullName
            speed = "Quick"
            noBarbarians = true
            numberOfCityStates = 0
            shufflePlayerOrder = false
            victoryTypes = arrayListOf("Domination", "Time")
            maxTurns = 330
            players = ArrayList(civilizations.map { Player(it, PlayerType.Human) })
        }
        val setup = GameSetupInfo(
            parameters,
            MapParameters().apply {
                noRuins = true
                this.seed = seed
            },
        )
        val ruleset = RulesetCache.getComplexRuleset(parameters)
        val positions = listOf(HexCoord(-7, -7), HexCoord(7, 0), HexCoord(0, 7))
        val map = TileMap(10, ruleset).apply {
            mapParameters = setup.mapParameters.apply {
                name = mapName
                mapSize = MapSize(10)
            }
            for (tile in values) {
                tile.baseTerrain = "Grassland"
                tile.setTerrainTransients()
            }
            civilizations.zip(positions).forEach { (civ, position) -> addStartingLocation(civ, this[position]) }
        }
        val temporary = Files.createTempFile("war-scenario-", ".json")
        val game = try {
            Files.writeString(temporary, MapSaver.mapToSavedString(map))
            setup.mapFile = FileHandle(temporary.toFile())
            GameStarter.startNewGame(setup)
        } finally {
            Files.deleteIfExists(temporary)
        }
        val players = civilizations.map(game::getCivilization)
        players.forEach { player ->
            player.units.getCivUnits().toList().forEach { it.destroy() }
            player.diplomacy.clear()
        }
        game.tileMap.values.forEach { tile -> players.forEach { tile.setExplored(it, false) } }
        val frontiers = listOf(HexCoord(-3, -3), HexCoord(3, 0), HexCoord(0, 3))
        for ((index, player) in players.withIndex()) {
            fun research(name: String) {
                if (player.tech.isResearched(name)) return
                ruleset.technologies.getValue(name).prerequisites.forEach(::research)
                player.tech.addTechnology(name, false)
            }
            for (technology in listOf("Currency", "Bronze Working", "Archery", "Construction")) research(technology)
            player.tech.techsToResearch = arrayListOf("Pottery", "Writing", "Philosophy")
            check(player.tech.canBeResearched(player.tech.techsToResearch.first()))
            for (position in listOf(positions[index], frontiers[index])) {
                val settler = checkNotNull(game.tileMap.placeUnitNearTile(position, "Settler", player))
                check(settler.currentTile.position == position)
                val city = checkNotNull(UnitSettlement.tryFoundCity(settler))
                city.population.setPopulation(4)
                city.avoidGrowth = true
                city.cityConstructions.addBuilding("Granary")
                city.cityConstructions.constructionQueue.clear()
                city.cityConstructions.addToQueue("Gold")
                city.getTiles().filterNot { it.isCityCenter() }.forEach { it.setImprovementBasic("Trading post") }
                city.reassignPopulation()
                city.cityStats.update()
                check(city.cityStats.currentCityStats.food >= 0)
            }
            for (unit in listOf("Spearman", "Spearman", "Archer", "Archer")) {
                checkNotNull(game.tileMap.placeUnitNearTile(frontiers[index], unit, player))
            }
            player.cache.updateCivResources()
            player.cache.updateViewableTiles()
            player.updateStatsForNextTurn()
            player.addGold(500 - player.gold)
            check(player.stats.statsForNextTurn.gold >= 0) { "Unsustainable opening for ${player.civID}" }
            check(player.cities.size == 2)
        }
        for (a in players) for (b in players) if (a !== b && !a.knows(b)) a.diplomacyFunctions.makeCivilizationsMeet(b)
        players.forEach {
            it.popupAlerts.clear()
            it.notifications.clear()
        }
        check(game.currentPlayer == "Rome" && players.all { it.isHuman() })
        return game
    }
}
