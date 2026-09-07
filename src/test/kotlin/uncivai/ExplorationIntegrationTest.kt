package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class ExplorationIntegrationTest {
    @Test
    fun `settlement survives abrupt process termination before and after checkpoint replacement`() {
        val root = Files.createTempDirectory("settlement-crash-")
        try {
            for (boundary in listOf("before", "after")) {
                val directory = Files.createDirectories(root.resolve(boundary)).toString()
                child(root, "uncivai.SettlementCrashProbe", "initialize", directory)
                child(root, "uncivai.SettlementCrashProbe", "crash-$boundary", directory, expectedExit = 73)
                child(root, "uncivai.SettlementCrashProbe", "verify-$boundary", directory)
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    @Test
    fun `fresh duel supports private exploration settlement turn handoff and recovery through MCP`() {
        val root = Files.createTempDirectory("exploration-")
        try {
            child(root, "uncivai.MainKt", "--new-duel", "--seed", "10001", "--data-dir", root.resolve("data").toString(), "--checkpoint", root.resolve("game.json").toString())
            child(root, "uncivai.ExplorationProbe", root.toString())
            child(root, "uncivai.ExplorationRecoveryProbe", root.toString())
            child(root, "uncivai.MainKt", "--new-duel", "--data-dir", root.resolve("data").toString(), "--checkpoint", root.resolve("game.json").toString(), expectedExit = 2)
            child(root, "uncivai.MainKt", "--new-duel", "--data-dir", root.resolve("data").toString(), expectedExit = 2)
            child(root, "uncivai.MainKt", "--new-duel", "--save", root.resolve("game.json").toString(), "--data-dir", root.resolve("data").toString(), expectedExit = 2)
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    private fun child(root: Path, main: String, vararg args: String, expectedExit: Int = 0) {
        val log = Files.createTempFile(root, "child-", ".log")
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
        ).directory(root.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
        try {
            check(process.waitFor(90, TimeUnit.SECONDS)) { "Timed out: ${Files.readString(log)}" }
            assertEquals(Files.readString(log), expectedExit, process.exitValue())
        } finally {
            if (process.isAlive) {
                process.destroyForcibly()
                process.waitFor(5, TimeUnit.SECONDS)
            }
        }
    }
}
