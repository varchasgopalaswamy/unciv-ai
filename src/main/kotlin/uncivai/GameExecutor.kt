package uncivai

import com.unciv.json.json
import com.unciv.logic.GameInfo
import com.unciv.logic.files.UncivFiles
import com.unciv.view.GameView
import uncivai.player.PlayerIdentifier
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.TurnSessionState
import uncivai.player.captureSettlementKnowledge
import uncivai.player.detached
import uncivai.player.gameFinished
import uncivai.player.observation
import java.nio.file.Path
import java.security.SecureRandom
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/**
 * Trusted ownership of one game, independent of graphics or transport bootstrap.
 * Raw state and diagnostics must never be exposed to a player client.
 *
 * Embedded callers must route all mutations and game replacement through this
 * service. The game monitor also serializes shared engine PlayerOperations.
 */
class GameExecutor internal constructor(
    private var game: GameInfo,
    private val lifecycleLock: Any = Any(),
    private val onGameReplaced: (GameInfo) -> Unit = {},
    private val isAvailable: () -> Boolean = { true },
) : AutoCloseable {
    // Epochs distinguish independent games; entity/revision counters are local to each player.
    internal var epoch = "g" + java.lang.Long.toUnsignedString(SecureRandom().nextLong(), 36)
        private set
    internal val turnSessions = HashMap<String, TurnSessionState>()

    init {
        observeVisibility()
    }

    private fun observeVisibility() {
        for (player in game.civilizations.filter { it.isHuman() && it.isMajorCiv() }) {
            player.cache.onViewableTilesUpdated = { tiles ->
                val state = turnSessions.getOrPut(player.civID) { TurnSessionState() }
                captureSettlementKnowledge(player, state, tiles)
            }
        }
    }

    private fun stopObservingVisibility() {
        game.civilizations.forEach { it.cache.onViewableTilesUpdated = null }
    }
    internal var communicationState = CommunicationState()
    internal val communication = Communication(this)
    internal val telemetry = Telemetry(this)
    internal val communicationChanges = CommunicationChanges()
    internal var communicationPaused = false
    internal var clockMillis: () -> Long = System::currentTimeMillis
    private val tradeDeadlines = Executors.newSingleThreadScheduledExecutor { task ->
        Thread(task, "trade-deadline").apply { isDaemon = true }
    }
    private var tradeDeadline: ScheduledFuture<*>? = null

    internal fun scheduleTradeDeadline(deadline: Long) {
        tradeDeadline?.cancel(false)
        tradeDeadline = tradeDeadlines.schedule({
            // Shutdown or fail-stop may win the race. Such a runtime must stay closed.
            runCatching { accessGame {} }
        }, (deadline - clockMillis()).coerceAtLeast(0), TimeUnit.MILLISECONDS)
    }

    /** Capture each player's permitted reports before turn processing can expire engine history. */
    internal fun captureCombatReports() {
        for (player in game.civilizations.filter { it.isHuman() && it.isMajorCiv() }) {
            val entries = GameView(game, player).attackEventsView.getCombatReportEntries()
            if (entries.isEmpty()) continue
            val state = turnSessions.getOrPut(player.civID) { TurnSessionState() }
            for (entry in entries) {
                state.combatReports.getOrPut(entry.id) {
                    entry.observation(state.identifiers.next(PlayerIdentifier.REPORT))
                }
            }
        }
    }
    private var closed = false
    private var paused = false
    internal var scheduled = false
    internal var handoff = 0L
    internal var scheduler: OperatorControl? = null
    private var checkpointStore: CheckpointStore? = null
    internal val durableRecovery get() = checkpointStore != null

    @Volatile
    internal var mutationFailure: Throwable? = null
        private set

    internal fun <T> accessGame(block: (GameInfo) -> T): T = synchronized(lifecycleLock) {
        checkAvailable()
        val outermost = !Thread.holdsLock(game)
        synchronized(game) {
            if (outermost) {
                scheduler?.expireIfNeeded()
                communication.trades.expireIfNeeded()
                val nativeChanged = communication.trades.nativeBridge.synchronize(game)
                if (communication.agreements.synchronize(game) || nativeChanged) commitCheckpoint()
            }
            block(game)
        }
    }

    /** Operator diagnostics remain available after an uncertain mutation; player queries do not. */
    internal fun <T> accessOperatorGame(block: (GameInfo) -> T): T = synchronized(lifecycleLock) {
        if (closed || !isAvailable()) throw RuntimeClosedException()
        val outermost = !Thread.holdsLock(game)
        synchronized(game) {
            if (outermost) {
                scheduler?.expireIfNeeded()
                if (mutationFailure == null) {
                    communication.trades.expireIfNeeded()
                    val nativeChanged = communication.trades.nativeBridge.synchronize(game)
                    if (communication.agreements.synchronize(game) || nativeChanged) commitCheckpoint()
                }
            }
            block(game)
        }
    }

    internal fun requireRunning() {
        if (paused) throw RuntimePausedException()
        if (communicationState.trades.activeProposal != null) throw PlayerSessionException(PlayerSessionFailure.NEGOTIATION_ACTIVE)
    }

    internal fun isPaused(): Boolean = paused

    /** Operator-only pause: observations remain available and commands are rejected. */
    fun pause() = accessGame {
        paused = true
        commitCheckpoint()
    }

    /** A mutation failure requires recovery; resume cannot clear it. */
    fun resume() = accessGame {
        paused = false
        commitCheckpoint()
    }

    /** Starts durable command acknowledgement. Existing checkpoints require explicit restoration. */
    fun enableCheckpoints(path: Path) = enableCheckpoints(CheckpointStore(path))

    internal fun enableCheckpoints(store: CheckpointStore) = accessGame {
        check(checkpointStore == null) { "Checkpointing is already configured" }
        try {
            store.writeInitial(CheckpointStore.capture(game, epoch, paused, turnSessions, handoff, scheduled, communication = communicationState))
            checkpointStore = store
            telemetry.publish(game, store.path)
            TradeCycleScenarioSetup.recordEvidence(game, communicationState, store.path.parent)
        } catch (failure: Throwable) {
            store.close()
            throw failure
        }
    }

    internal fun commitCheckpoint() {
        try {
            communication.trades.nativeBridge.synchronize(game)
            communication.agreements.synchronize(game)
            telemetry.synchronize(game)
            checkpointStore?.write(CheckpointStore.capture(game, epoch, paused, turnSessions, handoff, scheduled, communicationState, communicationPaused))
            turnSessions.values.forEach { it.checkpointRevision = it.revision }
            communicationChanges.committed(communicationState)
            checkpointStore?.let {
                telemetry.publish(game, it.path)
                TradeCycleScenarioSetup.recordEvidence(game, communicationState, it.path.parent)
            }
        } catch (failure: Throwable) {
            haltAfterMutation(failure)
        }
    }

    internal fun restore(checkpoint: GameCheckpoint, store: CheckpointStore) = accessGame {
        check(checkpointStore == null && turnSessions.isEmpty()) { "Restore before binding players" }
        epoch = checkpoint.epoch
        communicationState = checkpoint.communication
        val pendingTelemetry = communicationState.telemetry.pendingCalls.isNotEmpty()
        telemetry.restored()
        communicationState.trades.activeProposal?.let {
            scheduleTradeDeadline(communicationState.trades.proposals.getValue(it).last().deadlineMillis)
        }
        communicationPaused = checkpoint.communicationPaused
        scheduled = checkpoint.scheduled
        paused = checkpoint.paused || scheduled
        handoff = checkpoint.handoff
        check(handoff >= 0) { "Invalid handoff counter" }
        restorePlayers(checkpoint.players, checkpoint.gameJson)
        checkpointStore = store
        if (scheduled || pendingTelemetry) commitCheckpoint() else telemetry.publish(game, store.path)
    }

    internal fun turnSnapshot(): GameCheckpoint = accessGame {
        check(paused && communicationPaused && scheduled)
        val captured = CheckpointStore.capture(game, epoch, true, turnSessions, handoff, true, communication = communicationState, communicationPaused = true)
        kotlinx.serialization.json.Json.decodeFromString<GameCheckpoint>(kotlinx.serialization.json.Json.encodeToString(captured))
    }

    internal fun restoreTurn(saved: GameCheckpoint, newEpoch: String, configuredCredentials: Set<String> = emptySet()) = accessGame {
        check(paused && communicationPaused && scheduled)
        if (gameFinished(game)) throw OperatorFailure("GAME_FINISHED")
        if (saved.handoff != handoff) throw OperatorFailure("TURN_ALREADY_COMMITTED")
        val replacement = UncivFiles.gameInfoFromString(saved.gameJson)
        check(!replacement.isSimulation() && replacement.gameParameters.mods.isEmpty())
        check(replacement.civilizations.filter { it.isHuman() }.map { it.civID }.toSet() == game.civilizations.filter { it.isHuman() }.map { it.civID }.toSet())
        val accounting = communicationState
        try {
            stopObservingVisibility()
            game = replacement
            onGameReplaced(replacement)
            epoch = newEpoch
            handoff = saved.handoff
            turnSessions.clear()
            restorePlayers(saved.players, saved.gameJson)
            observeVisibility()
            val restoredCommunication = kotlinx.serialization.json.Json.decodeFromString<CommunicationState>(kotlinx.serialization.json.Json.encodeToString(saved.communication))
            communicationState = restoredCommunication.copy(
                jobs = accounting.jobs.mapValues { (_, job) -> job.copy(expires = 0) }.toMutableMap(), invocations = accounting.invocations,
                telemetry = accounting.telemetry,
                maxInvocations = accounting.maxInvocations, maxConcurrent = accounting.maxConcurrent, maxPerCivilization = accounting.maxPerCivilization,
                restorePending = true,
                revokedCredentials = (accounting.revokedCredentials + accounting.playerCredentials.keys + configuredCredentials).toMutableSet(),
                playerCredentials = mutableMapOf(),
            )
            tradeDeadline?.cancel(false)
            communicationState.trades.activeProposal?.let {
                scheduleTradeDeadline(communicationState.trades.proposals.getValue(it).last().deadlineMillis)
            }
            turnSessions.values.forEach {
                it.fingerprint = null
                it.observedRevision = null
            }
            commitCheckpoint()
            communicationChanges.invalidate()
        } catch (failure: Throwable) {
            haltAfterMutation(failure)
        }
    }

    private fun restorePlayers(players: Map<String, SavedPlayerState>, gameJson: String) {
        // Rebuilding engine caches can append notices, for example about a captured
        // city's connection. Validate handles against the original save, then require
        // that loading preserved its entire notification prefix before binding them.
        val serializer = json()
        val original = serializer.fromJson(GameInfo::class.java, gameJson)
        for ((civilization, saved) in players) {
            val player = game.civilizations.single { it.civID == civilization }
            val originalNotifications = original.civilizations.single { it.civID == civilization }.notifications
            check(saved.alertHandles.size == player.popupAlerts.size) { "Checkpoint alert state is inconsistent" }
            check(saved.notificationHandles.size == originalNotifications.size) { "Checkpoint notification state is inconsistent" }
            check(
                player.notifications.size >= originalNotifications.size && originalNotifications.indices.all {
                    serializer.toJson(originalNotifications[it]) == serializer.toJson(player.notifications[it])
                },
            ) { "Loading changed saved notifications" }
            turnSessions[civilization] = TurnSessionState(saved.identifiers).apply {
                cityHandles.putAll(saved.cityHandles)
                unitHandles.putAll(saved.unitHandles)
                constructionSelections.putAll(saved.constructionSelections)
                settlementKnowledge.putAll(saved.settlementKnowledge.associateBy { it.position })
                settlementTechnologies.addAll(saved.settlementTechnologies)
                settlerHistory.putAll(saved.settlerHistory)
                player.popupAlerts.forEachIndexed { index, alert ->
                    saved.alertHandles[index]?.let { alertHandles[alert] = it }
                }
                player.notifications.forEachIndexed { index, notification ->
                    saved.notificationHandles.getOrNull(index)?.let { notificationHandles[notification] = it }
                }
                dismissedNotifications.addAll(saved.dismissedNotifications)
                revision = saved.revision
                checkpointRevision = saved.revision
                fingerprint = saved.fingerprint
                reviewTurn = saved.reviewTurn
                reviewActions.addAll(saved.reviewActions)
                previousTurnReview = saved.previousTurnReview?.detached()
                saved.combatReports.forEach { (engineId, report) -> combatReports[engineId] = report.detached() }
                deliveredCombatReports = saved.deliveredCombatReports
                freeGreatPersonDecision = saved.freeGreatPersonDecision
                check(deliveredCombatReports in 0..combatReports.size) { "Checkpoint combat report cursor is inconsistent" }
                saved.receipts.forEach { entry -> receipts[entry.request.requestId] = entry.request.detached() to entry.receipt.detached() }
            }
        }
    }

    internal fun haltAfterMutation(failure: Throwable): Nothing = synchronized(lifecycleLock) {
        if (mutationFailure == null) mutationFailure = failure
        communicationChanges.invalidate()
        throw RuntimeHaltedException()
    }

    override fun close() {
        synchronized(lifecycleLock) {
            synchronized(game) {
                stopObservingVisibility()
                closed = true
                communicationChanges.invalidate()
                tradeDeadlines.shutdownNow()
                checkpointStore?.close()
            }
        }
    }

    private fun checkAvailable() {
        if (closed || !isAvailable()) throw RuntimeClosedException()
        if (mutationFailure != null) throw RuntimeHaltedException()
    }
}

internal class RuntimePausedException : IllegalStateException("Runtime is paused")
