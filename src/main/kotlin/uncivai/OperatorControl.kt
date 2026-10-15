package uncivai

import com.unciv.logic.GameInfo
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import uncivai.player.PlayerFinalState
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.TurnCommandRequest
import uncivai.player.finalState
import uncivai.player.gameFinished
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

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
    val pauseReason: String? = null,
)

@Serializable
internal data class RotatedPlayerCredential(val civilizationId: String, val token: String, val controller: String)

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
    val modelUsage: Map<String, Long> = emptyMap(),
    val negotiationWaitMillis: Long = 0,
    val negotiationTimeoutSeconds: Int = 300,
    val telemetryWriteError: String? = null,
    val activeController: String = "AGENT",
    val playerControllers: Map<String, String> = emptyMap(),
    val warDeclarationTurn: Int? = null,
    val busyOperation: String? = null,
    val busyElapsedMs: Long = 0,
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
    private data class Lease(val id: String, val expires: Long)
    private val lease = AtomicReference<Lease?>()
    private val expirationPending = AtomicBoolean()
    private val leaseId get() = lease.get()?.id
    private val expiresAt get() = lease.get()?.expires ?: 0L
    private data class StatusSnapshot(val status: OperatorStatus, val completedWait: Long, val openedAt: Long, val deadline: Long?)

    @Volatile private var committedStatus: StatusSnapshot? = null
    private var activation: Activation? = null
    private val communicationSessions = mutableMapOf<String, Pair<String, PlayerSession>>()
    private val configuredCredentials = mutableSetOf<String>()
    private val host = PlayerHost(runtime)

    init {
        runtime.accessGame { game ->
            check(runtime.scheduler == null) { "Operator control is already configured" }
            runtime.scheduler = this
            publishStatus(game)
        }
    }

    private class Activation(
        val id: String,
        val civilization: String,
        val handoff: Long,
        val token: String,
        val session: PlayerSession,
    )

    internal fun handle(request: OperatorRequest): OperatorResponse {
        validate(request)
        if (request.op in setOf("status", "heartbeat")) {
            if (runtime.isClosed) throw RuntimeClosedException()
            if (request.op == "heartbeat") {
                if (runtime.mutationFailure != null) throw OperatorFailure("GAME_HALTED")
                renew(request.leaseId)
            }
            return OperatorResponse(fastStatus(), if (request.op == "heartbeat") leaseId else null)
        }
        return runtime.accessOperatorGame { game ->
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
                        runtime.pauseReason = request.pauseReason
                        runtime.communicationPaused = true
                        runtime.pause()
                    }

                    "unbarrier" -> {
                        requireLease(request.leaseId)
                        if (runtime.communicationState.restorePending) throw OperatorFailure("RESTORE_INCOMPLETE")
                        runtime.communicationPaused = false
                        runtime.pauseReason = null
                        runtime.communication.deliverQueuedMessages()
                        runtime.commitCheckpoint()
                    }

                    "snapshot" -> {
                        requireLease(request.leaseId)
                        return@accessOperatorGame OperatorResponse(status(game), checkpoint = runtime.turnSnapshot())
                    }

                    "restore" -> {
                        requireLease(request.leaseId)
                        activation = null
                        communicationSessions.clear()
                        runtime.restoreTurn(checkNotNull(request.checkpoint), checkNotNull(request.epoch), configuredCredentials)
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
                            RotatedPlayerCredential(player.civID, token, controller(player))
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
                        if (checkNotNull(request.maxInvocations) < state.invocations || checkNotNull(request.maxConcurrent) !in 1..64 || checkNotNull(request.maxPerCivilization) !in 1..3) throw OperatorFailure("INVALID_ARGUMENT")
                        state.maxInvocations = request.maxInvocations
                        state.maxConcurrent = request.maxConcurrent
                        state.maxPerCivilization = request.maxPerCivilization
                        runtime.commitCheckpoint()
                    }

                    "acquire" -> {
                        if (!runtime.durableRecovery) throw OperatorFailure("DURABLE_CHECKPOINT_REQUIRED")
                        if (leaseId != null && leaseId != request.leaseId) throw OperatorFailure("LEASE_BUSY")
                        if (leaseId == null) {
                            lease.set(Lease(checkNotNull(request.leaseId), clockMillis() + LEASE_MILLIS))
                            runtime.scheduled = true
                            activation = null
                            renew()
                            runtime.pause()
                        } else {
                            renew()
                        }
                    }

                    "activate" -> {
                        requireLease(request.leaseId)
                        if (runtime.communicationState.restorePending || runtime.communicationPaused) throw OperatorFailure("RESTORE_INCOMPLETE")
                        if (request.epoch != runtime.epoch || request.serverInstance != serverInstance || request.handoff != runtime.handoff || request.civilization != game.currentPlayer) {
                            throw OperatorFailure("STALE_TURN")
                        }
                        if (finished(game) || game.currentPlayerCiv.isDefeated()) throw OperatorFailure("PLAYER_UNAVAILABLE")
                        val previous = activation
                        if (previous != null && previous.id == request.activationId) {
                            if (previous.civilization != request.civilization || previous.handoff != request.handoff) throw OperatorFailure("ACTIVATION_CONFLICT")
                        } else {
                            val id = checkNotNull(request.activationId)
                            val civilization = checkNotNull(request.civilization)
                            val session = host.bindPlayer(civilization)
                            activation = Activation(id, civilization, runtime.handoff, newToken(), session)
                            runtime.telemetry.beginTurn(game, id)
                        }
                        renew()
                        runtime.resume()
                        val current = checkNotNull(activation)
                        return@accessOperatorGame OperatorResponse(status(game), leaseId, current.token, current.id)
                    }

                    "pause" -> {
                        if (request.leaseId != null) {
                            requireLease(request.leaseId)
                        } else {
                            lease.set(null)
                            runtime.communicationPaused = true
                        }
                        activation = null
                        runtime.pause()
                    }

                    "release" -> {
                        requireLease(request.leaseId)
                        activation = null
                        lease.set(null)
                        runtime.pause()
                    }

                    "manual" -> {
                        if (leaseId != null) throw OperatorFailure("LEASE_BUSY")
                        if (runtime.communicationState.restorePending) throw OperatorFailure("RESTORE_INCOMPLETE")
                        activation = null
                        runtime.scheduled = false
                        runtime.communicationPaused = false
                        runtime.pauseReason = null
                        runtime.communication.deliverQueuedMessages()
                        runtime.resume()
                    }
                }
                OperatorResponse(status(game), leaseId)
            } finally {
                // All state and credential changes precede the wake, even for early returns.
                runtime.communicationChanges.invalidate()
                if (runtime.mutationFailure == null) publishStatus(game)
            }
        }
    }

    /** Called while holding the executor lock, including immediately before a player operation. */
    internal fun expireIfNeeded() {
        liveLease()
        if (!expirationPending.getAndSet(false)) return
        // A fast-path expiry notification may arrive after a new acquisition or
        // a switch to manual play. Those operations already fenced the old grant.
        if (lease.get() != null || !runtime.scheduled) return
        activation = null
        runtime.communicationChanges.invalidate()
        if (runtime.mutationFailure == null) runtime.pause()
    }

    /** Expiry fences authority immediately; game cleanup runs on the next executor access. */
    private fun liveLease(): Lease? {
        while (true) {
            val current = lease.get() ?: return null
            if (clockMillis() < current.expires) return current
            if (lease.compareAndSet(current, null)) {
                expirationPending.set(true)
                runtime.communicationChanges.invalidate()
                return null
            }
        }
    }

    internal fun publishStatus(game: GameInfo) {
        val trades = runtime.communicationState.trades
        val proposal = trades.activeProposal?.let { trades.proposals.getValue(it).last() }
        committedStatus = StatusSnapshot(status(game), trades.completedWaitMillis, trades.openedAtMillis, proposal?.deadlineMillis)
    }

    private fun fastStatus(): OperatorStatus {
        val current = liveLease()
        val snapshot = checkNotNull(committedStatus)
        val saved = snapshot.status
        val busy = runtime.currentOperation
        return saved.copy(
            phase = when {
                runtime.mutationFailure != null -> "HALTED"
                current == null && saved.scheduled && saved.phase == "ACTIVE" -> "PAUSED"
                else -> saved.phase
            },
            leaseId = current?.id,
            leaseExpiresInMs = current?.let { (it.expires - clockMillis()).coerceAtLeast(0) } ?: 0,
            modelUsage = runtime.telemetry.usageSnapshot,
            modelInvocations = runtime.telemetry.invocationsSnapshot,
            negotiationWaitMillis = snapshot.completedWait + (snapshot.deadline?.let { (minOf(runtime.clockMillis(), it) - snapshot.openedAt).coerceAtLeast(0) } ?: 0),
            telemetryWriteError = runtime.telemetry.monitoringError,
            busyOperation = busy?.first,
            busyElapsedMs = busy?.let { (System.nanoTime() - it.second) / 1_000_000 } ?: 0,
        )
    }

    internal fun manualSession(session: PlayerSession, token: String? = null): PlayerSession {
        val boundEpoch = runtime.epoch
        val credential = token?.let(::credentialHash)
        if (credential != null) configuredCredentials.add(credential)
        return GuardedSession(session) { mutation ->
            if (runtime.epoch != boundEpoch || credential in runtime.communicationState.revokedCredentials || (mutation && runtime.scheduled)) deny()
        }
    }

    /** Local desktop authority is bound to the same scheduler activation as a remote player. */
    internal fun humanCanAct(civilization: String, game: GameInfo): Boolean {
        val current = activation
        return civilization in runtime.communicationState.humanPlayers && current != null &&
            current.civilization == civilization && current.handoff == runtime.handoff &&
            leaseId != null && clockMillis() < expiresAt && !runtime.isPaused() && !runtime.communicationPaused &&
            !runtime.communicationState.restorePending && runtime.mutationFailure == null &&
            runtime.communicationState.trades.activeProposal == null &&
            game.currentPlayer == civilization && !finished(game)
    }

    private fun controller(civilization: com.unciv.logic.civilization.Civilization) = when {
        civilization.civID in runtime.communicationState.humanPlayers -> "HUMAN"
        civilization.isHuman() -> "AGENT"
        else -> "BUILT_IN_AI"
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

        override fun nativeAdvice(request: uncivai.player.NativeAdviceRequest): uncivai.player.NativeAdvice {
            guarded { Unit }
            // The selector waits on immutable work outside the live-game lock.
            val result = delegate.nativeAdvice(request)
            return guarded { result }
        }
        override fun inspect(request: uncivai.player.PlayerInspectionRequest) = guarded { delegate.inspect(request) }
        override fun missionPreview(request: uncivai.player.MissionPreviewRequest) = guarded { delegate.missionPreview(request) }
        override fun researchDetails(names: List<String>) = guarded { delegate.researchDetails(names) }
        override fun policyDetails(names: List<String>) = guarded { delegate.policyDetails(names) }
        override fun victoryProgress(request: uncivai.player.VictoryProgressRequest) = guarded { delegate.victoryProgress(request) }
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
        activeController = controller(game.currentPlayerCiv),
        playerControllers = game.civilizations.filter { it.isHuman() && it.isMajorCiv() }
            .associate { it.civID to controller(it) },
        warDeclarationTurn = runtime.communicationState.warDeclarationTurn,
        negotiationWaitMillis = runtime.communication.trades.waitMillis(),
        negotiationTimeoutSeconds = runtime.communicationState.trades.timeoutSeconds,
        scheduled = runtime.scheduled,
        leaseId = leaseId,
        leaseExpiresInMs = if (leaseId == null) 0 else (expiresAt - clockMillis()).coerceAtLeast(0),
        modelInvocations = runtime.communicationState.invocations,
        telemetryWriteError = runtime.telemetry.monitoringError,
        modelUsage = runtime.telemetry.usageSnapshot,
    )

    private fun finished(game: GameInfo) = gameFinished(game)

    private fun requireLease(id: String?) {
        if (id == null || id != liveLease()?.id) throw OperatorFailure("LEASE_LOST")
    }

    private fun renew(id: String? = leaseId) {
        while (true) {
            val current = lease.get() ?: throw OperatorFailure("LEASE_LOST")
            val now = clockMillis()
            if (now >= current.expires) {
                liveLease()
                continue
            }
            if (id == null || id != current.id) throw OperatorFailure("LEASE_LOST")
            if (lease.compareAndSet(current, current.copy(expires = now + LEASE_MILLIS))) return
        }
    }

    private fun validate(request: OperatorRequest) {
        val keys = when (request.op) {
            "status", "results", "manual" -> emptySet()
            "acquire", "heartbeat", "release" -> setOf("leaseId")
            "pause" -> if (request.leaseId == null) emptySet() else setOf("leaseId")
            "activate" -> setOf("leaseId", "epoch", "serverInstance", "handoff", "civilization", "activationId")
            "connect" -> setOf("leaseId", "epoch", "civilization")
            "rotate_players", "restore_complete" -> setOf("leaseId", "epoch")
            "job_limits" -> setOf("leaseId", "maxInvocations", "maxConcurrent", "maxPerCivilization")
            "barrier" -> setOf("leaseId") + if (request.pauseReason != null) setOf("pauseReason") else emptySet()
            "unbarrier", "snapshot" -> setOf("leaseId")
            "restore" -> setOf("leaseId", "epoch", "checkpoint")
            else -> throw OperatorFailure("INVALID_ARGUMENT")
        }
        val values = mapOf("leaseId" to request.leaseId, "epoch" to request.epoch, "serverInstance" to request.serverInstance, "handoff" to request.handoff, "civilization" to request.civilization, "activationId" to request.activationId, "maxInvocations" to request.maxInvocations, "maxConcurrent" to request.maxConcurrent, "maxPerCivilization" to request.maxPerCivilization, "checkpoint" to request.checkpoint, "pauseReason" to request.pauseReason)
        if (request.pauseReason != null && request.pauseReason != "STRATEGIC_REVIEW") throw OperatorFailure("INVALID_ARGUMENT")
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
