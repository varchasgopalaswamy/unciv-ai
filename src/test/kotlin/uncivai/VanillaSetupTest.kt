package uncivai

import com.unciv.logic.map.MapSize
import com.unciv.logic.map.MapType
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

class VanillaSetupTest {
    @Test
    fun `civilization starts survive reassignment between human agent and native slots`() {
        val directory = Files.createTempDirectory("independent-assignments-")
        try {
            val rosters = listOf(
                listOf("Greece", "France", "Egypt") to listOf("Rome"),
                listOf("Rome", "Greece", "Egypt") to listOf("France"),
                listOf("Egypt", "Rome", "Greece") to listOf("France"),
            )
            val openings = rosters.mapIndexed { index, (external, native) ->
                HeadlessRuntime.createVanillaGame(
                    directory.resolve("game-$index"),
                    422L,
                    cityStates = 2,
                    builtInAI = native,
                    agentPlayers = external,
                    mapSize = MapSize.Tiny,
                    options = VanillaOptions(difficulty = "King", startingPositionSeed = 17L),
                ).use { runtime ->
                    val game = runtime.game
                    assertEquals(external + native, game.civilizations.filter { it.isMajorCiv() }.map { it.civID })
                    assertEquals(external.first(), game.currentPlayer)
                    val starts = game.civilizations.filter { !it.isBarbarian && !it.isSpectator() }.associate { civ ->
                        civ.civID to civ.units.getCivUnits().single { it.baseUnit.isCityFounder() }.getTile().position
                    }
                    val tiles = game.tileMap.values.map {
                        "${it.position}:${it.baseTerrain}:${it.terrainFeatures}:${it.naturalWonder}:" +
                            "${it.resource}:${it.resourceAmount}:${it.improvement}:" +
                            "${it.hasBottomRiver}:${it.hasBottomLeftRiver}:${it.hasBottomRightRiver}"
                    }
                    val difficulty = game.getDifficulty()
                    assertTrue(difficulty.aiMajorCivBonusStartingUnits.isNotEmpty())
                    for (name in native + external) {
                        val civ = game.getCivilization(name)
                        assertEquals(name in native, civ.isAI())
                        val bonuses = if (civ.isAI()) difficulty.aiMajorCivBonusStartingUnits else difficulty.playerBonusStartingUnits
                        val expected = game.ruleset.eras.getValue(game.gameParameters.startingEra).getStartingUnits(game.ruleset) + bonuses
                        assertEquals(expected.size, civ.units.getCivUnits().count())
                    }
                    starts to tiles
                }
            }
            assertEquals(openings[0], openings[1])
            assertEquals(openings[0], openings[2])
        } finally {
            deleteTreeWithoutFollowingLinks(directory)
        }
    }

    @Test
    fun `assigned built in opponents play after all externally controlled civilizations`() {
        val directory = Files.createTempDirectory("assigned-players-")
        try {
            // The assigned native opponents may have appeared first in a roster.
            // Both display and headless callers pass the resolved controller groups.
            HeadlessRuntime.createVanillaGame(
                directory,
                422L,
                builtInAI = listOf("Rome", "Egypt"),
                agentPlayers = listOf("Greece", "France", "China"),
            ).use { runtime ->
                val game = runtime.game
                assertEquals(listOf("Greece", "France", "China", "Rome", "Egypt"), game.civilizations.filter { it.isMajorCiv() }.map { it.civID })
                assertEquals("Greece", game.currentPlayer)
                runtime.executor.accessGame {
                    game.nextTurn()
                    assertEquals("France", game.currentPlayer)
                    game.nextTurn()
                    assertEquals("China", game.currentPlayer)
                    game.nextTurn()
                    assertEquals("Greece", game.currentPlayer)
                    for (name in listOf("Rome", "Egypt")) {
                        assertTrue(game.getCivilization(name).cities.isNotEmpty())
                    }
                }
            }
        } finally {
            deleteTreeWithoutFollowingLinks(directory)
        }
    }

    @Test
    fun `configured rules reach the native game and determine its duration`() {
        val directory = Files.createTempDirectory("native-setup-")
        try {
            HeadlessRuntime.createVanillaGame(
                directory,
                421L,
                agentPlayers = listOf("Rome", "Greece"),
                options = VanillaOptions(MapType.fractal, "King", "Standard", "Medieval era", listOf("Domination", "Scientific"), 73L),
            ).use { runtime ->
                val parameters = runtime.game.gameParameters
                assertEquals("King", parameters.difficulty)
                assertEquals(73L, parameters.startingPositionSeed)
                assertEquals("Standard", parameters.speed)
                assertEquals("Medieval era", parameters.startingEra)
                assertEquals(listOf("Domination", "Scientific"), parameters.victoryTypes)
                assertEquals(runtime.game.ruleset.speeds.getValue("Standard").numTotalTurns(), parameters.maxTurns)
                assertEquals(MapType.fractal, runtime.game.tileMap.mapParameters.type)
            }
        } finally {
            deleteTreeWithoutFollowingLinks(directory)
        }
    }

    @Test
    fun `unknown native choices fail before creating a game`() {
        val directory = Files.createTempDirectory("native-invalid-setup-")
        try {
            for (options in listOf(
                VanillaOptions(difficulty = "missing"),
                VanillaOptions(speed = "missing"),
                VanillaOptions(startingEra = "missing"),
                VanillaOptions(mapType = "missing"),
                VanillaOptions(victories = listOf("missing")),
            )) {
                assertThrows(IllegalArgumentException::class.java) {
                    HeadlessRuntime.createVanillaGame(directory, 421L, options = options).close()
                }
            }
        } finally {
            deleteTreeWithoutFollowingLinks(directory)
        }
    }
}
