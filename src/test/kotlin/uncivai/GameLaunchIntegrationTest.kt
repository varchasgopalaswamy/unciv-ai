package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class GameLaunchIntegrationTest {
    @Test
    fun `agreement lifecycle CLI creates resumes and rejects a mismatched scenario`() {
        val root = Files.createTempDirectory("agreement-lifecycle-launch-")
        try {
            val checkpoint = root.resolve("checkpoint.json")
            launch(root, "create", listOf("--new-game", "--checkpoint", checkpoint.toString(), "--scenario", "agreement-lifecycle"), 0)
            val original = Files.readString(checkpoint)
            val saved = Json.parseToJsonElement(original).jsonObject
            val game = Json.parseToJsonElement(saved.getValue("gameJson").jsonPrimitive.content).jsonObject
            assertEquals(AgreementLifecycleScenarioSetup.MAP_NAME, game.getValue("tileMap").jsonObject.getValue("mapParameters").jsonObject.getValue("name").jsonPrimitive.content)
            launch(root, "resume", listOf("--resume-checkpoint", checkpoint.toString(), "--scenario", "agreement-lifecycle"), 0)
            assertEquals(original, Files.readString(checkpoint))
            launch(root, "mismatch", listOf("--resume-checkpoint", checkpoint.toString(), "--scenario", "war-paid"), 1)
            assertTrue(Files.readString(root.resolve("mismatch.log")).contains("Checkpoint scenario does not match"))
            assertEquals(original, Files.readString(checkpoint))
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    @Test
    fun `default CLI creates a Quick Vanilla three player game and resumes scheduled play`() {
        val root = Files.createTempDirectory("vanilla-launch-")
        try {
            val checkpoint = root.resolve("checkpoint.json")
            launch(root, "create", listOf("--new-game", "--checkpoint", checkpoint.toString(), "--seed", "-42", "--trade-counteroffers", "5"), 0)
            val original = Files.readString(checkpoint)
            launch(root, "resume", listOf("--resume-checkpoint", checkpoint.toString()), 0)
            assertEquals(original, Files.readString(checkpoint))
            launch(root, "wrong-scenario", listOf("--resume-checkpoint", checkpoint.toString(), "--scenario", "trade-cycle"), 1)
            assertEquals(original, Files.readString(checkpoint))
            launch(root, "scheduled-play", listOf(root.toString()), 0, "uncivai.VanillaGameProbe")
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    @Test
    fun `explicit trade cycle remains a two player exercise and rejects scenario mismatch without writing`() {
        val root = Files.createTempDirectory("trade-cycle-launch-")
        try {
            val checkpoint = root.resolve("checkpoint.json")
            launch(root, "create", listOf("--new-game", "--checkpoint", checkpoint.toString(), "--scenario", "trade-cycle"), 0)
            val original = Files.readString(checkpoint)
            val saved = Json.parseToJsonElement(original).jsonObject
            assertEquals("4", saved.getValue("formatVersion").jsonPrimitive.content)
            val game = Json.parseToJsonElement(saved.getValue("gameJson").jsonPrimitive.content).jsonObject
            val map = game.getValue("tileMap").jsonObject
            val parameters = map.getValue("mapParameters").jsonObject
            assertEquals(TradeCycleScenarioSetup.MAP_NAME, parameters.getValue("name").jsonPrimitive.content)
            val players = game.getValue("civilizations").jsonArray.map { it.jsonObject }.filter { it.getValue("civID").jsonPrimitive.content in setOf("Rome", "Greece") }
            assertEquals(2, players.size)
            assertTrue(players.all { it.getValue("cities").jsonArray.size == 2 })
            launch(root, "resume", listOf("--resume-checkpoint", checkpoint.toString(), "--scenario", "trade-cycle"), 0)
            assertEquals(original, Files.readString(checkpoint))
            launch(root, "resume-without-scenario", listOf("--resume-checkpoint", checkpoint.toString()), 0)
            assertEquals(original, Files.readString(checkpoint))
            val otherParameters = JsonObject(parameters + ("name" to JsonPrimitive("Ordinary map")))
            val otherMap = JsonObject(map + ("mapParameters" to otherParameters))
            val otherGame = JsonObject(game + ("tileMap" to otherMap))
            val other = root.resolve("other.json")
            val otherSaved = JsonObject(saved + ("gameJson" to JsonPrimitive(otherGame.toString())))
            Files.writeString(other, otherSaved.toString())
            launch(root, "mismatch", listOf("--resume-checkpoint", other.toString(), "--scenario", "trade-cycle"), 1)
            assertTrue(Files.readString(root.resolve("mismatch.log")).contains("Checkpoint scenario does not match"))
            assertEquals(otherSaved.toString(), Files.readString(other))
            val legacy = root.resolve("legacy.json")
            val legacySaved = JsonObject(saved + ("formatVersion" to JsonPrimitive(2))).toString()
            Files.writeString(legacy, legacySaved)
            launch(root, "legacy", listOf("--resume-checkpoint", legacy.toString()), 1)
            assertTrue(Files.readString(root.resolve("legacy.log")).contains("Unsupported checkpoint version; start a new game"))
            assertEquals(legacySaved, Files.readString(legacy))
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    @Test
    fun `war exercises preserve private history and negotiate native wars`() {
        val root = Files.createTempDirectory("war-launch-")
        try {
            launch(root, "war-probe", listOf(root.toString()), 0, "uncivai.WarScenarioProbe")
            val checkpoint = root.resolve("war-paid/exploration.json")
            val original = Files.readString(checkpoint)
            launch(root, "mismatched-war", listOf("--resume-checkpoint", checkpoint.toString(), "--scenario", "war-joint"), 1)
            assertEquals(original, Files.readString(checkpoint))
            launch(root, "resume-war", listOf("--resume-checkpoint", checkpoint.toString(), "--scenario", "war-paid"), 0)
            // A valid scheduled restart intentionally persists the paused state.
            val expected = communicationJson.decodeFromString<GameCheckpoint>(original).copy(paused = true)
            expected.communication.telemetry.turns.values.filter { it.endedAtMs == null }.forEach { it.timingComplete = false }
            assertEquals(communicationJson.encodeToJsonElement(GameCheckpoint.serializer(), expected), Json.parseToJsonElement(Files.readString(checkpoint)))
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    private fun launch(root: Path, stage: String, arguments: List<String>, expectedExit: Int, main: String = "uncivai.MainKt") {
        val log = root.resolve("$stage.log")
        val command = listOf(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(),
            "-Xmx1g",
            "-Djava.awt.headless=true",
            "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
            "-cp",
            System.getProperty("uncivai.testClasspath"),
            main,
        ) + (if (main == "uncivai.MainKt") listOf("--data-dir", root.resolve(stage).toString()) else emptyList()) + arguments
        val process = ProcessBuilder(command).directory(root.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
        try {
            check(process.waitFor(120, TimeUnit.SECONDS)) { "Scenario launch timed out: ${Files.readString(log)}" }
            assertEquals(Files.readString(log), expectedExit, process.exitValue())
        } finally {
            if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
        }
    }
}
