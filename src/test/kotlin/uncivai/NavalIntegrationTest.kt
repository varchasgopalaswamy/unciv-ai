package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class NavalIntegrationTest {
    @Test fun `destination previews commands continuation and cancellation survive restart`() = run("movement")

    @Test fun `naval combat economy and work boat consumption have native HTTPS outcomes`() = run("naval")

    @Test fun `distant path queries protect unexplored terrain and unseen units`() = run("privacy")

    @Test fun `destination and work boat commands recover across interrupted checkpoint writes`() = run("recovery")

    @Test fun `automatic continuation is atomic with the turn handoff`() = run("handoff")

    private fun run(scenario: String) {
        val root = Files.createTempDirectory("naval-$scenario-")
        try {
            val log = root.resolve("probe.log")
            val process = ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-Xmx1g", "-Djava.awt.headless=true",
                "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}", "-cp", System.getProperty("uncivai.testClasspath"),
                "uncivai.NavalProbe", root.toString(), scenario,
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
