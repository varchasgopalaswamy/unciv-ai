package uncivai

import com.unciv.logic.GameInfo
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import uncivai.player.ChooseFreeGreatPersonRequest
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.RenameCityRequest
import uncivai.player.TurnCommandRequest
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import java.util.UUID

@Serializable
internal data class OperatorRequest(
    val op: String,
    val leaseId: String? = null,
    val epoch: String? = null,
    val serverInstance: String? = null,
    val handoff: Long? = null,
    val civilization: String? = null,
    val activationId: String? = null,
)

@Serializable
internal data class OperatorStatus(
    val epoch: String,
    val serverInstance: String,
    val turn: Int,
    val handoff: Long,
    val phase: String,
    val activeCivilization: String,
    val scheduled: Boolean,
    val leaseId: String?,
    val leaseExpiresInMs: Long,
)

@Serializable
internal data class OperatorResponse(
    val status: OperatorStatus,
    val leaseId: String? = null,
    val playerToken: String? = null,
    val activationId: String? = null,
)

internal class OperatorFailure(val code: String) : IllegalStateException(code)

/** Local operator authority, deliberately separate from the player MCP capabilities. */
class OperatorControl internal constructor(
    private val runtime: GameExecutor,
    private val clockMillis: () -> Long = { System.nanoTime() / 1_000_000 },
) {
    private val serverInstance = UUID.randomUUID().toString()
    private var leaseId: String? = null
    private var expiresAt = 0L
    private var activation: Activation? = null
    private val host = PlayerHost(runtime)

    init {
        runtime.accessGame {
            check(runtime.scheduler == null) { "Operator control is already configured" }
            runtime.scheduler = this
        }
    }

    private class Activation(
        val id: String,
        val civilization: String,
        val handoff: Long,
        val token: String,
        val session: PlayerSession,
    )

    internal fun handle(request: OperatorRequest): OperatorResponse = runtime.accessOperatorGame { game ->
        validate(request)
        if (request.op == "status") return@accessOperatorGame OperatorResponse(status(game))
        if (runtime.mutationFailure != null) throw OperatorFailure("GAME_HALTED")
        when (request.op) {
            "acquire" -> {
                if (!runtime.durableRecovery) throw OperatorFailure("DURABLE_CHECKPOINT_REQUIRED")
                if (leaseId != null && leaseId != request.leaseId) throw OperatorFailure("LEASE_BUSY")
                if (leaseId == null) {
                    leaseId = request.leaseId
                    runtime.scheduled = true
                    activation = null
                    renew()
                    runtime.pause()
                } else {
                    renew()
                }
                // Also cover a supervisor crash after recording its intent but before activate.
                if (!finished(game)) runtime.beginScheduledTurn()
            }

            "heartbeat" -> {
                requireLease(request.leaseId)
                renew()
            }

            "activate" -> {
                requireLease(request.leaseId)
                if (request.epoch != runtime.epoch || request.serverInstance != serverInstance || request.handoff != runtime.handoff || request.civilization != game.currentPlayer) {
                    throw OperatorFailure("STALE_TURN")
                }
                if (finished(game) || game.currentPlayerCiv.isDefeated()) throw OperatorFailure("PLAYER_UNAVAILABLE")
                runtime.beginScheduledTurn(request.activationId)
                val previous = activation
                if (previous != null && previous.id == request.activationId) {
                    if (previous.civilization != request.civilization || previous.handoff != request.handoff) throw OperatorFailure("ACTIVATION_CONFLICT")
                } else {
                    val id = checkNotNull(request.activationId)
                    val civilization = checkNotNull(request.civilization)
                    val session = host.bindPlayer(civilization)
                    activation = Activation(id, civilization, runtime.handoff, newToken(), session)
                }
                renew()
                runtime.resume()
                val current = checkNotNull(activation)
                return@accessOperatorGame OperatorResponse(status(game), leaseId, current.token, current.id)
            }

            "rollback" -> {
                requireLease(request.leaseId)
                if (request.epoch != runtime.epoch || request.serverInstance != serverInstance || request.handoff != runtime.handoff || request.civilization != game.currentPlayer) {
                    throw OperatorFailure("STALE_TURN")
                }
                // Validate the attempt before revoking a potentially newer activation.
                runtime.requireRollbackActivation(checkNotNull(request.activationId))
                activation = null
                runtime.rollbackScheduledTurn(request.activationId)
                renew()
                // The enclosing access block refers to the game that was replaced.
                return@accessOperatorGame runtime.accessOperatorGame { OperatorResponse(status(it), leaseId) }
            }

            "pause" -> {
                if (request.leaseId != null) requireLease(request.leaseId) else leaseId = null
                activation = null
                runtime.pause()
            }

            "release" -> {
                requireLease(request.leaseId)
                activation = null
                leaseId = null
                runtime.pause()
            }

            "manual" -> {
                if (leaseId != null) throw OperatorFailure("LEASE_BUSY")
                activation = null
                runtime.scheduled = false
                runtime.discardTurnStart()
                runtime.resume()
            }
        }
        OperatorResponse(status(game), leaseId)
    }

    /** Called while holding the executor lock, including immediately before a player operation. */
    internal fun expireIfNeeded() {
        if (leaseId == null || clockMillis() < expiresAt) return
        leaseId = null
        activation = null
        if (runtime.mutationFailure == null) runtime.pause()
    }

    internal fun manualSession(session: PlayerSession): PlayerSession = GuardedSession(session) { mutation ->
        if (mutation && runtime.scheduled) deny()
    }

    internal fun activationSession(token: String): PlayerSession? = runtime.accessOperatorGame {
        val current = activation ?: return@accessOperatorGame null
        if (!MessageDigest.isEqual(hash(token), hash(current.token))) return@accessOperatorGame null
        GuardedSession(current.session, current.civilization) { mutation ->
            if (activation !== current || leaseId == null || (mutation && runtime.handoff != current.handoff)) deny()
        }
    }

    private inner class GuardedSession(
        private val delegate: PlayerSession,
        private val civilization: String? = null,
        private val check: (Boolean) -> Unit,
    ) : PlayerSession {
        private fun <T> guarded(mutation: Boolean = false, block: () -> T): T = runtime.accessOperatorGame {
            check(mutation)
            block()
        }
        override fun capabilities() = guarded { delegate.capabilities() }
        override fun turn() = guarded { delegate.turn() }
        override fun summary() = guarded { delegate.summary() }
        override fun cities() = guarded { delegate.cities() }
        override fun freeGreatPersonChoice() = guarded { delegate.freeGreatPersonChoice() }
        override fun execute(request: TurnCommandRequest) = guarded {
            val completed = civilization?.let { runtime.turnSessions[it]?.receipts?.get(request.requestId) }
            // A completed receipt is a read, including a lost END_TURN acknowledgement.
            if (completed?.first != request) check(true)
            delegate.execute(request)
        }
        override fun renameCity(request: RenameCityRequest) = guarded(true) { delegate.renameCity(request) }
        override fun chooseFreeGreatPerson(request: ChooseFreeGreatPersonRequest) = guarded(true) { delegate.chooseFreeGreatPerson(request) }
    }

    private fun status(game: GameInfo) = OperatorStatus(
        epoch = runtime.epoch,
        serverInstance = serverInstance,
        turn = game.turns,
        handoff = runtime.handoff,
        phase = when {
            runtime.mutationFailure != null -> "HALTED"
            finished(game) -> "FINISHED"
            runtime.isPaused() -> "PAUSED"
            else -> "ACTIVE"
        },
        activeCivilization = game.currentPlayer,
        scheduled = runtime.scheduled,
        leaseId = leaseId,
        leaseExpiresInMs = if (leaseId == null) 0 else (expiresAt - clockMillis()).coerceAtLeast(0),
    )

    private fun finished(game: GameInfo) = !game.oneMoreTurnMode &&
        (game.victoryData != null || game.civilizations.any { it.victoryManager.hasWon() })

    private fun requireLease(id: String?) {
        if (id == null || leaseId == null || id != leaseId) throw OperatorFailure("LEASE_LOST")
    }

    private fun renew() {
        expiresAt = clockMillis() + LEASE_MILLIS
    }

    private fun validate(request: OperatorRequest) {
        val keys = when (request.op) {
            "status", "manual" -> emptySet()
            "acquire", "heartbeat", "release" -> setOf("leaseId")
            "pause" -> if (request.leaseId == null) emptySet() else setOf("leaseId")
            "activate" -> setOf("leaseId", "epoch", "serverInstance", "handoff", "civilization", "activationId")
            "rollback" -> setOf("leaseId", "epoch", "serverInstance", "handoff", "civilization", "activationId")
            else -> throw OperatorFailure("INVALID_ARGUMENT")
        }
        val values = mapOf("leaseId" to request.leaseId, "epoch" to request.epoch, "serverInstance" to request.serverInstance, "handoff" to request.handoff, "civilization" to request.civilization, "activationId" to request.activationId)
        if (values.filterValues { it != null }.keys != keys || values.values.any { it is String && (it.isBlank() || it.length > 256) } || (request.handoff != null && request.handoff < 0)) {
            throw OperatorFailure("INVALID_ARGUMENT")
        }
    }

    companion object {
        const val LEASE_MILLIS = 30_000L
        internal val json = Json { encodeDefaults = true }
        private val random = SecureRandom()
        private fun newToken(): String = Base64.getUrlEncoder().withoutPadding().encodeToString(ByteArray(32).also(random::nextBytes))
        private fun hash(value: String) = MessageDigest.getInstance("SHA-256").digest(value.toByteArray(StandardCharsets.UTF_8))
        private fun deny(): Nothing = throw PlayerSessionException(PlayerSessionFailure.COMMAND_UNAVAILABLE)
    }
}
