package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class ObservationOpportunitiesIntegrationTest {
    @Test fun `additional player controls use native effects and explicit turn deferral`() = run("coverage-controls")

    @Test fun `reference history and world inspections are bounded private and read only`() = run("coverage-information")

    @Test fun `civilization profiles expose future uniques conditions and replacement tradeoffs before founding`() = run("civilization-profile")

    @Test fun `civilization profiles use the selected bundled ruleset`() = run("civilization-profile-gnk")

    @Test fun `intermediate movement discoveries retain terrain through fog and restart`() = run("map-knowledge-movement")

    @Test fun `technology reveals fogged resources without revealing hidden development`() = run("map-knowledge-resources")

    @Test fun `hidden terrain and infrastructure retain last observations through restart`() = run("map-knowledge")

    @Test fun `known cities retain identity while current defenses require visibility`() = run("map-knowledge-cities")

    @Test fun `mechanics overview uses native research culture and great person progress`() = run("mechanics")

    @Test fun `era progress follows own and known major eras without revealing unmet research`() = run("era-progress")

    @Test fun `expansion resources and happiness respect knowledge and native rules`() = run("expansion")

    @Test fun `settlement recommendations distinguish a crowded explored map`() = run("settlement")

    @Test fun `settlement advice exists before settlers and follows native restrictions under fog`() = run("settlement-before-settler")

    @Test fun `native exploration and worker automation support commands replay and restart`() = run("automation")

    @Test fun `construction options compare with selection time through restart`() = run("construction")

    @Test fun `resource work and road rail connections follow native availability`() = run("development")

    @Test fun `resource opportunities include clearing technologies and distinguish water builders`() = run("development-tech")

    @Test fun `luxury opportunities require contact and permitted trade quantities`() = run("diplomacy")

    @Test fun `settlers retain progress and compare current targets through restart`() = run("settler-progress")

    @Test fun `military readiness distinguishes recon and visible peaceful forces`() = run("military")

    @Test fun `production conversion previews match native output without changing queues`() = run("production-conversion")

    @Test fun `agent conversions unlock only in the final era including saved and automated queues`() = run("production-policy")

    @Test fun `unbuilt wonders expose benefits and city requirements without rival progress`() = run("wonders")

    @Test fun `city building estimates match native completion and remain pure through HTTPS`() = run("building-estimates")

    @Test fun `saved small city compares library and granary benefits without changing native state`() = run("building-estimates-saved")

    private fun run(scenario: String) {
        val root = Files.createTempDirectory("observation-opportunities-$scenario-")
        try {
            val log = root.resolve("probe.log")
            val process = ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-Xmx1g", "-Djava.awt.headless=true",
                "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}", "-cp", System.getProperty("uncivai.testClasspath"),
                "uncivai.ObservationOpportunitiesProbe", root.toString(), scenario, System.getProperty("uncivai.testFixtures"),
            ).redirectErrorStream(true).redirectOutput(log.toFile()).start()
            try {
                check(process.waitFor(120, TimeUnit.SECONDS)) { "Timed out: ${Files.readString(log)}" }
                assertEquals(Files.readString(log), 0, process.exitValue())
                Files.readAllLines(log).filter { it.startsWith("building estimates:") }.forEach(::println)
            } finally {
                if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
