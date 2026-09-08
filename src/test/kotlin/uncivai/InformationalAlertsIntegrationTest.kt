package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class InformationalAlertsIntegrationTest {
    @Test
    fun `HTTPS informational alerts expose complete player content and require observation before dismissal`() {
        runStages("content")
    }

    @Test
    fun `pending content survives process restart while observation resets and committed acknowledgements replay`() {
        runStages("prepare", "acknowledge", "replay")
    }

    private fun runStages(vararg stages: String) {
        val root = Files.createTempDirectory("informational-alerts-")
        try {
            for (stage in stages) {
                val log = root.resolve("$stage.log")
                val process = ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-Xmx1g", "-Djava.awt.headless=true",
                    "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
                    "-cp", System.getProperty("uncivai.testClasspath"),
                    "uncivai.InformationalAlertsProbe", root.toString(), stage,
                ).directory(root.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
                try {
                    check(process.waitFor(120, TimeUnit.SECONDS)) { "Alert fixture timed out: ${Files.readString(log)}" }
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
