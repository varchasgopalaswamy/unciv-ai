package uncivai

import com.badlogic.gdx.files.FileHandle
import com.unciv.logic.GameInfo
import com.unciv.logic.GameStarter
import com.unciv.logic.civilization.Civilization
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
import uncivai.player.GameplayScenario
import java.nio.file.Files

/** Five major civilizations: two agent slots, two human slots, one native AI, plus a city-state. */
internal object MixedDiplomacyScenario {
    val civilizations = listOf("Rome", "Greece", "France", "Egypt", "China")

    const val MAP_NAME = "Mixed-player diplomacy"
    val humanPlayers = setOf("France", "Egypt")

    fun scenario(runtime: GameExecutor, game: GameInfo, player: Civilization): GameplayScenario {
        val own = runtime.communicationState.trades.proposals.values.map { it.last() }.filter { player.civID in it.participants() }
        val role = when (player.civID) {
            "Rome" -> "On turn zero, inspect native diplomacy, issue China the native settlement demand, and explicitly denounce Greece once through chat_send.nativeAction. Send France a greeting and negotiate an immediate gift of 5–10 Gold using the shared trade interface; France is a human-controller test participant. Queue one gift of 20–30 Gold to China using trade_offer. Do not wait for China: finish your turn. On your next turn inspect the actual native outcomes and responses, then update your private diplomatic plan."
            "Greece" -> "On turn zero, inspect Rome's native denouncement and acknowledge it without declaring war. Discuss it in ordinary chat; your words create no native promise. Offer China one native friendship request using friendship_offer, then continue your turn without waiting. On your next turn inspect China's actual decision and update your private plan."
            else -> "Use the shared chat/trade interface with the agents. France may negotiate and accept Rome's 5–10 Gold goodwill gift. Both human participants then finish ordinary turns so the native AI can respond. Do not use the native offer bridge."
        }
        return GameplayScenario(
            "MIXED_PLAYER_DIPLOMACY",
            if (game.turns == 0) "OPENING" else "REVIEW",
            listOf(
                "Guided diplomacy exercise: Rome and Greece are agents, France and Egypt are human controllers, China is the built-in AI. A city-state is present; direct city-state actions are outside this exercise.",
                role,
                "Read player_turn before every explicit native chat action, inspect its options and consequences, and use an owned alert handle for a response. Trade and peace always use the trade tools. Social language stays nonbinding.",
                "Check chat_read and trade_read before opening anything. Continue existing proposals across jobs and restarts; never duplicate a milestone. Native AI requests have no wall-clock deadline and do not pause gameplay. The native AI is free to decline or counter.",
                "Your current proposal references: ${own.joinToString { "${it.proposal} (${it.status})" }.ifEmpty { "none" }}.",
                "Keep city production and research running, hold military units, and finish the turn after the diplomacy milestones. Use current sourced observations for your private plan.",
            ),
        )
    }

    fun create(seed: Long, mapName: String = MAP_NAME): GameInfo {
        val parameters = GameParameters().apply {
            this.baseRuleset = BaseRuleset.Civ_V_GnK.fullName
            speed = "Quick"
            noBarbarians = true
            numberOfCityStates = 1
            shufflePlayerOrder = false
            victoryTypes = arrayListOf("Domination", "Time")
            maxTurns = 330
            players = ArrayList(civilizations.map { Player(it, if (it == "China") PlayerType.AI else PlayerType.Human) })
        }
        val setup = GameSetupInfo(
            parameters,
            MapParameters().apply {
                noRuins = true
                this.seed = seed
            },
        )
        val ruleset = RulesetCache.getComplexRuleset(parameters)
        val positions = listOf(HexCoord(-10, -10), HexCoord(10, 0), HexCoord(0, 10), HexCoord(-10, 0), HexCoord(0, -10))
        val map = TileMap(14, ruleset).apply {
            mapParameters = setup.mapParameters.apply {
                name = mapName
                mapSize = MapSize(14)
            }
            for (tile in values) {
                tile.baseTerrain = "Grassland"
                tile.setTerrainTransients()
            }
            civilizations.zip(positions).forEach { (civ, position) -> addStartingLocation(civ, this[position]) }
        }
        val temporary = Files.createTempFile("mixed-diplomacy-", ".json")
        val game = try {
            Files.writeString(temporary, MapSaver.mapToSavedString(map))
            setup.mapFile = FileHandle(temporary.toFile())
            GameStarter.startNewGame(setup)
        } finally {
            Files.deleteIfExists(temporary)
        }
        val players = civilizations.map(game::getCivilization)
        game.civilizations.forEach { it.diplomacy.clear() }
        players.forEach { player ->
            player.units.getCivUnits().toList().forEach { it.destroy() }
        }
        game.tileMap.values.forEach { tile -> players.forEach { tile.setExplored(it, false) } }
        val frontiers = listOf(HexCoord(-6, -6), HexCoord(6, 0), HexCoord(0, 6), HexCoord(-6, 0), HexCoord(0, -6))
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
        // Found the minor's capital at a fixed legal location. A random starting settler
        // could otherwise be trapped inside the major cities prepared above.
        val minor = game.civilizations.single { it.isCityState }
        minor.units.getCivUnits().toList().forEach { it.destroy() }
        val minorSettler = checkNotNull(game.tileMap.placeUnitNearTile(HexCoord(10, 10), "Settler", minor))
        checkNotNull(UnitSettlement.tryFoundCity(minorSettler))
        minor.cache.updateCivResources()
        minor.cache.updateViewableTiles()
        minor.updateStatsForNextTurn()
        for (a in players) for (b in players) if (a !== b && !a.knows(b)) a.diplomacyFunctions.makeCivilizationsMeet(b)
        players.forEach {
            it.popupAlerts.clear()
            it.notifications.clear()
        }
        check(game.currentPlayer == "Rome" && players.count { it.isHuman() } == 4 && players.count { it.isAI() } == 1)
        return game
    }
}
