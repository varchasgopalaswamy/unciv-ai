package uncivai

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFileAttributeView
import java.nio.file.attribute.PosixFilePermissions

class McpPlayerConfigTest {
    @Test
    fun `operator configuration has a strict separate schema and sanitized failures`() = withConfig { file ->
        Files.writeString(file, """{"token":"$tokenA"}""")
        assertEquals(tokenA, loadOperatorConfig(file))
        for (source in listOf(configuration(player("Rome", tokenA)), """{"token":"$tokenA","civilizationId":"Rome"}""", """{"token":"$tokenA"""", """{"token":123}""")) {
            Files.writeString(file, source)
            val failure = runCatching { loadOperatorConfig(file) }.exceptionOrNull()
            assertEquals("Invalid operator configuration", failure?.message)
            assertNull(failure?.cause)
        }
    }

    @Test
    fun `private configuration binds distinct credentials without exposing them in toString`() = withConfig { file ->
        Files.writeString(file, configuration(player("Rome", tokenA), player("Greece", tokenB)))
        val grants = loadMcpPlayerConfig(file)

        assertEquals(listOf("Rome", "Greece"), grants.map { it.civilizationId })
        assertEquals(listOf(tokenA, tokenB), grants.map { it.token })
        assertFalse(grants.toString().contains(tokenA))
        assertFalse(grants.toString().contains(tokenB))
    }

    @Test
    fun `invalid configuration cannot quote credentials or silently change the binding schema`() = withConfig { file ->
        val invalidConfigurations = listOf(
            """{"players":[{"civilizationId":"Rome","token":"$tokenA"""",
            """{"players":[],"token":"$tokenA"}""",
            configuration(),
            configuration(player("Rome", tokenA), player("Greece", tokenA)),
            configuration(player("Rome", tokenA), player("Rome", tokenB)),
            configuration("""{"civilizationId":"Rome","token":123}"""),
            configuration("""{"civilizationId":123,"token":"$tokenA"}"""),
            configuration("""{"civilizationId":null,"token":"$tokenA"}"""),
            configuration("""{"civilizationId":"Rome","token":"$tokenA","viewer":"Greece"}"""),
            configuration(player("Rome", "short")),
            configuration(player("Rome", "!".repeat(43))),
        )
        for (source in invalidConfigurations) {
            Files.writeString(file, source)
            val failure = runCatching { loadMcpPlayerConfig(file) }.exceptionOrNull()
            assertTrue("Invalid configuration was accepted", failure is IllegalArgumentException)
            assertNull(failure!!.cause)
            assertFalse(failure.message.orEmpty().contains(tokenA))
            assertFalse(failure.message.orEmpty().contains(tokenB))
        }
    }

    @Test
    fun `configuration reads are bounded and require a private absolute file`() = withConfig { file ->
        Files.writeString(file, configuration(player("Rome", tokenA)))
        assertTrue(runCatching { loadMcpPlayerConfig(Path.of("players.json")) }.isFailure)
        if (Files.getFileAttributeView(file, PosixFileAttributeView::class.java) != null) {
            Files.setPosixFilePermissions(file, PosixFilePermissions.fromString("rw-r--r--"))
            val failure = runCatching { loadMcpPlayerConfig(file) }.exceptionOrNull()
            assertTrue(failure is IllegalArgumentException)
            assertTrue(failure!!.message.orEmpty().contains("owner-only"))
            Files.setPosixFilePermissions(file, PosixFilePermissions.fromString("rw-------"))
        }
        Files.writeString(file, " ".repeat(65_537))
        val oversized = runCatching { loadMcpPlayerConfig(file) }.exceptionOrNull()
        assertEquals("MCP configuration exceeds 64 KiB", oversized?.message)
        Files.write(file, byteArrayOf(0xc3.toByte(), 0x28))
        val invalidEncoding = runCatching { loadMcpPlayerConfig(file) }.exceptionOrNull()
        assertEquals("Invalid MCP player configuration", invalidEncoding?.message)
    }

    private fun withConfig(test: (Path) -> Unit) {
        val root = Files.createTempDirectory("uncivai-mcp-config-")
        try {
            val file = Files.createFile(root.resolve("players.json"))
            if (Files.getFileAttributeView(file, PosixFileAttributeView::class.java) != null) {
                Files.setPosixFilePermissions(file, PosixFilePermissions.fromString("rw-------"))
            }
            test(file)
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    private fun player(name: String, token: String) = """{"civilizationId":"$name","token":"$token"}"""
    private fun configuration(vararg players: String) = """{"players":[${players.joinToString(",") }]}"""

    companion object {
        private val tokenA = "A".repeat(43)
        private val tokenB = "B".repeat(43)
    }
}
