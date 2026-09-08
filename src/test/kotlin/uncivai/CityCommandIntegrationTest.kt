package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class CityCommandIntegrationTest {
    @Test
    fun `city commands preserve ownership validation retry receipts and recovery`() {
        val directory = Files.createTempDirectory("city-commands-")
        try {
            for (mode in listOf("execute", "recover")) {
                val log = directory.resolve("$mode.log")
                val process = ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-Xmx1g", "-Djava.awt.headless=true",
                    "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
                    "-cp", System.getProperty("uncivai.testClasspath"),
                    "uncivai.CityCommandProbe", mode, directory.toString(),
                ).directory(directory.toFile()).redirectErrorStream(true).redirectOutput(log.toFile()).start()
                try {
                    check(process.waitFor(90, TimeUnit.SECONDS)) { "Timed out: ${Files.readString(log)}" }
                    assertEquals(Files.readString(log), 0, process.exitValue())
                } finally {
                    if (process.isAlive) {
                        process.destroyForcibly()
                        process.waitFor(5, TimeUnit.SECONDS)
                    }
                }
            }
        } finally {
            deleteTreeWithoutFollowingLinks(directory)
        }
    }
}
