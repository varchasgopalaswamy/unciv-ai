package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class TurnReviewIntegrationTest {
    @Test
    fun `turn review requires observation and explicit deferral and survives restart without replaying actions`() {
        val root = Files.createTempDirectory("turn-review-")
        try {
            for (stage in listOf("prepare", "complete", "verify")) {
                val log = root.resolve("$stage.log")
                val process = ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-Xmx1g",
                    "-Djava.awt.headless=true",
                    "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
                    "-cp", System.getProperty("uncivai.testClasspath"),
                    "uncivai.TurnReviewProbe", root.toString(), stage,
                ).redirectErrorStream(true).redirectOutput(log.toFile()).start()
                try {
                    check(process.waitFor(60, TimeUnit.SECONDS)) { "$stage timed out: ${Files.readString(log)}" }
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
