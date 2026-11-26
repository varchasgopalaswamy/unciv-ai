package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class ManagerScenarioIntegrationTest {
    @Test fun `controlled economy reserves funds rejects spending and permits purchase after release`() = run("economy-manager")

    @Test fun `controlled war exposes nearby armies an escort and native peace eligibility`() = run("military-manager")

    private fun run(variant: String) {
        val root = Files.createTempDirectory("manager-exercise-")
        val log = root.resolve("probe.log")
        try {
            val process = ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-Xmx1g", "-Djava.awt.headless=true", "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
                "-cp", System.getProperty("uncivai.testClasspath"), "uncivai.ManagerScenarioProbe", root.toString(), variant,
            )
                .directory(root.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
            try {
                check(process.waitFor(120, TimeUnit.SECONDS)) { "Manager exercise timed out: ${Files.readString(log)}" }
                assertEquals(Files.readString(log), 0, process.exitValue())
            } finally {
                if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
