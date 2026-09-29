package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class SettlementPreviewIntegrationTest {
    @Test fun `settlement previews explain sites separately from movement without prohibiting travel`() = run("rules")

    @Test fun `settlement lens follows native restrictions under fog without disclosing city identities`() = run("privacy")

    @Test fun `coastal settler can choose an eligible neighbor of a site blocked by an undiscovered city`() = run("coast")

    @Test fun `standing orders report destination sites independently and clear on cancellation or arrival`() = run("orders")

    private fun run(scenario: String) {
        val root = Files.createTempDirectory("settlement-preview-$scenario-")
        try {
            val log = root.resolve("probe.log")
            val process = ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-Xmx1g", "-Djava.awt.headless=true",
                "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}", "-cp", System.getProperty("uncivai.testClasspath"),
                "uncivai.SettlementPreviewProbe", root.toString(), scenario,
            ).redirectErrorStream(true).redirectOutput(log.toFile()).start()
            try {
                check(process.waitFor(120, TimeUnit.SECONDS)) { "Timed out: ${Files.readString(log)}" }
                assertEquals(Files.readString(log), 0, process.exitValue())
            } finally {
                if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
