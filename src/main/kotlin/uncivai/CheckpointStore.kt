package uncivai

import com.unciv.json.json
import com.unciv.logic.GameInfo
import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.CombatReport
import uncivai.player.CompletedTurnReview
import uncivai.player.ConfirmedTurnAction
import uncivai.player.PlayerIdentifier
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
    val formatVersion: Int = 9,
    val epoch: String,
    val paused: Boolean,
    val gameJson: String,
    val players: Map<String, SavedPlayerState>,
    val handoff: Long = 0,
    val scheduled: Boolean = false,
    val communication: CommunicationState = CommunicationState(),
    val communicationPaused: Boolean = false,
    val accountingId: String? = null,
    val accountingOutbox: List<AccountingBatch> = emptyList(),
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    val replay: ReplayCursor? = null,
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
    val receipts: List<SavedReceipt>,
    val unitHandles: Map<Int, String> = emptyMap(),
    val reviewTurn: Int? = null,
    val reviewActions: List<ConfirmedTurnAction> = emptyList(),
    val previousTurnReview: CompletedTurnReview? = null,
    val combatReports: Map<String, CombatReport> = emptyMap(),
    val deliveredCombatReports: Int = 0,
    val freeGreatPersonDecision: SharedFreeGreatPersonDecision? = null,
    val constructionSelections: Map<String, List<uncivai.player.ConstructionSelection>> = emptyMap(),
    val constructionQueues: Map<String, uncivai.player.ConstructionQueueState> = emptyMap(),
    val settlementKnowledge: List<uncivai.player.KnownSettlementTile> = emptyList(),
    val settlementTechnologies: Set<String>,
    val settlerHistory: Map<Int, uncivai.player.SettlerHistory> = emptyMap(),
    val barbarianSightings: Map<Int, uncivai.player.BarbarianSighting>,
)

