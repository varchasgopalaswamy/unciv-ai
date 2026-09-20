package uncivai

import com.unciv.logic.GameInfo
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import uncivai.player.ChooseFreeGreatPersonRequest
import uncivai.player.PlayerFinalState
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.RenameCityRequest
import uncivai.player.TurnCommandRequest
import uncivai.player.finalState
import uncivai.player.gameFinished
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
    val maxInvocations: Long? = null,
    val maxConcurrent: Int? = null,
    val maxPerCivilization: Int? = null,
    val checkpoint: GameCheckpoint? = null,
)

@Serializable
internal data class RotatedPlayerCredential(val civilizationId: String, val token: String)

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
    val modelInvocations: Long = 0,
    val roundStart: Boolean = false,
    val modelUsage: Map<String, Long> = emptyMap(),
    val negotiationWaitMillis: Long = 0,
    val negotiationTimeoutSeconds: Int = 300,
    val telemetryExportError: String? = null,
)

@Serializable
internal data class OperatorResponse(
    val status: OperatorStatus,
    val leaseId: String? = null,
    val playerToken: String? = null,
    val activationId: String? = null,
    val results: Map<String, PlayerFinalState> = emptyMap(),
    val checkpoint: GameCheckpoint? = null,
    val players: List<RotatedPlayerCredential> = emptyList(),
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
    private val communicationSessions = mutableMapOf<String, Pair<String, PlayerSession>>()
    private val configuredCredentials = mutableSetOf<String>()
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
        if (request.op == "results") {
            val results = game.civilizations.filter { it.isHuman() && it.isMajorCiv() }.mapNotNull { player ->
                if (!finished(game) && !player.isDefeated()) return@mapNotNull null
                val snapshot = host.bindPlayer(player.civID).turn()
                if (snapshot.result == null) return@mapNotNull null
                player.civID to snapshot.finalState(player.civID)
            }.toMap()
            return@accessOperatorGame OperatorResponse(status(game), results = results)
        }
        try {
            when (request.op) {
                "barrier" -> {
                    requireLease(request.leaseId)
                    activation = null
                    runtime.communicationPaused = true
                    runtime.pause()
                }

                "unbarrier" -> {
                    requireLease(request.leaseId)
                    if (runtime.communicationState.restorePending) throw OperatorFailure("RESTORE_INCOMPLETE")
                    runtime.communicationPaused = false
                    runtime.commitCheckpoint()
                }

                "snapshot" -> {
                    requireLease(request.leaseId)
                    return@accessOperatorGame OperatorResponse(status(game), checkpoint = runtime.roundSnapshot())
                }

                "restore" -> {
                    requireLease(request.leaseId)
                    activation = null
                    communicationSessions.clear()
                    runtime.restoreRound(checkNotNull(request.checkpoint), checkNotNull(request.epoch), configuredCredentials)
                    return@accessOperatorGame runtime.accessOperatorGame { OperatorResponse(status(it), leaseId) }
                }

                "connect" -> {
                    requireLease(request.leaseId)
                    if (request.epoch != runtime.epoch) throw OperatorFailure("STALE_TURN")
                    val civilization = checkNotNull(request.civilization)
                    val session = host.bindPlayer(civilization)
                    val epoch = runtime.epoch
                    val owner = leaseId
                    val token = newToken()
                    communicationSessions[token] = epoch to GuardedSession(session) { mutation ->
                        if (mutation || runtime.epoch != epoch || leaseId == null || leaseId != owner) deny()
                    }
                    runtime.communicationState.coordinatedRecovery = true
                    runtime.commitCheckpoint()
                    return@accessOperatorGame OperatorResponse(status(game), leaseId, token)
                }

                "rotate_players" -> {
                    requireLease(request.leaseId)
                    if (request.epoch != runtime.epoch || !runtime.communicationPaused) throw OperatorFailure("STALE_TURN")
                    val credentials = runtime.communicationState
                    credentials.revokedCredentials.addAll(configuredCredentials)
                    credentials.revokedCredentials.addAll(credentials.playerCredentials.keys)
                    credentials.playerCredentials.clear()
                    val players = game.civilizations.filter { it.isHuman() && it.isMajorCiv() }.map { player ->
                        val token = newToken()
                        credentials.playerCredentials[credentialHash(token)] = player.civID
                        RotatedPlayerCredential(player.civID, token)
                    }
                    runtime.commitCheckpoint()
                    return@accessOperatorGame OperatorResponse(status(game), players = players)
                }

                "restore_complete" -> {
                    requireLease(request.leaseId)
                    if (request.epoch != runtime.epoch) throw OperatorFailure("STALE_TURN")
                    runtime.communicationState.restorePending = false
                    runtime.commitCheckpoint()
                }

                "job_limits" -> {
                    requireLease(request.leaseId)
                    val state = runtime.communicationState
                    if (checkNotNull(request.maxInvocations) < state.invocations || checkNotNull(request.maxConcurrent) !in 1..64 || checkNotNull(request.maxPerCivilization) !in 1..2) throw OperatorFailure("INVALID_ARGUMENT")
                    state.maxInvocations = request.maxInvocations
                    state.maxConcurrent = request.maxConcurrent
                    state.maxPerCivilization = request.maxPerCivilization
                    runtime.commitCheckpoint()
                }

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
                    if (runtime.communicationState.restorePending || runtime.communicationPaused) throw OperatorFailure("RESTORE_INCOMPLETE")
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
                    if (runtime.communicationState.coordinatedRecovery) throw OperatorFailure("COORDINATED_RESTORE_REQUIRED")
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
                    if (request.leaseId != null) {
                        requireLease(request.leaseId)
                    } else {
                        leaseId = null
                        runtime.communicationPaused = true
                    }
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
                    if (runtime.communicationState.restorePending) throw OperatorFailure("RESTORE_INCOMPLETE")
                    activation = null
                    runtime.scheduled = false
                    runtime.communicationPaused = false
                    runtime.discardTurnStart()
                    runtime.resume()
                }
            }
            OperatorResponse(status(game), leaseId)
        } finally {
            // All state and credential changes precede the wake, even for early returns.
            if (request.op != "heartbeat") runtime.communicationChanges.invalidate()
        }
    }

    /** Called while holding the executor lock, including immediately before a player operation. */
    internal fun expireIfNeeded() {
        if (leaseId == null || clockMillis() < expiresAt) return
        leaseId = null
        activation = null
        runtime.communicationChanges.invalidate()
        if (runtime.mutationFailure == null) runtime.pause()
    }

    internal fun manualSession(session: PlayerSession, token: String? = null): PlayerSession {
        val boundEpoch = runtime.epoch
        val credential = token?.let(::credentialHash)
        if (credential != null) configuredCredentials.add(credential)
        return GuardedSession(session) { mutation ->
            if (runtime.epoch != boundEpoch || credential in runtime.communicationState.revokedCredentials || (mutation && runtime.scheduled)) deny()
        }
    }

    internal fun activationSession(token: String): PlayerSession? = runtime.accessOperatorGame {
        runtime.communicationState.playerCredentials[credentialHash(token)]?.let { civilization ->
            return@accessOperatorGame manualSession(host.bindPlayer(civilization), token)
        }
        communicationSessions[token]?.let { (epoch, session) ->
            return@accessOperatorGame session.takeIf { epoch == runtime.epoch && leaseId != null }
        }
        val current = activation ?: return@accessOperatorGame null
        if (!MessageDigest.isEqual(hash(token), hash(current.token))) return@accessOperatorGame null
        GuardedSession(current.session) { mutation ->
            if (activation !== current || leaseId == null || (mutation && runtime.handoff != current.handoff)) deny()
        }
    }

    private inner class GuardedSession(
        private val delegate: PlayerSession,
        private val check: (Boolean) -> Unit,
    ) : PlayerSession,
        PlayerApiAccounting {
        override fun beginApiCall(tool: String, arguments: kotlinx.serialization.json.JsonObject?) = guarded { (delegate as? PlayerApiAccounting)?.beginApiCall(tool, arguments) }
        override fun finishApiCall(id: String, outcome: String) {
            (delegate as? PlayerApiAccounting)?.finishApiCall(id, outcome)
        }
        private fun <T> guarded(mutation: Boolean = false, block: () -> T): T = runtime.accessOperatorGame {
            check(mutation)
            block()
        }
        override fun capabilities() = guarded { delegate.capabilities() }
        override fun communicationChanges() = guarded { delegate.communicationChanges() }
        override fun communication(request: kotlinx.serialization.json.JsonObject) = guarded { delegate.communication(request) }
        override fun turn() = guarded { delegate.turn() }
        override fun result() = guarded { delegate.result() }
        override fun movementPreview(request: uncivai.player.MovementPreviewRequest) = guarded { delegate.movementPreview(request) }
        override fun missionPreview(request: uncivai.player.MissionPreviewRequest) = guarded { delegate.missionPreview(request) }
        override fun researchDetails(names: List<String>) = guarded { delegate.researchDetails(names) }
        override fun policyDetails(names: List<String>) = guarded { delegate.policyDetails(names) }
        override fun victoryProgress(names: List<String>) = guarded { delegate.victoryProgress(names) }
        override fun completedReceipt(request: TurnCommandRequest) = guarded { delegate.completedReceipt(request) }
        override fun summary() = guarded { delegate.summary() }
        override fun cities() = guarded { delegate.cities() }
        override fun freeGreatPersonChoice() = guarded { delegate.freeGreatPersonChoice() }
        override fun execute(request: TurnCommandRequest) = guarded {
            // A completed receipt is a read, including a lost END_TURN acknowledgement.
            delegate.completedReceipt(request)?.let { return@guarded it }
            check(true)
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
        negotiationWaitMillis = runtime.communication.trades.waitMillis(),
        negotiationTimeoutSeconds = runtime.communicationState.trades.timeoutSeconds,
        scheduled = runtime.scheduled,
        leaseId = leaseId,
        leaseExpiresInMs = if (leaseId == null) 0 else (expiresAt - clockMillis()).coerceAtLeast(0),
        modelInvocations = runtime.communicationState.invocations,
        telemetryExportError = runtime.telemetry.exportError,
        modelUsage = listOf("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens").associateWith { key ->
            runtime.communicationState.jobs.values.sumOf { job ->
                job.usage?.tokens(key) ?: 0L
            }
        },
        roundStart = game.civilizations.firstOrNull { it.isHuman() && it.isMajorCiv() && !it.isDefeated() }?.civID == game.currentPlayer,
    )

    private fun finished(game: GameInfo) = gameFinished(game)

    private fun requireLease(id: String?) {
        if (id == null || leaseId == null || id != leaseId) throw OperatorFailure("LEASE_LOST")
    }

    private fun renew() {
        expiresAt = clockMillis() + LEASE_MILLIS
    }

    private fun validate(request: OperatorRequest) {
        val keys = when (request.op) {
            "status", "results", "manual" -> emptySet()
            "acquire", "heartbeat", "release" -> setOf("leaseId")
            "pause" -> if (request.leaseId == null) emptySet() else setOf("leaseId")
            "activate" -> setOf("leaseId", "epoch", "serverInstance", "handoff", "civilization", "activationId")
            "rollback" -> setOf("leaseId", "epoch", "serverInstance", "handoff", "civilization", "activationId")
            "connect" -> setOf("leaseId", "epoch", "civilization")
            "rotate_players", "restore_complete" -> setOf("leaseId", "epoch")
            "job_limits" -> setOf("leaseId", "maxInvocations", "maxConcurrent", "maxPerCivilization")
            "barrier", "unbarrier", "snapshot" -> setOf("leaseId")
            "restore" -> setOf("leaseId", "epoch", "checkpoint")
            else -> throw OperatorFailure("INVALID_ARGUMENT")
        }
        val values = mapOf("leaseId" to request.leaseId, "epoch" to request.epoch, "serverInstance" to request.serverInstance, "handoff" to request.handoff, "civilization" to request.civilization, "activationId" to request.activationId, "maxInvocations" to request.maxInvocations, "maxConcurrent" to request.maxConcurrent, "maxPerCivilization" to request.maxPerCivilization, "checkpoint" to request.checkpoint)
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
        private fun credentialHash(value: String) = Base64.getUrlEncoder().withoutPadding().encodeToString(hash(value))
        private fun deny(): Nothing = throw PlayerSessionException(PlayerSessionFailure.COMMAND_UNAVAILABLE)
    }
}
