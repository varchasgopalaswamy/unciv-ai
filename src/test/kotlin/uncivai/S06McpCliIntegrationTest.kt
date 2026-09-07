package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFileAttributeView
import java.nio.file.attribute.PosixFilePermissions
import java.security.SecureRandom
import java.time.Duration
import java.util.Base64
import java.util.concurrent.TimeUnit

class S06McpCliIntegrationTest {
    @Test
    fun `serve CLI provisions two clients reports readiness and stops on termination`() = withFixture { root, save ->
        val originalSave = Files.readAllBytes(save)
        val romeToken = token()
        val greeceToken = token()
        val config = configuration(root, "Rome" to romeToken, "Greece" to greeceToken)
        val configBytes = Files.readAllBytes(config)
        val child = startChild(
            root,
            "uncivai.MainKt",
            listOf(
                "--save",
                save.toString(),
                "--data-dir",
                root.resolve("runtime").toString(),
                "--mcp-config",
                config.toString(),
                "--port",
                "0",
            ),
        )
        try {
            val endpoint = awaitReady(child)
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build().use { client ->
                for ((name, credential) in listOf("Rome" to romeToken, "Greece" to greeceToken)) {
                    val initialized = rpc(client, endpoint, credential, initialize)
                    assertEquals(200, initialized.statusCode())
                    val response = rpc(client, endpoint, credential, summary)
                    assertEquals(200, response.statusCode())
                    val result = Json.parseToJsonElement(response.body()).jsonObject.getValue("result").jsonObject
                    assertEquals(
                        name,
                        result.getValue("structuredContent").jsonObject.getValue("civilization").jsonPrimitive.content,
                    )
                }
            }
            child.process.destroy()
            assertTrue("Serve CLI did not stop after termination", child.process.waitFor(10, TimeUnit.SECONDS))
            assertTrue(originalSave.contentEquals(Files.readAllBytes(save)))
            assertTrue(configBytes.contentEquals(Files.readAllBytes(config)))
            val output = Files.readString(child.log)
            assertFalse(output.contains(romeToken))
            assertFalse(output.contains(greeceToken))
            assertFalse(output.contains("Exception in thread"))
        } finally {
            stopChild(child)
        }
    }

    @Test
    fun `invalid provisioning exits before serving and ordinary CLI still exits normally`() = withFixture { root, save ->
        val credential = token()
        val config = configuration(root, "Unknown player" to credential)
        val invalid = startChild(
            root,
            "uncivai.MainKt",
            listOf(
                "--save",
                save.toString(),
                "--data-dir",
                root.resolve("invalid-runtime").toString(),
                "--mcp-config",
                config.toString(),
                "--port",
                "0",
            ),
        )
        try {
            assertTrue(invalid.process.waitFor(15, TimeUnit.SECONDS))
            assertEquals(2, invalid.process.exitValue())
            val output = Files.readString(invalid.log)
            assertFalse(output.contains("MCP-READY:"))
            assertFalse(output.contains(credential))
            assertTrue(output.contains("Player is unavailable"))
        } finally {
            stopChild(invalid)
        }

        val ordinary = startChild(
            root,
            "uncivai.MainKt",
            listOf("--save", save.toString(), "--data-dir", root.resolve("ordinary-runtime").toString()),
        )
        try {
            assertTrue(ordinary.process.waitFor(15, TimeUnit.SECONDS))
            assertEquals(0, ordinary.process.exitValue())
            val output = Files.readString(ordinary.log)
            assertTrue(output.contains("Loaded game "))
            assertFalse(output.contains("MCP-READY:"))
        } finally {
            stopChild(ordinary)
        }
    }

    private fun rpc(client: HttpClient, endpoint: URI, credential: String, body: JsonObject): HttpResponse<String> = client.send(
        HttpRequest.newBuilder(endpoint)
            .timeout(Duration.ofSeconds(5))
            .header("Authorization", "Bearer $credential")
            .header("Content-Type", "application/json")
            .header("Accept", "application/json, text/event-stream")
            .header("MCP-Protocol-Version", "2025-11-25")
            .POST(HttpRequest.BodyPublishers.ofString(body.toString()))
            .build(),
        HttpResponse.BodyHandlers.ofString(),
    )

    private fun awaitReady(child: Child): URI {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20)
        while (System.nanoTime() < deadline) {
            val line = Files.readString(child.log).lineSequence().firstOrNull { it.startsWith("MCP-READY:") }
            if (line != null) return URI.create(line.removePrefix("MCP-READY:"))
            check(child.process.isAlive) { "Serve CLI exited before readiness: ${Files.readString(child.log)}" }
            Thread.sleep(25)
        }
        error("Serve CLI did not become ready: ${Files.readString(child.log)}")
    }

    private fun configuration(root: Path, vararg players: Pair<String, String>): Path {
        val file = Files.createTempFile(root, "players-", ".json")
        if (Files.getFileAttributeView(file, PosixFileAttributeView::class.java) != null) {
            Files.setPosixFilePermissions(file, PosixFilePermissions.fromString("rw-------"))
        }
        Files.writeString(
            file,
            buildJsonObject {
                put(
                    "players",
                    buildJsonArray {
                        for ((civilizationId, credential) in players) {
                            add(
                                buildJsonObject {
                                    put("civilizationId", civilizationId)
                                    put("token", credential)
                                },
                            )
                        }
                    },
                )
            }.toString(),
        )
        return file
    }

    private fun withFixture(test: (Path, Path) -> Unit) {
        val root = Files.createTempDirectory("uncivai-s06-cli-")
        try {
            val scenarios = Files.createDirectories(root.resolve("scenarios"))
            val generator = startChild(root, "uncivai.S01ScenarioGenerator", listOf(scenarios.toString()))
            try {
                assertTrue(generator.process.waitFor(60, TimeUnit.SECONDS))
                assertEquals(Files.readString(generator.log), 0, generator.process.exitValue())
            } finally {
                stopChild(generator)
            }
            test(root, scenarios.resolve("vanilla.json"))
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    private fun startChild(root: Path, mainClass: String, arguments: List<String>): Child {
        val log = Files.createTempFile(root, "child-", ".log")
        val command = listOf(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(),
            "-Xmx1g",
            "-Djava.awt.headless=true",
            "-Duncivai.assetsDir=${requireNotNull(System.getProperty("uncivai.assetsDir"))}",
            "-cp",
            requireNotNull(System.getProperty("uncivai.testClasspath")),
            mainClass,
        ) + arguments
        val process = ProcessBuilder(command)
            .directory(root.toFile())
            .redirectErrorStream(true)
            .redirectOutput(log.toFile())
            .apply {
                environment().remove("DISPLAY")
                environment().remove("WAYLAND_DISPLAY")
            }
            .start()
        return Child(process, log)
    }

    private fun stopChild(child: Child) {
        if (child.process.isAlive) {
            child.process.destroyForcibly()
            check(child.process.waitFor(5, TimeUnit.SECONDS)) { "Child process did not stop" }
        }
    }

    private fun token(): String = ByteArray(32).also { SecureRandom().nextBytes(it) }
        .let { Base64.getUrlEncoder().withoutPadding().encodeToString(it) }

    private data class Child(val process: Process, val log: Path)

    companion object {
        private val initialize = Json.parseToJsonElement(
            """{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"cli-test","version":"1"}}}""",
        ).jsonObject
        private val summary = Json.parseToJsonElement(
            """{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"player_summary","arguments":{}}}""",
        ).jsonObject
    }
}
