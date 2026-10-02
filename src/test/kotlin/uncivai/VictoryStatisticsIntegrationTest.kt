package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class VictoryStatisticsIntegrationTest {
    @Test fun `rankings and bounded charts match native values and survive restart`() = runProbe("statistics")

    @Test fun `statistics conceal unmet players and obey opponent visibility`() = runProbe("privacy")

    @Test fun `statistics honor tabs and freeze demographics while validating selections`() = runProbe("settings")

    @Test fun `total score is absent from player statistics and cannot be requested`() = runProbe("score")

    private fun runProbe(stage: String) {
        val root = Files.createTempDirectory("victory-statistics-")
        try {
            for (mode in if (stage == "statistics") listOf(stage, "restore") else listOf(stage)) {
                val log = root.resolve("$mode.log")
                val process = ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-Xmx1g", "-Djava.awt.headless=true",
                    "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
                    "-cp", System.getProperty("uncivai.testClasspath"),
                    if (mode == "restore") "uncivai.VictoryProgressProbe" else "uncivai.VictoryStatisticsProbe", root.toString(), mode,
                ).redirectErrorStream(true).redirectOutput(log.toFile()).start()
                try {
                    check(process.waitFor(90, TimeUnit.SECONDS)) { "Timed out: ${Files.readString(log)}" }
                    assertEquals(Files.readString(log), 0, process.exitValue())
                } finally {
                    if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
                }
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
