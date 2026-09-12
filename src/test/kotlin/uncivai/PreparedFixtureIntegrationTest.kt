package uncivai

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFileAttributeView
import java.nio.file.attribute.PosixFilePermissions
import java.util.concurrent.TimeUnit

class PreparedFixtureIntegrationTest {
    @Test
    fun `interactive fixtures prepare private launch credentials and never replace existing match state`() {
        val root = Files.createTempDirectory("prepared-fixture-")
        try {
            val tokens = hashSetOf<String>()
            for ((probe, stage) in listOf("EconomyRunProbe" to "prepare", "CityManagementRunProbe" to "prepare", "CombatRunProbe" to "prepare-conquest", "CombatRunProbe" to "prepare-victory")) {
                val match = root.resolve("$probe-$stage")
                val output = prepare(root, match, probe, stage, success = true)
                val players = match.resolve("players.json")
                val credentials = loadMcpPlayerConfig(players)
                assertEquals(listOf("Rome", "Greece"), credentials.map { it.civilizationId })
                for (credential in credentials) {
                    assertTrue("Fixture tokens must be distinct", tokens.add(credential.token))
                    assertFalse("Preparation must not print credentials", output.contains(credential.token))
                }
                if (Files.getFileAttributeView(players, PosixFileAttributeView::class.java) != null) {
                    assertEquals(PosixFilePermissions.fromString("rw-------"), Files.getPosixFilePermissions(players))
                }
                val checkpoint = match.resolve("exploration.json")
                val beforeSave = Files.readAllBytes(checkpoint)
                val beforeCredentials = Files.readAllBytes(players)
                // A partial preparation can reuse credentials; a completed one cannot overwrite its save.
                prepareDuelCredentials(match)
                prepare(root, match, probe, stage, success = false)
                assertTrue(beforeSave.contentEquals(Files.readAllBytes(checkpoint)))
                assertTrue(beforeCredentials.contentEquals(Files.readAllBytes(players)))
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    private fun prepare(root: Path, match: Path, probe: String, stage: String, success: Boolean): String {
        val log = root.resolve("$stage-$success.log")
        val process = ProcessBuilder(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-Xmx1g", "-Djava.awt.headless=true",
            "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}", "-cp", System.getProperty("uncivai.testClasspath"),
            "uncivai.$probe", match.toString(), stage,
        ).redirectErrorStream(true).redirectOutput(log.toFile()).start()
        try {
            check(process.waitFor(60, TimeUnit.SECONDS)) { "Fixture preparation timed out: ${Files.readString(log)}" }
            val output = Files.readString(log)
            assertEquals(output, success, process.exitValue() == 0)
            return output
        } finally {
            if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
        }
    }
}
