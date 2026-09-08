package uncivai

import com.unciv.logic.GameInfo
import com.unciv.logic.files.UncivFiles
import uncivai.player.FreeGreatPersonDecisionTracker
import uncivai.player.TurnSessionState
import uncivai.player.detached
import java.nio.file.Path
import java.util.UUID

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
    internal var epoch = UUID.randomUUID().toString()
        private set
    internal var freeGreatPersonDecisions = FreeGreatPersonDecisionTracker()
        private set
    internal val turnSessions = HashMap<String, TurnSessionState>()
    private var closed = false
    private var paused = false
    internal var scheduled = false
    internal var handoff = 0L
    internal var scheduler: OperatorControl? = null
    private var checkpointStore: CheckpointStore? = null
    private var turnStart: TurnStartCheckpoint? = null
    internal val durableRecovery get() = checkpointStore != null

    @Volatile
    internal var mutationFailure: Throwable? = null
        private set

    internal fun <T> accessGame(block: (GameInfo) -> T): T = synchronized(lifecycleLock) {
        checkAvailable()
        synchronized(game) {
            scheduler?.expireIfNeeded()
            block(game)
        }
    }

    /** Operator diagnostics remain available after an uncertain mutation; player queries do not. */
    internal fun <T> accessOperatorGame(block: (GameInfo) -> T): T = synchronized(lifecycleLock) {
        if (closed || !isAvailable()) throw RuntimeClosedException()
        synchronized(game) {
            scheduler?.expireIfNeeded()
            block(game)
        }
    }

    internal fun requireRunning() {
        if (paused) throw RuntimePausedException()
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
            store.writeInitial(CheckpointStore.capture(game, epoch, paused, turnSessions, handoff, scheduled))
            checkpointStore = store
        } catch (failure: Throwable) {
            store.close()
            throw failure
        }
    }

    internal fun commitCheckpoint() {
        val store = checkpointStore ?: return
        try {
            // END_TURN and dropping its rollback point are one durable commit.
            if (turnStart?.handoff != handoff) turnStart = null
            store.write(CheckpointStore.capture(game, epoch, paused, turnSessions, handoff, scheduled, turnStart))
        } catch (failure: Throwable) {
            haltAfterMutation(failure)
        }
    }

    internal fun restore(checkpoint: GameCheckpoint, store: CheckpointStore) = accessGame {
        check(checkpointStore == null && turnSessions.isEmpty()) { "Restore before binding players" }
        epoch = checkpoint.epoch
        scheduled = checkpoint.scheduled
        paused = checkpoint.paused || scheduled
        handoff = checkpoint.handoff
        check(handoff >= 0) { "Invalid handoff counter" }
        turnStart = checkpoint.turnStart
        turnStart?.let {
            check(scheduled && it.handoff == handoff && it.civilization == game.currentPlayer && it.turn == game.turns) {
                "Checkpoint turn-start state is inconsistent"
            }
        }
        restorePlayers(checkpoint.players)
        checkpointStore = store
        if (scheduled) commitCheckpoint()
    }

    /** Capture before issuing any activation credential, and retain it across retries. */
    internal fun beginScheduledTurn(activationId: String? = null) = accessGame {
        check(scheduled && durableRecovery)
        if (turnStart?.handoff == handoff && activationId in turnStart!!.rolledBackActivationIds) throw OperatorFailure("STALE_ACTIVATION")
        if (turnStart?.handoff != handoff) {
            val saved = CheckpointStore.capture(game, epoch, paused, turnSessions, handoff, scheduled)
            turnStart = TurnStartCheckpoint(handoff, game.currentPlayer, game.turns, saved.gameJson, saved.players)
        } else if (activationId == null || activationId == turnStart?.activationId) {
            return@accessGame
        }
        if (activationId != null) turnStart = checkNotNull(turnStart).copy(activationId = activationId)
        commitCheckpoint()
    }

    internal fun discardTurnStart() {
        turnStart = null
    }

    /** Check before fencing credentials so a delayed retry cannot stop a newer attempt. */
    internal fun requireRollbackActivation(activationId: String) {
        val saved = turnStart ?: throw OperatorFailure("TURN_CHECKPOINT_UNAVAILABLE")
        if (saved.activationId != null && saved.activationId != activationId) throw OperatorFailure("STALE_ACTIVATION")
    }

    /** Caller has fenced the activation; replaying this operation restores the same game. */
    internal fun rollbackScheduledTurn(activationId: String) = accessGame {
        val saved = turnStart ?: throw OperatorFailure("TURN_CHECKPOINT_UNAVAILABLE")
        if (!scheduled || saved.handoff != handoff || saved.civilization != game.currentPlayer || saved.turn != game.turns) {
            throw OperatorFailure("STALE_TURN")
        }
        requireRollbackActivation(activationId)
        if (activationId in saved.rolledBackActivationIds) return@accessGame
        try {
            val replacement = UncivFiles.gameInfoFromString(saved.gameJson)
            check(replacement.currentPlayer == saved.civilization && replacement.turns == saved.turn)
            check(!replacement.isSimulation() && replacement.gameParameters.mods.isEmpty())
            game = replacement
            onGameReplaced(replacement)
            paused = true
            turnSessions.clear()
            restorePlayers(saved.players)
            // A discarded attempt must never regain authority through an old revision.
            turnSessions.values.forEach {
                it.revision = UUID.randomUUID().toString()
                it.fingerprint = null
                it.observedRevision = null
            }
            freeGreatPersonDecisions = FreeGreatPersonDecisionTracker()
            turnStart = saved.copy(activationId = activationId, rolledBackActivationIds = saved.rolledBackActivationIds + activationId)
            commitCheckpoint()
        } catch (failure: Throwable) {
            haltAfterMutation(failure)
        }
    }

    private fun restorePlayers(players: Map<String, SavedPlayerState>) {
        for ((civilization, saved) in players) {
            val player = game.civilizations.single { it.civID == civilization }
            check(saved.alertHandles.size == player.popupAlerts.size) { "Checkpoint alert state is inconsistent" }
            check(saved.tradeHandles.size == player.tradeRequests.size) { "Checkpoint trade state is inconsistent" }
            turnSessions[civilization] = TurnSessionState().apply {
                cityHandles.putAll(saved.cityHandles)
                unitHandles.putAll(saved.unitHandles)
                player.popupAlerts.forEachIndexed { index, alert ->
                    saved.alertHandles[index]?.let { alertHandles[alert] = it }
                }
                player.tradeRequests.forEachIndexed { index, request ->
                    saved.tradeHandles[index]?.let { tradeHandles[request] = it }
                }
                revision = saved.revision
                fingerprint = saved.fingerprint
                reviewTurn = saved.reviewTurn
                reviewActions.addAll(saved.reviewActions)
                previousTurnReview = saved.previousTurnReview?.detached()
                saved.receipts.forEach { entry -> receipts[entry.request.requestId] = entry.request.detached() to entry.receipt.detached() }
            }
        }
    }

    internal fun haltAfterMutation(failure: Throwable): Nothing = synchronized(lifecycleLock) {
        if (mutationFailure == null) mutationFailure = failure
        throw RuntimeHaltedException()
    }

    override fun close() {
        synchronized(lifecycleLock) {
            synchronized(game) {
                closed = true
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
