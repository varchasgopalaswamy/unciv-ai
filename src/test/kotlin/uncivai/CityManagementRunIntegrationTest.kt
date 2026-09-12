package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class CityManagementRunIntegrationTest {
    @Test
    fun `CLI creates the city management exercise and rejects a different scenario on resume`() {
        val root = Files.createTempDirectory("city-management-launch-")
        try {
            val checkpoint = root.resolve("checkpoint.json")
            val create = listOf("--new-duel", "--checkpoint", checkpoint.toString(), "--scenario", "city-management")
            launch(root, "create", create, 0)
            val original = Files.readString(checkpoint)
            val saved = Json.parseToJsonElement(original).jsonObject
            val game = Json.parseToJsonElement(saved.getValue("gameJson").jsonPrimitive.content).jsonObject
            assertEquals(CityManagementScenarioSetup.MAP_NAME, game.getValue("tileMap").jsonObject.getValue("mapParameters").jsonObject.getValue("name").jsonPrimitive.content)
            val players = game.getValue("civilizations").jsonArray.map { it.jsonObject }.filter { it.getValue("civID").jsonPrimitive.content in setOf("Rome", "Greece") }
            assertEquals(2, players.size)
            for (player in players) {
                assertEquals("1000", player.getValue("gold").jsonPrimitive.content)
                val cities = player.getValue("cities").jsonArray
                assertEquals(2, cities.size)
                assertEquals(setOf("3", "4"), cities.map { it.jsonObject.getValue("population").jsonObject.getValue("population").jsonPrimitive.content }.toSet())
                for (city in cities) {
                    val buildings = city.jsonObject.getValue("cityConstructions").jsonObject.getValue("builtBuildings").jsonArray.map { it.jsonPrimitive.content }
                    assertTrue(buildings.containsAll(listOf("University", "Granary", "Library", "Market")))
                    assertEquals("true", city.jsonObject.getValue("avoidGrowth").jsonPrimitive.content)
                }
            }
            launch(root, "resume", listOf("--resume-checkpoint", checkpoint.toString(), "--scenario", "city-management"), 0)
            assertEquals(original, Files.readString(checkpoint))

            val ordinary = root.resolve("ordinary.json")
            launch(root, "ordinary", listOf("--new-duel", "--checkpoint", ordinary.toString(), "--seed", "42"), 0)
            val before = Files.readString(ordinary)
            launch(root, "mismatch", listOf("--resume-checkpoint", ordinary.toString(), "--scenario", "city-management"), 2)
            assertTrue(Files.readString(root.resolve("mismatch.log")).contains("Checkpoint is not the city-management scenario"))
            assertEquals(before, Files.readString(ordinary))
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    private fun launch(root: Path, stage: String, arguments: List<String>, expectedExit: Int) {
        val log = root.resolve("$stage.log")
        val command = listOf(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-Xmx1g", "-Djava.awt.headless=true",
            "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}", "-cp", System.getProperty("uncivai.testClasspath"),
            "uncivai.MainKt", "--data-dir", root.resolve(stage).toString(),
        ) + arguments
        val process = ProcessBuilder(command).directory(root.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
        try {
            check(process.waitFor(120, TimeUnit.SECONDS)) { "Scenario launch timed out: ${Files.readString(log)}" }
            assertEquals(Files.readString(log), expectedExit, process.exitValue())
        } finally {
            if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
        }
    }

    @Test
    fun `HTTPS city management updates native saves across turns rollback and restarts`() {
        val root = Files.createTempDirectory("city-management-run-")
        try {
            for (stage in listOf("observe", "start", "restore", "verify")) {
                val log = root.resolve("$stage.log")
                val process = ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-Xmx1g", "-Djava.awt.headless=true",
                    "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}", "-cp", System.getProperty("uncivai.testClasspath"),
                    "uncivai.CityManagementRunProbe", root.toString(), stage,
                ).directory(root.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
                try {
                    check(process.waitFor(180, TimeUnit.SECONDS)) { "City management stage $stage timed out: ${Files.readString(log)}" }
                    assertEquals("$stage: ${Files.readString(log)}", 0, process.exitValue())
                } finally {
                    if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
                }
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
