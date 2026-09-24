package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class ObservationOpportunitiesIntegrationTest {
    @Test fun `settlement recommendations distinguish a crowded explored map`() = run("settlement")

    @Test fun `construction options compare with selection time through restart`() = run("construction")

    @Test fun `resource work and road rail connections follow native availability`() = run("development")

    @Test fun `luxury opportunities require contact and permitted trade quantities`() = run("diplomacy")

    private fun run(scenario: String) {
        val root = Files.createTempDirectory("observation-opportunities-$scenario-")
        try {
            val log = root.resolve("probe.log")
            val process = ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-Xmx1g", "-Djava.awt.headless=true",
                "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}", "-cp", System.getProperty("uncivai.testClasspath"),
                "uncivai.ObservationOpportunitiesProbe", root.toString(), scenario,
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
