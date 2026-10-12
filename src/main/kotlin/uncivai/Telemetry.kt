package uncivai

import com.unciv.logic.GameInfo
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import uncivai.player.gameFinished
import java.nio.file.Path
import java.util.UUID

/** Transport instrumentation is deliberately absent from the public player capability interface. */
internal interface PlayerApiAccounting {
    fun beginApiCall(tool: String, arguments: JsonObject?): String?
    fun finishApiCall(id: String, outcome: String)
}

/** Accounting is operator data. It survives game rollback and is never a player observation. */
@Serializable
internal data class TelemetryState(
    val turns: MutableMap<String, TurnMetrics> = linkedMapOf(),
    val windows: MutableMap<String, ApiWindow> = linkedMapOf(),
    val pendingCalls: MutableMap<String, PendingApiCall> = mutableMapOf(),
    var result: NativeGameResult? = null,
    val priorResults: MutableList<NativeGameResult> = mutableListOf(),
    var startedAtMs: Long = System.currentTimeMillis(),
    val hostRevision: String = System.getenv("UNCIV_HOST_REVISION") ?: "unknown",
    val engineRevision: String = System.getenv("UNCIV_ENGINE_REVISION") ?: "unknown",
)

@Serializable
internal data class ApiCounts(var calls: Long = 0, var succeeded: Long = 0, var errors: Long = 0, var replayed: Long = 0, var incomplete: Long = 0, var elapsedMs: Long = 0)

/** Edit outcomes are independent of transport success, and exclude replayed batches. */
@Serializable
internal data class MemoryEditCounts(
    val batches: Long = 0,
    val accepted: Long = 0,
    val rejected: Long = 0,
    val rejectedBatches: Long = 0,
    val rejectionReasons: Map<String, Long> = emptyMap(),
    val complete: Boolean = true,
) {
    fun valid() = listOf(batches, accepted, rejected, rejectedBatches).all { it >= 0 } &&
        rejectedBatches <= batches && rejectedBatches <= rejected &&
        rejectionReasons.values.all { it >= 0 } && rejectionReasons.values.sum() == rejected

    fun follows(prior: MemoryEditCounts) = batches >= prior.batches && accepted >= prior.accepted &&
        rejected >= prior.rejected && rejectedBatches >= prior.rejectedBatches &&
        (!prior.complete || complete) && prior.rejectionReasons.all { (reason, count) -> (rejectionReasons[reason] ?: 0) >= count }
}

@Serializable
internal data class ApiWindow(val epoch: String, val handoff: Long, val gameTurn: Int, val activeCivilization: String, val actors: MutableMap<String, MutableMap<String, ApiCounts>> = linkedMapOf())

@Serializable
internal data class PendingApiCall(val window: String, val civilization: String, val keys: List<String>, val replay: Boolean)

@Serializable
internal data class TurnMetrics(
    val epoch: String,
    val handoff: Long,
    val gameTurn: Int,
    val civilization: String,
    val startedAtMs: Long,
    var endedAtMs: Long? = null,
    var status: String = "active",
    var activeMs: Long = 0,
    var timingComplete: Boolean = true,
    val activationIds: MutableSet<String> = linkedSetOf(),
    val nativeActions: MutableMap<String, Long> = linkedMapOf(),
)

/** Cumulative service counters. Sequence numbers make retransmission and late delivery idempotent. */
@Serializable
internal data class JobTelemetry(
    val sequence: Long,
    val phase: String,
    val startedAtMs: Long,
    val endedAtMs: Long? = null,
    val elapsedMs: Long,
    val negotiationWaitMs: Long,
    val usageReported: Boolean,
    val usageComplete: Boolean,
    val model: String? = null,
    val reasoningEffort: String? = null,
    val apiCalls: Map<String, ApiCounts> = emptyMap(),
    val gameplayElapsedMs: Long? = null,
    val finalizationElapsedMs: Long? = null,
    val memoryEdits: MemoryEditCounts? = null,
    val provider: String? = null,
    val modelRequests: Long = 0,
)

@Serializable
internal data class PlayerScore(val civilization: String, val controller: String, val defeated: Boolean, val score: Int, val rawScore: Double, val breakdown: Map<String, Double>)

