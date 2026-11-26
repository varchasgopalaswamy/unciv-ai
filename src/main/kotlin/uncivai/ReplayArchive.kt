package uncivai

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.nio.file.StandardOpenOption
import java.nio.file.attribute.PosixFilePermissions
import java.security.MessageDigest
import java.util.zip.GZIPInputStream
import java.util.zip.GZIPOutputStream

@Serializable
internal data class ReplayCursor(val sequence: Long, val sha256: String) {
    init {
        require(sequence >= 0 && sha256.matches(Regex("[0-9a-f]{64}")))
    }

    val filename get() = "$sequence-$sha256.json.gz"
}

/** Write immutable frames first. Only the cursor in the atomic game checkpoint commits them. */
internal class ReplayArchive(private val directory: Path) {
    var cursor: ReplayCursor? = null
        private set
    private var previousWorld: JsonObject? = null
    private var epoch: String? = null
    private var previousBuild: JsonElement? = null
    private val build = obj(
        "runtimeImage" to System.getenv("UNCIV_RUNTIME_IMAGE"),
        "engineRevision" to System.getenv("UNCIV_ENGINE_REVISION"),
        "engineHash" to System.getenv("UNCIV_ENGINE_HASH"),
        "hostRevision" to System.getenv("UNCIV_HOST_REVISION"),
    )

    fun restore(checkpoint: GameCheckpoint) {
        val saved = checkpoint.replay
        if (saved == null) {
            cursor = null
            previousWorld = null
            epoch = null
            previousBuild = null
            return
        }
        val frame = GZIPInputStream(Files.newInputStream(directory.resolve(saved.filename))).use { it.readBytes() }
        check(sha256(frame) == saved.sha256) { "Replay frame does not match checkpoint" }
        val header = Json.parseToJsonElement(frame.decodeToString()).jsonObject
        check(header["epoch"] == JsonPrimitive(checkpoint.epoch)) { "Replay epoch does not match checkpoint" }
        cursor = saved
        previousWorld = Json.parseToJsonElement(checkpoint.gameJson).jsonObject
        epoch = checkpoint.epoch
        previousBuild = header["build"]
    }

    fun stage(checkpoint: GameCheckpoint, events: List<JsonObject>): ReplayCursor {
        val world = Json.parseToJsonElement(checkpoint.gameJson).jsonObject
        val previous = cursor
        val sequence = previous?.sequence?.plus(1) ?: 0
        val keyframe = previousWorld == null || epoch != checkpoint.epoch || build != previousBuild || sequence % 128 == 0L
        val changes = if (keyframe) emptyList() else buildList { diff(checkNotNull(previousWorld), world, emptyList(), this) }
        if (!keyframe && changes.isEmpty() && events.isEmpty()) return checkNotNull(previous)
        val frame = obj(
            "version" to 1, "sequence" to sequence, "previous" to previous?.sha256,
            "epoch" to checkpoint.epoch, "handoff" to checkpoint.handoff, "build" to build,
            "world" to world.takeIf { keyframe }, "changes" to JsonArray(changes),
            "worldSha256" to sha256(buildString { appendCanonical(world) }.encodeToByteArray()),
            "events" to JsonArray(events),
        ).toString().encodeToByteArray()
        val next = ReplayCursor(sequence, sha256(frame))
        Files.createDirectories(directory, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rwx------")))
        require(!Files.isSymbolicLink(directory))
        val target = directory.resolve(next.filename)
        val temporary = Files.createTempFile(directory, ".frame-", ".tmp")
        try {
            GZIPOutputStream(Files.newOutputStream(temporary)).use { it.write(frame) }
            FileChannel.open(temporary, StandardOpenOption.WRITE).use { it.force(true) }
            Files.move(temporary, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            FileChannel.open(directory, StandardOpenOption.READ).use { it.force(true) }
            FileChannel.open(directory.parent, StandardOpenOption.READ).use { it.force(true) }
        } finally {
            Files.deleteIfExists(temporary)
        }
        // The caller halts on a failed checkpoint write. Recovery loads the committed cursor.
        cursor = next
        previousWorld = world
        epoch = checkpoint.epoch
        previousBuild = build
        return next
    }

    private fun diff(before: JsonElement, after: JsonElement, path: List<JsonElement>, changes: MutableList<JsonElement>) {
        if (before == after) return
        when {
            before is JsonObject && after is JsonObject -> {
                for (key in before.keys - after.keys) {
                    changes.add(obj("path" to JsonArray(path + JsonPrimitive(key)), "remove" to true))
                }
                for ((key, value) in after) {
                    val child = path + JsonPrimitive(key)
                    val prior = before[key]
                    if (prior == null) changes.add(obj("path" to JsonArray(child), "value" to value))
                    else diff(prior, value, child, changes)
                }
            }
            before is JsonArray && after is JsonArray && before.size == after.size -> {
                for (index in before.indices) diff(before[index], after[index], path + JsonPrimitive(index), changes)
            }
            else -> changes.add(obj("path" to JsonArray(path), "value" to after))
        }
    }

    private fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes).toHexString()
}

private fun StringBuilder.appendCanonical(value: JsonElement) {
    when (value) {
        is JsonObject -> {
            append('{')
            value.keys.sorted().forEachIndexed { index, key ->
                if (index != 0) append(',')
                append(JsonPrimitive(key))
                append(':')
                appendCanonical(value.getValue(key))
            }
            append('}')
        }
        is JsonArray -> {
            append('[')
            value.forEachIndexed { index, item ->
                if (index != 0) append(',')
                appendCanonical(item)
            }
            append(']')
        }
        is JsonNull -> append("null")
        is JsonPrimitive -> append(
            if (value.isString) value.toString()
            else value.content.toBigDecimalOrNull()?.stripTrailingZeros()?.toPlainString() ?: value.content,
        )
    }
}
