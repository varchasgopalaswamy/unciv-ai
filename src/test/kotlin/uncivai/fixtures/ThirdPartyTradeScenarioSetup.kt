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

/** Three mutually known majors and one unknown observer, with separated private territory. */
internal object ThirdPartyTradeScenarioSetup {
    val civilizations = listOf("Rome", "Greece", "France", "Egypt")

    fun create(baseRuleset: BaseRuleset = BaseRuleset.Civ_V_Vanilla): GameInfo {
        val parameters = GameParameters().apply {
            this.baseRuleset = baseRuleset.fullName
            speed = "Quick"
            noBarbarians = true
            numberOfCityStates = 0
            shufflePlayerOrder = false
            victoryTypes = arrayListOf("Domination", "Time")
            maxTurns = 330
            players = ArrayList(civilizations.map { Player(it, PlayerType.Human) })
        }
        val setup = GameSetupInfo(parameters, MapParameters().apply { noRuins = true })
        val ruleset = RulesetCache.getComplexRuleset(parameters)
        val positions = listOf(HexCoord(-7, -7), HexCoord(7, 7), HexCoord(7, 0), HexCoord(-7, 0))
        val map = TileMap(10, ruleset).apply {
            mapParameters = setup.mapParameters.apply {
                name = "Third-party trade regression"
                mapSize = MapSize(10)
            }
            for (tile in values) {
                tile.baseTerrain = "Grassland"
                tile.setTerrainTransients()
            }
            civilizations.zip(positions).forEach { (civ, position) -> addStartingLocation(civ, this[position]) }
        }
        val temporary = Files.createTempFile("third-party-trades-", ".json")
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
        for ((player, position) in players.zip(positions)) {
            val settler = checkNotNull(game.tileMap.placeUnitNearTile(position, "Settler", player))
            check(settler.currentTile.position == position)
            val city = checkNotNull(UnitSettlement.tryFoundCity(settler))
            city.population.setPopulation(3)
            city.avoidGrowth = true
            city.cityConstructions.addBuilding("Granary")
            city.cityConstructions.constructionQueue.clear()
            city.cityConstructions.addToQueue("Barracks")
            player.tech.techsToResearch = arrayListOf("Pottery", "Mining")
            city.reassignPopulation()
            city.cityStats.update()
            player.cache.updateViewableTiles()
            player.updateStatsForNextTurn()
            player.addGold(1000 - player.gold)
        }
        for (a in players.take(3)) for (b in players.take(3)) if (a !== b && !a.knows(b)) a.diplomacyFunctions.makeCivilizationsMeet(b)
        players.forEach {
            it.popupAlerts.clear()
            it.notifications.clear()
        }
        check(players.take(3).none { it.knows(players.last()) })
        return game
    }
}
