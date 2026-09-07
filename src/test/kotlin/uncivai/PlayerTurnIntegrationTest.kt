package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class PlayerTurnIntegrationTest {
    @Test
    fun `turn commands preserve authority privacy and retries and advance both rulesets`() {
        val root = Files.createTempDirectory("player-turn-")
        try {
            runChild(root, "uncivai.S01ScenarioGenerator", root.resolve("scenarios").toString())
            for (ruleset in listOf("vanilla", "gods-and-kings")) {
                runChild(
                    root,
                    "uncivai.PlayerTurnProbe",
                    root.resolve("scenarios/$ruleset.json").toString(),
                    root.resolve(ruleset).toString(),
                )
                runChild(
                    root,
                    "uncivai.CheckpointProbe",
                    root.resolve("scenarios/$ruleset.json").toString(),
                    root.resolve("checkpoint-$ruleset").toString(),
                )
                for (boundary in listOf("before", "after")) {
                    val save = root.resolve("scenarios/$ruleset.json").toString()
                    val crashRoot = root.resolve("crash-$ruleset-$boundary").toString()
                    runChild(root, "uncivai.CheckpointCrashProbe", "initialize", save, crashRoot)
                    runChild(root, "uncivai.CheckpointCrashProbe", "crash-$boundary", save, crashRoot, expectedExit = 73)
                    runChild(root, "uncivai.CheckpointCrashProbe", "verify-$boundary", save, crashRoot)
                }
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    private fun runChild(root: Path, main: String, vararg args: String, expectedExit: Int = 0) {
        val output = Files.createTempFile(root, "process-", ".log")
        val process = ProcessBuilder(
            listOf(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-Xmx1g",
                "-Djava.awt.headless=true",
                "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
                "-cp",
                System.getProperty("uncivai.testClasspath"),
                main,
            ) + args,
        ).directory(root.toFile()).redirectErrorStream(true).redirectOutput(output.toFile())
            .apply {
                environment().remove("DISPLAY")
                environment().remove("WAYLAND_DISPLAY")
            }.start()
        try {
            check(process.waitFor(90, TimeUnit.SECONDS)) { "$main timed out: ${Files.readString(output)}" }
            assertEquals(Files.readString(output), expectedExit, process.exitValue())
        } finally {
            if (process.isAlive) {
                process.destroyForcibly()
                process.waitFor(5, TimeUnit.SECONDS)
            }
        }
    }
}
