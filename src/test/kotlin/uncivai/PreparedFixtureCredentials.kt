package uncivai

import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.file.FileAlreadyExistsException
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardOpenOption.CREATE_NEW
import java.nio.file.StandardOpenOption.WRITE
import java.nio.file.attribute.PosixFileAttributeView
import java.nio.file.attribute.PosixFilePermissions
import java.security.SecureRandom
import java.util.Base64

/** Interactive fixtures need their own credentials before launch_host can resume their checkpoint. */
internal fun prepareDuelCredentials(root: Path) {
    Files.createDirectories(root)
    val path = root.resolve("players.json")
    val random = SecureRandom()
    val tokens = generateSequence {
        Base64.getUrlEncoder().withoutPadding().encodeToString(ByteArray(32).also(random::nextBytes))
    }.distinct().take(2).toList()
    val players = listOf("Rome", "Greece")
    val content = buildJsonObject {
        putJsonArray("players") {
            players.zip(tokens).forEach { (civilization, token) ->
                addJsonObject {
                    put("civilizationId", civilization)
                    put("token", token)
                }
            }
        }
    }.toString() + "\n"
    val attributes = if (Files.getFileAttributeView(root, PosixFileAttributeView::class.java) != null) {
        arrayOf(PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rw-------")))
    } else {
        emptyArray()
    }
    try {
        FileChannel.open(path, setOf(CREATE_NEW, WRITE), *attributes).use { file ->
            val bytes = ByteBuffer.wrap(content.toByteArray(Charsets.UTF_8))
            while (bytes.hasRemaining()) file.write(bytes)
            file.force(true)
        }
    } catch (_: FileAlreadyExistsException) {
        // Keep credentials from an earlier preparation attempt; never rotate them.
    }
    require(!Files.isSymbolicLink(path)) { "Fixture credentials must not be a symbolic link" }
    require(loadMcpPlayerConfig(path).map { it.civilizationId }.toSet() == players.toSet()) {
        "Fixture credentials must bind Rome and Greece"
    }
}
