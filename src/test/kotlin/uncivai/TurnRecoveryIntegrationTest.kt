package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class TurnRecoveryIntegrationTest {
    @Test
    fun `interrupted scheduled game turn rolls back across restarts without losing committed history`() {
        probeStages(listOf("mutate", "restore", "verify"))
    }

    @Test
    fun `restore persistence failures recover on either side of atomic replacement`() {
        probeStages(listOf("failure-before", "failure-after"))
    }

    private fun probeStages(stages: List<String>) {
        val root = Files.createTempDirectory("scheduled-restore-")
        try {
            for (stage in stages) {
                val log = root.resolve("$stage.log")
                val process = ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-Xmx1g", "-Djava.awt.headless=true",
                    "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
                    "-cp", System.getProperty("uncivai.testClasspath"),
                    "uncivai.TurnRecoveryProbe", root.toString(), stage,
                ).directory(root.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
                try {
                    check(process.waitFor(150, TimeUnit.SECONDS)) { "Restore fixture timed out: ${Files.readString(log)}" }
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
