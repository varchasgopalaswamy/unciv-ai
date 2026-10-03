package uncivai

import com.unciv.logic.map.MapType
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

class VanillaSetupTest {
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
