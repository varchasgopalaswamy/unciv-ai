package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class VictoryProgressIntegrationTest {
    @Test
    fun `victory progress matches displayed milestones and survives restart without revealing unmet rivals`() {
        val root = Files.createTempDirectory("victory-progress-")
        try {
            for (stage in listOf("observe", "restore")) {
                val log = root.resolve("$stage.log")
                val process = ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-Xmx1g", "-Djava.awt.headless=true",
                    "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
                    "-cp", System.getProperty("uncivai.testClasspath"),
                    "uncivai.VictoryProgressProbe", root.toString(), stage,
                ).redirectErrorStream(true).redirectOutput(log.toFile()).start()
                try {
                    check(process.waitFor(90, TimeUnit.SECONDS)) { "Timed out: ${Files.readString(log)}" }
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
