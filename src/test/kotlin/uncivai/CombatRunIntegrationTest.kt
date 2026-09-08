package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class CombatRunIntegrationTest {
    @Test
    fun `HTTPS combat heals promotes negotiates peace and survives rollback retries and two restarts`() = stages("combat", "restore", "verify")

    @Test
    fun `HTTPS city captures require disposition and deliver both final results across restart`() = stages("conquest", "final")

    @Test
    fun `HTTPS attacking or moving onto the final settler commits victory without a city decision`() {
        stages("last-settler-attack", "final")
        stages("last-settler-move", "final")
    }

    @Test
    fun `loading a won engine save records its missing victory before player queries`() = stages("last-settler-move", "unrecorded-final")

    private fun stages(vararg stages: String) {
        val root = Files.createTempDirectory("combat-run-")
        try {
            for (stage in stages) {
                val log = root.resolve("$stage.log")
                val process = ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-Xmx1g", "-Djava.awt.headless=true", "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
                    "-cp", System.getProperty("uncivai.testClasspath"), "uncivai.CombatRunProbe", root.toString(), stage,
                )
                    .directory(root.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
                try {
                    check(process.waitFor(180, TimeUnit.SECONDS)) { "Combat stage $stage timed out: ${Files.readString(log)}" }
                    assertEquals("$stage: ${Files.readString(log)}", 0, process.exitValue())
                } finally {
                    if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
                }
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
