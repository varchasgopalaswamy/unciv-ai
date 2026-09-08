package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class EncounterRunIntegrationTest {
    @Test
    fun `HTTPS players meet acknowledge across two process restarts and continue four rounds`() {
        val root = Files.createTempDirectory("encounter-run-")
        try {
            for (stage in listOf("meet", "acknowledge", "continue")) {
                val log = root.resolve("$stage.log")
                val process = ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-Xmx1g", "-Djava.awt.headless=true",
                    "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
                    "-cp", System.getProperty("uncivai.testClasspath"),
                    "uncivai.EncounterRunProbe", root.toString(), stage,
                ).directory(root.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
                try {
                    check(process.waitFor(120, TimeUnit.SECONDS)) { "Encounter fixture timed out: ${Files.readString(log)}" }
                    assertEquals(Files.readString(log), 0, process.exitValue())
                } finally {
                    if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
                }
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
