package uncivai

import com.unciv.json.json
import com.unciv.logic.GameInfo
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import uncivai.player.TurnSessionState
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.channels.FileLock
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.nio.file.StandardOpenOption

/** Trusted full-state artifact. Never serve this file through a player endpoint. */
@Serializable
internal data class GameCheckpoint(
    val formatVersion: Int = 1,
    val epoch: String,
    val paused: Boolean,
    val gameJson: String,
    val players: Map<String, SavedPlayerState>,
    val handoff: Long = 0,
    val scheduled: Boolean = false,
)

@Serializable
internal data class SavedReceipt(val request: TurnCommandRequest, val receipt: TurnCommandReceipt)

@Serializable
internal data class SavedPlayerState(
    val cityHandles: Map<String, String>,
    val alertHandles: List<String?>,
    val revision: String,
    val fingerprint: PlayerTurnSnapshot?,
    val receipts: List<SavedReceipt>,
    val unitHandles: Map<Int, String> = emptyMap(),
)

internal class CheckpointStore(
    val path: Path,
    private val beforeReplace: () -> Unit = {},
    private val afterReplace: () -> Unit = {},
) : AutoCloseable {
    private var lockChannel: FileChannel? = null
    private var lock: FileLock? = null
    init {
        require(path.isAbsolute) { "Checkpoint path must be absolute" }
    }

    fun write(checkpoint: GameCheckpoint) {
        acquire()
        val parent = path.parent
        Files.createDirectories(parent)
        val temporary = Files.createTempFile(parent, ".checkpoint-", ".tmp")
        try {
            val bytes = checkpointJson.encodeToString(checkpoint).toByteArray(Charsets.UTF_8)
            FileChannel.open(temporary, StandardOpenOption.WRITE).use { channel ->
                val buffer = ByteBuffer.wrap(bytes)
                while (buffer.hasRemaining()) channel.write(buffer)
                channel.force(true)
            }
            beforeReplace()
            // Deliberately no non-atomic fallback: a torn checkpoint is not recovery.
            Files.move(temporary, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            FileChannel.open(parent, StandardOpenOption.READ).use { it.force(true) }
            afterReplace()
        } finally {
            Files.deleteIfExists(temporary)
        }
    }

    fun writeInitial(checkpoint: GameCheckpoint) {
        acquire()
        require(!Files.exists(path)) { "Checkpoint already exists; resume it explicitly" }
        write(checkpoint)
    }

    fun read(): GameCheckpoint {
        acquire()
        val saved = checkpointJson.decodeFromString<GameCheckpoint>(Files.readString(path))
        require(saved.formatVersion == 1) { "Unsupported checkpoint version" }
        require(saved.epoch.isNotEmpty()) { "Invalid checkpoint epoch" }
        return saved
    }

    private fun acquire() {
        if (lock != null) return
        Files.createDirectories(path.parent)
        val channel = FileChannel.open(
            path.resolveSibling("${path.fileName}.lock"),
            StandardOpenOption.CREATE,
            StandardOpenOption.WRITE,
        )
        try {
            lock = channel.tryLock() ?: error("Checkpoint is already in use")
            lockChannel = channel
        } catch (failure: Throwable) {
            channel.close()
            throw failure
        }
    }

    override fun close() {
        lockChannel?.close()
        lockChannel = null
        lock = null
    }

    companion object {
        private val checkpointJson = Json { encodeDefaults = true }

        fun capture(game: GameInfo, epoch: String, paused: Boolean, states: Map<String, TurnSessionState>, handoff: Long = 0, scheduled: Boolean = false): GameCheckpoint = GameCheckpoint(
            epoch = epoch,
            paused = paused,
            handoff = handoff,
            scheduled = scheduled,
            gameJson = json().toJson(game),
            players = states.mapValues { (civilization, state) ->
                val player = game.civilizations.single { it.civID == civilization }
                SavedPlayerState(
                    cityHandles = state.cityHandles.toMap(),
                    unitHandles = state.unitHandles.toMap(),
                    alertHandles = player.popupAlerts.map { state.alertHandles[it] },
                    revision = state.revision,
                    fingerprint = state.fingerprint,
                    receipts = state.receipts.values.map { SavedReceipt(it.first, it.second) },
                )
            },
        )
    }
}
