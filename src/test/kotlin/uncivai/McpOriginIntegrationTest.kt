package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class McpOriginIntegrationTest {
    @Test
    fun `MCP metadata separates poll agent and coordinator calls without changing tools or legacy totals`() {
        val root = Files.createTempDirectory("mcp-origin-")
        val log = root.resolve("probe.log")
        val process = ProcessBuilder(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(),
            "-Xmx1g",
            "-Djava.awt.headless=true",
            "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
            "-cp",
            System.getProperty("uncivai.testClasspath"),
            "uncivai.McpOriginProbe",
            root.toString(),
        ).redirectErrorStream(true).redirectOutput(log.toFile()).start()
        try {
            check(process.waitFor(120, TimeUnit.SECONDS)) { "MCP origin probe timed out: ${Files.readString(log)}" }
            assertEquals(Files.readString(log), 0, process.exitValue())
        } finally {
            if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
