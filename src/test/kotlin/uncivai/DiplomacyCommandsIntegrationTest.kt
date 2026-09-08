package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class DiplomacyCommandsIntegrationTest {
    @Test
    fun `diplomacy commands preserve private information reject forged choices and detach cached terms`() {
        val root = Files.createTempDirectory("diplomacy-commands-")
        try {
            val log = root.resolve("probe.log")
            val process = ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-Xmx1g",
                "-Djava.awt.headless=true",
                "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
                "-cp",
                System.getProperty("uncivai.testClasspath"),
                "uncivai.DiplomacyCommandsProbe",
                root.toString(),
            ).directory(root.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
            try {
                check(process.waitFor(120, TimeUnit.SECONDS)) { "Diplomacy command probe timed out: ${Files.readString(log)}" }
                assertEquals(Files.readString(log), 0, process.exitValue())
            } finally {
                if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
