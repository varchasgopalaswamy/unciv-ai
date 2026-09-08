package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class WarPeaceRunIntegrationTest {
    @Test
    fun `HTTPS players meet declare war negotiate paid peace across two restarts and continue four rounds`() {
        val root = Files.createTempDirectory("war-peace-run-")
        try {
            for (stage in listOf("offer", "accept", "continue")) {
                val log = root.resolve("$stage.log")
                val process = ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-Xmx1g", "-Djava.awt.headless=true",
                    "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
                    "-cp", System.getProperty("uncivai.testClasspath"),
                    "uncivai.WarPeaceRunProbe", root.toString(), stage,
                ).directory(root.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
                try {
                    check(process.waitFor(150, TimeUnit.SECONDS)) { "War and peace fixture timed out: ${Files.readString(log)}" }
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