@Serializable
internal data class NativeGameResult(val epoch: String, val recordedAtMs: Long, val winner: String?, val victoryType: String?, val victoryTurn: Int?, val scores: List<PlayerScore>)

internal class Telemetry(private val runtime: GameExecutor) {
    private val state get() = runtime.communicationState.telemetry
    private var activeKey: String? = null
    private var activeNanos = 0L
    private val callNanos = mutableMapOf<String, Long>()
    private var monitoringDirty = true
    private val diplomacy = DiplomacyMonitoring(runtime)
    private val dirtyTurns = mutableSetOf<String>()
    private val dirtyJobs = mutableSetOf<String>()
    private val dirtyWindows = mutableSetOf<String>()
    private var lastPaused: Boolean? = null
    private val dirtyPending = mutableSetOf<String>()
    private var lastMetadata: JsonObject? = null
    private var publisher: AccountingMonitoring? = null
    private val reportedUsage = mutableMapOf<String, ModelUsage?>()
    private val usageTotals = mutableMapOf("input_tokens" to 0L, "cached_input_tokens" to 0L, "output_tokens" to 0L, "reasoning_output_tokens" to 0L)

    @Volatile internal var usageSnapshot: Map<String, Long> = emptyMap()
        private set

    @Volatile internal var invocationsSnapshot: Long = 0
        private set
    internal val monitoringError get() = publisher?.error

    fun initialize(store: CheckpointStore) {
        publisher = AccountingMonitoring(store.path.parent.resolve("runner/monitoring.sqlite3"))
        val ledger = checkNotNull(store.accounting)
        ledger.onCommitted = { publisher!!.enqueue(it) }
        publisher!!.enqueue(ledger.rows())
        runtime.communicationState.jobs.keys.forEach(::updateUsage)
        invocationsSnapshot = runtime.communicationState.invocations
    }

    private fun updateUsage(id: String) {
        val next = runtime.communicationState.jobs[id]?.usage
        val prior = reportedUsage.put(id, next)
        usageTotals.keys.forEach { key -> usageTotals[key] = usageTotals.getValue(key) + (next?.tokens(key) ?: 0) - (prior?.tokens(key) ?: 0) }
        usageSnapshot = usageTotals.toMap()
    }

    fun communicationChanged() {
        monitoringDirty = true
    }

    private fun key() = "${runtime.epoch}/${runtime.handoff}"

    fun changed(jobId: String) {
        updateUsage(jobId)
        invocationsSnapshot = runtime.communicationState.invocations
        monitoringDirty = true
        dirtyJobs.add(jobId)
        runtime.communicationState.jobs[jobId]?.let { job ->
            if (job.handoff != null) {
                dirtyTurns.add("${job.epoch}/${job.handoff}")
            } else {
                state.turns.filterValues { belongsToTurn(job, it) }.keys.forEach(dirtyTurns::add)
            }
        }
    }

    private fun belongsToTurn(job: CommunicationJob, turn: TurnMetrics): Boolean = job.purpose in setOf("gameplay", "review") && job.epoch == turn.epoch && job.civilization == turn.civilization &&
        (job.handoff == turn.handoff || (job.purpose == "review" && job.handoff == null && job.gameTurn == 0 && turn.gameTurn == 0))

    fun beginTurn(game: GameInfo, activationId: String) {
        monitoringDirty = true
        val turn = state.turns.getOrPut(key()) { TurnMetrics(runtime.epoch, runtime.handoff, game.turns, game.currentPlayer, System.currentTimeMillis()) }
        turn.activationIds.add(activationId)
        dirtyTurns.add(key())
    }

    fun nativeAction(kind: String) {
        state.turns[key()]?.let { turn ->
            turn.nativeActions[kind] = (turn.nativeActions[kind] ?: 0) + 1
            dirtyTurns.add(key())
            monitoringDirty = true
        }
    }

