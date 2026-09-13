package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class CommunicationIntegrationTest {
    @Test
    fun `peace consent shares trade transactions invalidates attacks and rolls back atomically`() {
        runProbe("uncivai.PeaceTradeProbe")
    }

    @Test
    fun `economic consent applies exact terms out of turn and survives recovery`() {
        runProbe("uncivai.EngineTradeProbe")
    }

    @Test
    fun `chat privacy consent and complete round recovery`() {
        runProbe("uncivai.CommunicationProbe")
    }

    @Test
    fun `private betrayal role social pact and authoritative declaration survive restart`() {
        runProbe("uncivai.VerbalBetrayalProbe")
    }

    private fun runProbe(probe: String) {
        val root = Files.createTempDirectory("communication-")
        val log = root.resolve("probe.log")
        val process = ProcessBuilder(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(),
            "-Xmx1g",
            "-Djava.awt.headless=true",
            "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
            "-cp",
            System.getProperty("uncivai.testClasspath"),
            probe,
            root.toString(),
        ).redirectErrorStream(true).redirectOutput(log.toFile()).start()
        try {
            check(process.waitFor(120, TimeUnit.SECONDS)) { "Communication probe timed out: ${Files.readString(log)}" }
            assertEquals(Files.readString(log), 0, process.exitValue())
        } finally {
            if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
