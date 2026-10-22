package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class RequestResolutionIntegrationTest {
    @Test
    fun `request resolution preserves committed receipts cancels abandoned identities and survives restart`() {
        val root = Files.createTempDirectory("request-resolution-")
        val log = root.resolve("probe.log")
        val process = ProcessBuilder(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(),
            "-Xmx1g",
            "-Djava.awt.headless=true",
            "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
            "-cp",
            System.getProperty("uncivai.testClasspath"),
            "uncivai.RequestResolutionProbe",
            root.toString(),
        ).redirectErrorStream(true).redirectOutput(log.toFile()).start()
        try {
            check(process.waitFor(120, TimeUnit.SECONDS)) { "Resolution probe timed out: ${Files.readString(log)}" }
            assertEquals(Files.readString(log), 0, process.exitValue())
        } finally {
            if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
