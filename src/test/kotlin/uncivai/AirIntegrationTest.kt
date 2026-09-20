package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class AirIntegrationTest {
    @Test fun `aircraft missions carrier travel and receipts survive HTTPS restart`() = run("missions")

    @Test fun `nuclear confirmation native war and filtered outcomes survive restart`() = run("nuclear")

    @Test fun `air and nuclear queries do not disclose hidden forces`() = run("privacy")

    @Test fun `special missions recover across interrupted checkpoint replacement`() = run("recovery")

    private fun run(scenario: String) {
        val root = Files.createTempDirectory("air-$scenario-")
        try {
            val log = root.resolve("probe.log")
            val process = ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-Xmx1g", "-Djava.awt.headless=true",
                "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}", "-cp", System.getProperty("uncivai.testClasspath"),
                "uncivai.AirProbe", root.toString(), scenario,
            ).redirectErrorStream(true).redirectOutput(log.toFile()).start()
            try {
                check(process.waitFor(240, TimeUnit.SECONDS)) { "Timed out: ${Files.readString(log)}" }
                assertEquals(Files.readString(log), 0, process.exitValue())
            } finally {
                if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
