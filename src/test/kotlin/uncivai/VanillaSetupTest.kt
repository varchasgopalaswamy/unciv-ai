package uncivai

import com.unciv.logic.map.MapType
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import java.nio.file.Files

class VanillaSetupTest {
    @Test
    fun `configured rules reach the native game and determine its duration`() {
        val directory = Files.createTempDirectory("native-setup-")
        try {
            HeadlessRuntime.createVanillaGame(
                directory,
                421L,
                agentPlayers = listOf("Rome", "Greece"),
                options = VanillaOptions(MapType.fractal, "King", "Standard", "Medieval era", listOf("Domination", "Scientific")),
            ).use { runtime ->
                val parameters = runtime.game.gameParameters
                assertEquals("King", parameters.difficulty)
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
