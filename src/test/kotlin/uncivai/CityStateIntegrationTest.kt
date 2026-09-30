package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class CityStateIntegrationTest {
    @Test fun `ordinary game creates and restores native city states`() = run("ordinary")

    @Test fun `gifts missions protection quests and alliances use native effects through HTTPS`() = run("interactions")

    @Test fun `quest gifts require current funds`() = run("affordability")

    @Test fun `city state war peace and tribute use native constraints`() = run("conflict")

    @Test fun `city state popups resolve through the turn API and survive recovery`() = run("popups")

    @Test fun `city state observations protect unknown identities and hidden resources`() = run("privacy")

    @Test fun `every native quest retains its mechanical requirement in serialized observations`() = run("requirements")

    @Test fun `city state commands recover before and after checkpoint replacement`() = run("recovery")

    @Test fun `marriage reports elimination and resolves transferred cities across recovery`() = run("marriage")

    private fun run(scenario: String) {
        val root = Files.createTempDirectory("city-state-$scenario-")
        try {
            val log = root.resolve("probe.log")
            val process = ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-Xmx1g", "-Djava.awt.headless=true",
                "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}", "-cp", System.getProperty("uncivai.testClasspath"),
                "uncivai.CityStateProbe", root.toString(), scenario,
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
