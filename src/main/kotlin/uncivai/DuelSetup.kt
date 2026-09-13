package uncivai

import com.badlogic.gdx.files.FileHandle
import com.unciv.logic.GameInfo
import com.unciv.logic.GameStarter
import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.files.MapSaver
import com.unciv.logic.map.MapParameters
import com.unciv.logic.map.MapSize
import com.unciv.logic.map.MapType
import com.unciv.logic.map.TileMap
import com.unciv.logic.map.mapgenerator.MapGenerator
import com.unciv.logic.map.tile.Tile
import com.unciv.models.metadata.BaseRuleset
import com.unciv.models.metadata.GameParameters
import com.unciv.models.metadata.GameSetupInfo
import com.unciv.models.metadata.Player
import com.unciv.models.ruleset.RulesetCache
import java.nio.file.Files

/** Operator-owned setup. Human engine slots prevent built-in AI substitution. */
internal class DuelSetup(private val seed: Long, private val closeStarts: Boolean = false, private val combatScenario: Boolean = false, private val greatPeopleScenario: Boolean = false, private val tradeScenario: Boolean = false) {
    fun create(): GameInfo {
        require(listOf(closeStarts, combatScenario, greatPeopleScenario, tradeScenario).count { it } <= 1) { "Choose one scenario" }
        val parameters = GameParameters().apply {
            baseRuleset = BaseRuleset.Civ_V_Vanilla.fullName
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
            this.seed = this@DuelSetup.seed
        }
        val setup = GameSetupInfo(parameters, map)
        return when {
            tradeScenario -> TradeScenarioSetup.create(setup)
            greatPeopleScenario -> GreatPeopleScenarioSetup.create(setup)
            combatScenario -> CombatScenarioSetup.create(setup)
            closeStarts -> createCloseDuel(setup)
            else -> GameStarter.startNewGame(setup)
        }.also { game ->
            check(game.gameParameters.baseRuleset == parameters.baseRuleset && game.gameParameters.mods.isEmpty())
            check(game.civilizations.filter { it.isMajorCiv() }.map { it.civName }.toSet() == setOf("Rome", "Greece"))
            check(game.civilizations.filter { it.isMajorCiv() }.all { it.isHuman() })
            check(game.currentPlayer == "Rome")
        }
    }

    private fun createCloseDuel(setup: GameSetupInfo): GameInfo {
        val map = MapGenerator(RulesetCache.getComplexRuleset(setup.gameParameters))
            .generateMap(setup.mapParameters, setup.gameParameters)
        val path = closeStartingPath(map)
        map.clearStartingLocations()
        map.addStartingLocation("Rome", path.first())
        map.addStartingLocation("Greece", path.last())
        // Load explicit starts through the normal new-game path, before units or fog exist.
        setup.mapParameters.name = "Close-start duel"
        val temporary = Files.createTempFile("unciv-close-starts-", ".json")
        val game = try {
            Files.writeString(temporary, MapSaver.mapToSavedString(map))
            setup.mapFile = FileHandle(temporary.toFile())
            GameStarter.startNewGame(setup)
        } finally {
            Files.deleteIfExists(temporary)
        }
        for ((civilization, position) in listOf("Rome" to path[2].position, "Greece" to path[3].position)) {
            val player = game.getCivilization(civilization)
            val scout = checkNotNull(game.tileMap.placeUnitNearTile(position, "Scout", player))
            check(scout.currentTile.position == position) { "Could not place the starting Scout" }
        }
        val players = listOf("Rome", "Greece").map(game::getCivilization)
        players.forEach { it.cache.updateViewableTiles() }
        check(players[0].knows(players[1]) && players[1].knows(players[0])) { "Starting Scouts did not make contact" }
        return game
    }

    /** Five land steps leave room for both capitals; adjacent Scouts make ordinary visual contact. */
    private fun closeStartingPath(map: TileMap): List<Tile> {
        fun passable(tile: Tile) = tile.isLand && !tile.isImpassible() && !tile.isNaturalWonder()
        fun quality(tile: Tile): Int {
            var score = 0
            tile.forEachTileInDistance(2) { if (passable(it) && it.baseTerrain in setOf("Grassland", "Plains")) score++ }
            return score
        }
        val starts = map.values.filter { passable(it) && it.baseTerrain in setOf("Grassland", "Plains") }
            .sortedByDescending(::quality)
        for (start in starts) {
            val paths = linkedMapOf(start to listOf(start))
            val queue = ArrayDeque<Tile>().apply { add(start) }
            while (queue.isNotEmpty()) {
                val current = queue.removeFirst()
                val path = paths.getValue(current)
                if (path.size == 6) continue
                for (next in current.neighbors.filter(::passable)) {
                    if (next in paths) continue
                    paths[next] = path + next
                    queue.add(next)
                }
            }
            val end = starts.filter { paths[it]?.size == 6 && start.aerialDistanceTo(it) == 5 }
                .maxByOrNull(::quality)
            if (end != null) return paths.getValue(end)
        }
        error("Could not find connected close starting locations for this seed")
    }
}
