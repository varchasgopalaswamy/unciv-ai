package uncivai

import org.junit.AfterClass
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class S05FreeGreatPersonIntegrationTest {
    @Test
    fun `free great person decisions are private authorized and retry safe in real saved games`() {
        val result = runChild(
            mainClass = "uncivai.S05FreeGreatPersonProbe",
            arguments = listOf(
                scenarioDirectory.toString(),
                testRoot.resolve("runtime-data").toString(),
            ),
            timeoutSeconds = 240,
        )

        assertEquals(result.output, 0, result.exitCode)
        assertTrue(
            result.output,
            result.output.lineSequence().any { it == "S05-VERIFIED" },
        )
    }

    private data class ChildResult(val exitCode: Int, val output: String)

    companion object {
        private lateinit var testRoot: Path
        private lateinit var scenarioDirectory: Path
        private lateinit var assetsDirectory: Path
        private lateinit var testClasspath: String

        @BeforeClass
        @JvmStatic
        fun generateScenarios() {
            assetsDirectory = Path.of(
                requireNotNull(System.getProperty("uncivai.assetsDir")) {
                    "Gradle must provide uncivai.assetsDir"
                },
            ).toAbsolutePath().normalize()
            testClasspath = requireNotNull(System.getProperty("uncivai.testClasspath")) {
                "Gradle must provide uncivai.testClasspath"
            }
            testRoot = Files.createTempDirectory("uncivai-s05-").toAbsolutePath()
            scenarioDirectory = Files.createDirectories(testRoot.resolve("scenarios"))

            val result = runChild(
                mainClass = "uncivai.S04UiScenarioGenerator",
                arguments = listOf(scenarioDirectory.toString()),
                timeoutSeconds = 90,
            )
            assertEquals(result.output, 0, result.exitCode)
            for (
            fileName in listOf(
                "ordinary-vanilla.json",
                "ordinary.json",
                "maya.json",
                "blocked-placement.json",
                "no-city.json",
                "empty-maya-pool.json",
            )
            ) {
                assertTrue(fileName, Files.isRegularFile(scenarioDirectory.resolve(fileName)))
            }
        }

        @AfterClass
        @JvmStatic
        fun removeScenarios() {
            if (::testRoot.isInitialized) deleteTreeWithoutFollowingLinks(testRoot)
        }

        private fun runChild(
            mainClass: String,
            arguments: List<String>,
            timeoutSeconds: Long,
        ): ChildResult {
            val outputFile = Files.createTempFile(testRoot, "child-", ".log")
            val java = Path.of(System.getProperty("java.home"), "bin", "java").toString()
            val command = buildList {
                add(java)
                add("-Xmx1g")
                add("-Djava.awt.headless=true")
                add("-Duncivai.assetsDir=$assetsDirectory")
                add("-cp")
                add(testClasspath)
                add(mainClass)
                addAll(arguments)
            }
            val process = ProcessBuilder(command)
                .directory(testRoot.toFile())
                .redirectErrorStream(true)
                .redirectOutput(outputFile.toFile())
                .apply {
                    environment().remove("DISPLAY")
                    environment().remove("WAYLAND_DISPLAY")
                }
                .start()

            if (!process.waitFor(timeoutSeconds, TimeUnit.SECONDS)) {
                process.destroyForcibly()
                process.waitFor(5, TimeUnit.SECONDS)
                val output = Files.readString(outputFile)
                throw AssertionError("$mainClass did not exit within ${timeoutSeconds}s:\n$output")
            }
            return ChildResult(process.exitValue(), Files.readString(outputFile))
        }
    }
}
