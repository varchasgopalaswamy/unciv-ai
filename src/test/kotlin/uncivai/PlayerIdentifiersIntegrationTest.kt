package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class PlayerIdentifiersIntegrationTest {
    @Test
    fun `compact player references survive reconnect and restart without recycling removed objects`() {
        val root = Files.createTempDirectory("player-identifiers-")
        try {
            for (stage in listOf("prepare", "restore")) {
                val log = root.resolve("$stage.log")
                val process = ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-Xmx1g", "-Djava.awt.headless=true",
                    "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
                    "-cp", System.getProperty("uncivai.testClasspath"),
                    "uncivai.PlayerIdentifiersProbe", root.toString(), stage,
                ).directory(root.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
                try {
                    check(process.waitFor(90, TimeUnit.SECONDS)) { "Identifier fixture timed out: ${Files.readString(log)}" }
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
