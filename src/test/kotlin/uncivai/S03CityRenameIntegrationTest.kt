package uncivai

import org.junit.AfterClass
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class S03CityRenameIntegrationTest {
    @Test
    fun `owned city rename is authorized serialized and retry safe in both bundled rulesets`() {
        for (fileName in listOf("vanilla.json", "gods-and-kings.json")) {
            val result = runChild(
                mainClass = "uncivai.S03CityRenameProbe",
                arguments = listOf(
                    scenarioDirectory.resolve(fileName).toString(),
                    testRoot.resolve("runtime-$fileName").toString(),
                ),
                timeoutSeconds = 120,
            )

            assertEquals(result.output, 0, result.exitCode)
            assertTrue(
                result.output,
                result.output.lineSequence().any { it == "S03-VERIFIED:$fileName" },
            )
        }
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
            testRoot = Files.createTempDirectory("uncivai-s03-").toAbsolutePath()
            scenarioDirectory = Files.createDirectories(testRoot.resolve("scenarios"))

            val result = runChild(
                mainClass = "uncivai.S01ScenarioGenerator",
                arguments = listOf(scenarioDirectory.toString()),
                timeoutSeconds = 60,
            )
            assertEquals(result.output, 0, result.exitCode)
            assertTrue(Files.isRegularFile(scenarioDirectory.resolve("vanilla.json")))
            assertTrue(Files.isRegularFile(scenarioDirectory.resolve("gods-and-kings.json")))
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
