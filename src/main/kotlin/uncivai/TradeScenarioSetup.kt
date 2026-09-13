package uncivai

import com.badlogic.gdx.files.FileHandle
import com.unciv.logic.GameInfo
import com.unciv.logic.GameStarter
import com.unciv.logic.files.MapSaver
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.MapSize
import com.unciv.logic.map.TileMap
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.models.metadata.GameSetupInfo
import com.unciv.models.ruleset.RulesetCache
import java.nio.file.Files

/** A peaceful, solvent pair with connected surplus resources for bilateral trade verification. */
internal object TradeScenarioSetup {
    const val MAP_NAME = "Asynchronous trade exercise"

    fun create(setup: GameSetupInfo): GameInfo {
        val ruleset = RulesetCache.getComplexRuleset(setup.gameParameters)
        val map = TileMap(6, ruleset).apply {
            mapParameters = setup.mapParameters.apply {
                name = MAP_NAME
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

    fun instructions(civilization: String): List<String> = listOf(
        "Complete one ordinary binding trade with the other player while continuing normal scheduled turns. Remain at peace. Keep the existing construction and research queues; resolve any mandatory choices.",
        "Negotiate in private chat, then use trade_options and trade_offer for exact quantities and engine durations. A social commitment does not transfer resources. Read trade_read before accepting or counteroffering; the service supplies the inspected version.",
        "After the engine confirms ENACTED, verify your resource access and active agreements with trade_options, and record the actual deal in economy and your relationship notes with observed sources. Do not initiate another deal. Never poll or wait for a reply; incoming offers wake communication work.",
        "Rome's gameplay job should initiate the first offer once, only if chat_read shows no existing proposal. Leave subsequent counteroffers and acceptance to communication jobs; gameplay should keep developing and ending turns. Before responding, read the latest terms and relevant relationship memory so concurrent jobs do not repeat completed steps.",
        if (civilization == "Rome") "Offer Greece 1 Silk in exchange for 4 Gold per turn, using the actual offered names and durations. If Greece counters with 2 Gold per turn and 20 immediate Gold in exchange for your 1 Silk, read those terms and accept." else "Wait for Rome's initial Silk offer through event delivery. Counteroffer once: give Rome 2 Gold per turn plus 20 immediate Gold and receive 1 Silk. Use the same proposal ID after reading it. Let Rome accept your counteroffer; do not accept the original 4-GPT price.",
    )
}
