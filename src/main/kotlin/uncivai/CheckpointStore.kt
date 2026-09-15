package uncivai

import com.unciv.json.json
import com.unciv.logic.GameInfo
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.CombatReport
import uncivai.player.CompletedTurnReview
import uncivai.player.ConfirmedTurnAction
import uncivai.player.PlayerIdentifier
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.SharedFreeGreatPersonDecision
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
    val formatVersion: Int = 4,
    val epoch: String,
    val paused: Boolean,
    val gameJson: String,
    val players: Map<String, SavedPlayerState>,
    val handoff: Long = 0,
    val scheduled: Boolean = false,
    val turnStart: TurnStartCheckpoint? = null,
    val communication: CommunicationState = CommunicationState(),
    val communicationPaused: Boolean = false,
)

/** The pre-activation state, persisted atomically alongside every command checkpoint. */
@Serializable
internal data class TurnStartCheckpoint(
    val handoff: Long,
    val civilization: String,
    val turn: Int,
    val gameJson: String,
    val players: Map<String, SavedPlayerState>,
    val activationId: String? = null,
    val rolledBackActivationIds: Set<String> = emptySet(),
)

@Serializable
internal data class SavedReceipt(val request: TurnCommandRequest, val receipt: TurnCommandReceipt)

@Serializable
internal data class SavedPlayerState(
    val identifiers: Map<PlayerIdentifier, Long>,
    val cityHandles: Map<String, String>,
    val alertHandles: List<String?>,
    val notificationHandles: List<String?>,
    val dismissedNotifications: Set<String>,
    val revision: String,
    val fingerprint: PlayerTurnSnapshot?,
    val receipts: List<SavedReceipt>,
    val unitHandles: Map<Int, String> = emptyMap(),
    val reviewTurn: Int? = null,
    val reviewActions: List<ConfirmedTurnAction> = emptyList(),
    val previousTurnReview: CompletedTurnReview? = null,
    val combatReports: Map<String, CombatReport> = emptyMap(),
    val deliveredCombatReports: Int = 0,
    val freeGreatPersonDecision: SharedFreeGreatPersonDecision? = null,
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
        val document = checkpointJson.parseToJsonElement(Files.readString(path))
        require(document.jsonObject["formatVersion"]?.jsonPrimitive?.intOrNull == 4) { "Unsupported checkpoint version; start a new game" }
        val saved = checkpointJson.decodeFromJsonElement<GameCheckpoint>(document)
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

        fun capture(game: GameInfo, epoch: String, paused: Boolean, states: Map<String, TurnSessionState>, handoff: Long = 0, scheduled: Boolean = false, turnStart: TurnStartCheckpoint? = null, communication: CommunicationState = CommunicationState(), communicationPaused: Boolean = false): GameCheckpoint = GameCheckpoint(
            epoch = epoch,
            paused = paused,
            handoff = handoff,
            scheduled = scheduled,
            turnStart = turnStart,
            communication = communication,
            communicationPaused = communicationPaused,
            gameJson = json().toJson(game),
            players = states.mapValues { (civilization, state) ->
                val player = game.civilizations.single { it.civID == civilization }
                SavedPlayerState(
                    identifiers = state.identifiers.snapshot(),
                    cityHandles = state.cityHandles.toMap(),
                    unitHandles = state.unitHandles.toMap(),
                    alertHandles = player.popupAlerts.map { state.alertHandles[it] },
                    notificationHandles = player.notifications.map { state.notificationHandles[it] },
                    dismissedNotifications = state.dismissedNotifications.toSet(),
                    revision = state.revision,
                    fingerprint = state.fingerprint,
                    receipts = state.receipts.values.map { SavedReceipt(it.first, it.second) },
                    reviewTurn = state.reviewTurn,
                    reviewActions = state.reviewActions.toList(),
                    previousTurnReview = state.previousTurnReview,
                    combatReports = state.combatReports.toMap(),
                    deliveredCombatReports = state.deliveredCombatReports,
                    freeGreatPersonDecision = state.freeGreatPersonDecision,
                )
            },
        )
    }
}