internal class CheckpointStore(
    val path: Path,
    private val beforeReplace: () -> Unit = {},
    private val afterReplace: () -> Unit = {},
    internal val recordReplay: Boolean = false,
) : AutoCloseable {
    private var lockChannel: FileChannel? = null
    private var lock: FileLock? = null
    internal var accounting: HostAccountingStore? = null
        private set
    internal val accountingPath get() = path.resolveSibling("${path.fileName}.accounting.sqlite3")
    private val replayPath get() = path.resolveSibling("${path.fileName}.replay")
    internal var replayArchive: ReplayArchive? = null
        private set
    internal var writes = 0L
        private set
    internal var bytesWritten = 0L
        private set
    init {
        require(path.isAbsolute) { "Checkpoint path must be absolute" }
    }

    fun write(checkpoint: GameCheckpoint, replayEvents: List<JsonObject> = emptyList()) {
        acquire()
        val ledger = checkNotNull(accounting) { "Initialize accounting before writing a checkpoint" }
        ledger.checkHealthy()
        val parent = path.parent
        Files.createDirectories(parent)
        val temporary = Files.createTempFile(parent, ".checkpoint-", ".tmp")
        try {
            val cursor = replayArchive?.stage(checkpoint, replayEvents) ?: checkpoint.replay
            val persisted = checkpoint.copy(formatVersion = 9, communication = checkpoint.communication.withoutAccounting(), accountingId = ledger.id, accountingOutbox = ledger.outbox(), replay = cursor)
            val bytes = HostTimings.measure("checkpoint.serialize") { checkpointJson.encodeToString(persisted).toByteArray(Charsets.UTF_8) }
            FileChannel.open(temporary, StandardOpenOption.WRITE).use { channel ->
                val buffer = ByteBuffer.wrap(bytes)
                while (buffer.hasRemaining()) channel.write(buffer)
                HostTimings.measure("checkpoint.fsync") { channel.force(true) }
            }
            beforeReplace()
            // Deliberately no non-atomic fallback: a torn checkpoint is not recovery.
            Files.move(temporary, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            FileChannel.open(parent, StandardOpenOption.READ).use { it.force(true) }
            afterReplace()
            writes++
            bytesWritten += bytes.size
        } finally {
            Files.deleteIfExists(temporary)
        }
    }

    fun writeInitial(checkpoint: GameCheckpoint, replayEvents: List<JsonObject> = emptyList()) {
        acquire()
        require(!Files.exists(path)) { "Checkpoint already exists; resume it explicitly" }
        require(!Files.exists(accountingPath)) { "Accounting already exists; use a fresh checkpoint path" }
        initializeAccounting(checkpoint.communication, "initial")
        if (recordReplay) replayArchive = ReplayArchive(replayPath)
        write(checkpoint, replayEvents)
    }

    fun read(): GameCheckpoint {
        acquire()
        val document = checkpointJson.parseToJsonElement(Files.readString(path))
        val version = document.jsonObject["formatVersion"]?.jsonPrimitive?.intOrNull
        require(version in setOf(8, 9)) { "Unsupported checkpoint version; start a new game" }
        val saved = checkpointJson.decodeFromJsonElement<GameCheckpoint>(document)
        require(saved.epoch.isNotEmpty()) { "Invalid checkpoint epoch" }
        if (version == 8) {
            val backup = path.resolveSibling("${path.fileName}.v8.json")
            if (!Files.exists(backup)) {
                Files.copy(path, backup)
                FileChannel.open(backup, StandardOpenOption.WRITE).use { it.force(true) }
            }
            val digest = java.security.MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(path)).joinToString("") { "%02x".format(it) }
            initializeAccounting(saved.communication, digest)
            write(saved)
        } else {
            require(saved.accountingId != null && Files.isRegularFile(accountingPath)) { "Checkpoint accounting database is missing" }
            val ledger = accounting ?: HostAccountingStore(accountingPath)
            accounting = ledger
            require(saved.accountingId == ledger.id) { "Checkpoint and accounting database do not match" }
            saved.accountingOutbox.forEach { ledger.submit(it).join() }
        }
        if (recordReplay || saved.replay != null) replayArchive = ReplayArchive(replayPath).also { it.restore(saved) }
        return saved.copy(formatVersion = 9, communication = checkNotNull(accounting).restore(saved.communication), accountingId = accounting!!.id, accountingOutbox = emptyList())
    }

    private fun initializeAccounting(state: CommunicationState, sourceId: String) {
        if (accounting == null) accounting = HostAccountingStore(accountingPath)
        accounting!!.importLegacy(state, sourceId)
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
        accounting?.close()
        accounting = null
        lockChannel?.close()
        lockChannel = null
        lock = null
    }

    companion object {
        private val checkpointJson = Json { encodeDefaults = true }

        fun capture(game: GameInfo, epoch: String, paused: Boolean, states: Map<String, TurnSessionState>, handoff: Long = 0, scheduled: Boolean = false, communication: CommunicationState = CommunicationState(), communicationPaused: Boolean = false): GameCheckpoint = GameCheckpoint(
            epoch = epoch,
            paused = paused,
            handoff = handoff,
            scheduled = scheduled,
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
                    receipts = state.receipts.values.map { SavedReceipt(it.first, it.second) },
                    reviewTurn = state.reviewTurn,
                    reviewActions = state.reviewActions.toList(),
                    previousTurnReview = state.previousTurnReview,
                    combatReports = state.combatReports.toMap(),
                    deliveredCombatReports = state.deliveredCombatReports,
                    freeGreatPersonDecision = state.freeGreatPersonDecision,
                    constructionSelections = state.constructionSelections.toMap(),
                    constructionQueues = state.constructionQueues.toMap(),
                    settlementKnowledge = state.settlementKnowledge.values.toList(),
                    settlementTechnologies = state.settlementTechnologies.toSet(),
                    settlerHistory = state.settlerHistory.toMap(),
                    barbarianSightings = state.barbarianSightings.toMap(),
                )
            },
        )
    }
}

internal fun CommunicationState.withoutAccounting() = copy(
    telemetry = TelemetryState(startedAtMs = 0, hostRevision = "", engineRevision = ""),
    jobs = mutableMapOf(),
    invocations = 0,
)
