package uncivai

import com.unciv.logic.GameInfo
import uncivai.player.FreeGreatPersonDecisionTracker
import uncivai.player.TurnSessionState
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
    private val game: GameInfo,
    private val lifecycleLock: Any = Any(),
    private val isAvailable: () -> Boolean = { true },
) : AutoCloseable {
    internal var epoch = UUID.randomUUID().toString()
        private set
    internal val freeGreatPersonDecisions = FreeGreatPersonDecisionTracker()
    internal val turnSessions = HashMap<String, TurnSessionState>()
    private var closed = false
    private var paused = false
    private var checkpointStore: CheckpointStore? = null
    internal val durableRecovery get() = checkpointStore != null

    @Volatile
    internal var mutationFailure: Throwable? = null
        private set

    internal fun <T> accessGame(block: (GameInfo) -> T): T = synchronized(lifecycleLock) {
        checkAvailable()
        synchronized(game) { block(game) }
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
            store.writeInitial(CheckpointStore.capture(game, epoch, paused, turnSessions))
            checkpointStore = store
        } catch (failure: Throwable) {
            store.close()
            throw failure
        }
    }

    internal fun commitCheckpoint() {
        val store = checkpointStore ?: return
        try {
            store.write(CheckpointStore.capture(game, epoch, paused, turnSessions))
        } catch (failure: Throwable) {
            haltAfterMutation(failure)
        }
    }

    internal fun restore(checkpoint: GameCheckpoint, store: CheckpointStore) = accessGame {
        check(checkpointStore == null && turnSessions.isEmpty()) { "Restore before binding players" }
        epoch = checkpoint.epoch
        paused = checkpoint.paused
        for ((civilization, saved) in checkpoint.players) {
            val player = game.civilizations.single { it.civID == civilization }
            check(saved.alertHandles.size == player.popupAlerts.size) { "Checkpoint alert state is inconsistent" }
            turnSessions[civilization] = TurnSessionState().apply {
                cityHandles.putAll(saved.cityHandles)
                player.popupAlerts.forEachIndexed { index, alert ->
                    saved.alertHandles[index]?.let { alertHandles[alert] = it }
                }
                revision = saved.revision
                fingerprint = saved.fingerprint
                saved.receipts.forEach { entry -> receipts[entry.request.requestId] = entry.request to entry.receipt }
            }
        }
        checkpointStore = store
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
