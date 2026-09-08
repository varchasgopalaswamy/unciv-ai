package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFileAttributeView
import java.nio.file.attribute.PosixFilePermission

/** Trusted configuration only; deliberately has no generated toString containing credentials. */
internal class McpPlayerGrant(val civilizationId: String, val token: String)

internal fun privateConfigurationBytes(path: Path, maxBytes: Int = 65_536): ByteArray {
    require(path.isAbsolute) { "MCP configuration path must be absolute" }
    require(Files.isRegularFile(path)) { "MCP configuration must be a regular file" }
    val posix = Files.getFileAttributeView(path, PosixFileAttributeView::class.java)
    if (posix != null) {
        val permissions = posix.readAttributes().permissions()
        require(permissions.all { it in privateConfigPermissions }) {
            "MCP configuration must have owner-only read/write permissions (chmod 600)"
        }
    }
    val bytes = Files.newInputStream(path).use { it.readNBytes(maxBytes + 1) }
    require(bytes.size <= maxBytes) { if (maxBytes == 65_536) "MCP configuration exceeds 64 KiB" else "Private configuration exceeds size limit" }
    return bytes
}

internal fun loadOperatorConfig(path: Path): String {
    val bytes = privateConfigurationBytes(path)
    return try {
        val root = Json.parseToJsonElement(bytes.decodeToString(throwOnInvalidSequence = true)) as? JsonObject
        require(root != null && root.keys == setOf("token"))
        val token = root["token"] as? JsonPrimitive
        require(token != null && token.isString && tokenPattern.matches(token.content))
        token.content
    } catch (_: Exception) {
        throw IllegalArgumentException("Invalid operator configuration")
    }
}

internal fun loadMcpPlayerConfig(path: Path): List<McpPlayerGrant> {
    val bytes = privateConfigurationBytes(path)

    // Parse errors can quote source values, including a token. Keep their text and causes private.
    val players = try {
        val root = Json.parseToJsonElement(bytes.decodeToString(throwOnInvalidSequence = true)) as? JsonObject
        require(root != null && root.keys == setOf("players"))
        val entries = root["players"] as? JsonArray
        require(entries != null && entries.size in 1..64)
        entries.map { entry ->
            val player = entry as? JsonObject
            require(player != null && player.keys == setOf("civilizationId", "token"))
            val civilizationId = player["civilizationId"] as? JsonPrimitive
            val token = player["token"] as? JsonPrimitive
            require(civilizationId != null && civilizationId.isString && civilizationId.content.isNotBlank())
            require(token != null && token.isString && tokenPattern.matches(token.content))
            McpPlayerGrant(civilizationId.content, token.content)
        }
    } catch (_: Exception) {
        throw IllegalArgumentException("Invalid MCP player configuration")
    }
    require(players.map { it.token }.distinct().size == players.size) {
        "MCP configuration contains duplicate credentials"
    }
    require(players.map { it.civilizationId }.distinct().size == players.size) {
        "MCP configuration contains duplicate players"
    }
    return players
}

private val tokenPattern = Regex("[A-Za-z0-9_-]{43}")
private val privateConfigPermissions = setOf(
    PosixFilePermission.OWNER_READ,
    PosixFilePermission.OWNER_WRITE,
)
