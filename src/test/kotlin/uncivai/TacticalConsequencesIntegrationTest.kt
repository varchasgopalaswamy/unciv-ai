package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class TacticalConsequencesIntegrationTest {
    @Test fun `damage projections expose lethal risk without preventing the action`() = run("combat")

    @Test fun `civilian capture outcomes agree with map metadata and execution`() = run("capture")

    @Test fun `lethal exchanges show possible survivors and receipts match preview bounds`() = run("lethal")

    @Test fun `movement consequences match native movement attacks and settlement`() = run("movement")

    @Test fun `endpoint context cannot reveal hidden enemies`() = run("privacy")

    @Test fun `civilian capture risk requires confirmation while escorts allow movement`() = run("civilian")

    @Test fun `settlers expose protection rendezvous and unknown surroundings before moving`() = run("protection")

    @Test fun `barbarian sightings remember only observations across restart and rewind`() = run("sightings")

    @Test fun `promotion comparisons preserve native costs healing and long term experience limits`() = run("promotions")

    private fun run(case: String) {
        val root = Files.createTempDirectory("tactical-consequences-$case-")
        try {
            val log = root.resolve("probe.log")
            val process = ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-Xmx1g", "-Djava.awt.headless=true",
                "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}", "-cp", System.getProperty("uncivai.testClasspath"),
                "uncivai.TacticalConsequencesProbe", root.toString(), case,
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
