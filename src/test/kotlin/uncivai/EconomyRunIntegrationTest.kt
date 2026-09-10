package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class EconomyRunIntegrationTest {
    @Test
    fun `HTTPS resource improvement purchases upgrades pillage and disband survive rollback and restarts`() {
        val root = Files.createTempDirectory("economy-run-")
        try {
            for (stage in listOf("start", "restore", "verify")) {
                val log = root.resolve("$stage.log")
                val process = ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-Xmx1g", "-Djava.awt.headless=true",
                    "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}", "-cp", System.getProperty("uncivai.testClasspath"),
                    "uncivai.EconomyRunProbe", root.toString(), stage,
                ).directory(root.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
                try {
                    check(process.waitFor(180, TimeUnit.SECONDS)) { "Economy stage $stage timed out: ${Files.readString(log)}" }
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