    fun restored() {
        monitoringDirty = true
        activeKey = null
        state.turns.filterValues { it.endedAtMs == null }.forEach { (id, turn) ->
            turn.timingComplete = false
            dirtyTurns.add(id)
        }
        dirtyPending.addAll(state.pendingCalls.keys)
        for (pending in state.pendingCalls.values) {
            dirtyWindows.add(pending.window)
            for (name in pending.keys) state.windows.getValue(pending.window).actors.getValue(pending.civilization).getValue(name).incomplete++
        }
        state.pendingCalls.clear()
        callNanos.clear()
    }

    fun synchronize(game: GameInfo) {
        val now = System.nanoTime()
        activeKey?.let {
            state.turns[it]?.let { turn ->
                turn.activeMs += ((now - activeNanos) / 1_000_000).coerceAtLeast(0)
                dirtyTurns.add(it)
            }
        }
        val finished = gameFinished(game)
        state.result?.takeIf { it.epoch != runtime.epoch }?.let {
            state.priorResults.add(it)
            state.result = null
        }
        val wall = System.currentTimeMillis()
        if (lastPaused != runtime.isPaused()) monitoringDirty = true
        lastPaused = runtime.isPaused()
        for ((id, turn) in state.turns) {
            if (turn.endedAtMs == null && (turn.epoch != runtime.epoch || turn.handoff != runtime.handoff || finished)) {
                turn.endedAtMs = wall
                dirtyTurns.add(id)
                monitoringDirty = true
                turn.status = if (turn.epoch != runtime.epoch) {
                    "restored"
                } else if (finished) {
                    "game_finished"
                } else {
                    "completed"
                }
            }
        }
        activeKey = key().takeIf { it in state.turns && !runtime.isPaused() && !finished }
        activeNanos = now
        if (finished && state.result == null) {
            monitoringDirty = true
            val victory = game.victoryData
            state.result = NativeGameResult(runtime.epoch, wall, victory?.winningCiv, victory?.victoryType, victory?.victoryTurn, scores(game))
        }
    }

    fun startCall(civilization: String, tool: String, arguments: JsonObject?): String? = runtime.accessOperatorGame { game ->
        // A halted game's in-memory mutation may differ from disk. Never checkpoint it for diagnostics.
        if (runtime.mutationFailure != null) return@accessOperatorGame null
        val args = if (tool == "player_communication") arguments?.get("request") as? JsonObject else arguments
        fun field(name: String) = (args?.get(name) as? JsonPrimitive)?.content?.takeIf { it.length <= 128 }
        if (tool == "player_communication" && field("op") in setOf("job_claim", "job_report", "job_release", "job_heartbeat")) monitoringDirty = true
        val names = buildList {
            add("tool:$tool")
            if (tool == "player_command") field("kind")?.let { add("command:$it") }
            if (tool == "player_communication") field("op")?.let { add("communication:$it") }
        }
        val requestId = field("requestId")
        val replay = requestId != null && when (tool) {
            "player_command" -> runtime.turnSessions[civilization]?.receipts?.containsKey(requestId) == true
            "player_communication" -> runtime.communicationState.requests.containsKey("$civilization:$requestId")
            else -> false
        }
        val window = state.windows.getOrPut(key()) { ApiWindow(runtime.epoch, runtime.handoff, game.turns, game.currentPlayer) }
        dirtyWindows.add(key())
        dirtyTurns.add(key())
        val counts = window.actors.getOrPut(civilization) { linkedMapOf() }
        names.forEach { counts.getOrPut(it) { ApiCounts() }.calls++ }
        val id = UUID.randomUUID().toString()
        state.pendingCalls[id] = PendingApiCall(key(), civilization, names, replay)
        callNanos[id] = System.nanoTime()
        dirtyPending.add(id)
        runtime.commitAccounting()
        id
    }

