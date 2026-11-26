package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class VictoryDecisionsIntegrationTest {
    @Test fun `last spaceship part wins and replays across restart`() = run("scientific")

    @Test fun `all spaceship parts can be produced transported and assembled through the API`() = run("spaceship-production")

    @Test fun `failed election then winning election completes through HTTPS and restart`() = run("elections")

    @Test fun `city-state influence closes before the early player's ballot`() = run("city-state-vote-timing")

    @Test fun `ballots and results preserve visibility and unsupported requirements`() = run("privacy")

    @Test fun `victory choices recover before and after checkpoint replacement`() = run("recovery")

    @Test fun `ordinary production can complete cultural victory`() = run("cultural")

    @Test fun `ordinary handoffs can finish at the time limit`() = run("time")

    private fun run(scenario: String) {
        val root = Files.createTempDirectory("victory-$scenario-")
        try {
            val log = root.resolve("probe.log")
            val process = ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-Xmx1g", "-Djava.awt.headless=true",
                "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}", "-cp", System.getProperty("uncivai.testClasspath"),
                "uncivai.VictoryDecisionsProbe", root.toString(), scenario,
            ).redirectErrorStream(true).redirectOutput(log.toFile()).start()
            try {
                check(process.waitFor(if (scenario == "spaceship-production") 600 else 180, TimeUnit.SECONDS)) { "Timed out: ${Files.readString(log)}" }
                assertEquals(Files.readString(log), 0, process.exitValue())
                Files.readString(log).lineSequence().filter { it.startsWith("SPACE-PRODUCTION:") }.forEach(::println)
            } finally {
                if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