    fun finishCall(id: String, outcome: String) = runtime.accessOperatorGame { game ->
        val pending = state.pendingCalls.remove(id) ?: return@accessOperatorGame
        if (runtime.isPaused() || gameFinished(game) || state.turns[pending.window]?.endedAtMs != null || pending.keys.any { it.startsWith("communication:job_") }) monitoringDirty = true
        dirtyWindows.add(pending.window)
        dirtyTurns.add(pending.window)
        val elapsed = callNanos.remove(id)?.let { (System.nanoTime() - it) / 1_000_000 } ?: 0
        for (name in pending.keys) {
            val counts = state.windows.getValue(pending.window).actors.getValue(pending.civilization).getValue(name)
            when (outcome) {
                "success" -> {
                    counts.succeeded++
                    if (pending.replay) counts.replayed++
                }

                "error" -> counts.errors++

                else -> counts.incomplete++
            }
            counts.elapsedMs += elapsed.coerceAtLeast(0)
        }
        dirtyPending.add(id)
        if (runtime.mutationFailure == null) runtime.commitAccounting()
    }

    private fun scores(game: GameInfo) = game.civilizations.filter { it.isMajorCiv() }.map { player ->
        PlayerScore(
            player.civID,
            if (player.isAI()) {
                "BUILT_IN_AI"
            } else if (player.civID in runtime.communicationState.humanPlayers) {
                "HUMAN"
            } else {
                "AGENT"
            },
            player.isDefeated(),
            player.calculateTotalScore().toInt(),
            player.calculateTotalScore(),
            player.calculateScoreBreakdown().toSortedMap(),
        )
    }

    /** Freeze only changed records while holding the executor; the writer consumes immutable JSON. */
    fun accountingBatch(game: GameInfo, includeGame: Boolean): AccountingBatch? {
        val rows = mutableListOf<AccountingRow>()
        val metadata = runtime.communicationState.accountingMetadata().recordJson()
        if (metadata != lastMetadata) rows.add(AccountingRow("meta", "accounting", metadata))
        dirtyJobs.forEach { id -> runtime.communicationState.jobs[id]?.let { rows.add(AccountingRow("jobs", id, it.recordJson())) } }
        dirtyTurns.forEach { id -> state.turns[id]?.let { rows.add(AccountingRow("turns", id, it.recordJson())) } }
        dirtyWindows.forEach { id -> state.windows[id]?.let { rows.add(AccountingRow("windows", id, it.recordJson())) } }
        dirtyPending.forEach { id -> rows.add(AccountingRow("pending", id, state.pendingCalls[id]?.recordJson())) }
        if (includeGame) {
            rows.add(
                AccountingRow(
                    "meta",
                    "game",
                    obj(
                        "schemaVersion" to 1, "matchId" to game.gameId, "epoch" to runtime.epoch,
                        "status" to if (state.result != null) {
                            "completed"
                        } else if (runtime.isPaused()) {
                            "incomplete"
                        } else {
                            "running"
                        },
                        "result" to state.result?.recordJson(), "scores" to (state.result?.scores ?: scores(game)).map { it.recordJson() },
                        "humanPlayers" to runtime.communicationState.humanPlayers.toList(),
                        "startedAtMs" to state.startedAtMs, "elapsedMs" to ((state.result?.recordedAtMs ?: System.currentTimeMillis()) - state.startedAtMs).coerceAtLeast(0),
                        "hostRevision" to state.hostRevision, "engineRevision" to state.engineRevision,
                        "seed" to game.tileMap.mapParameters.seed, "ruleset" to game.gameParameters.baseRuleset,
                        "speed" to game.gameParameters.speed, "difficulty" to game.gameParameters.difficulty,
                        "victoryTypes" to game.gameParameters.victoryTypes, "maxTurns" to game.gameParameters.maxTurns,
                    ),
                ),
            )
        }
        lastMetadata = metadata
        dirtyJobs.clear()
        dirtyTurns.clear()
        dirtyWindows.clear()
        dirtyPending.clear()
        return rows.takeIf { it.isNotEmpty() }?.let { AccountingBatch(rows = it) }
    }

    /** Queued copies survive a slow/failed dashboard write; restart rebuilds this projection. */
    fun publishDiplomacy() {
        if (!monitoringDirty && !diplomacy.epochChanged) return
        val batch = diplomacy.collect()
        publisher?.enqueueMonitoring(batch.records)
        diplomacy.committed(batch)
        monitoringDirty = false
    }

    fun flushMonitoring() {
        publisher?.flush()
    }
    fun close() {
        publisher?.close()
    }
}
