package uncivai

import com.unciv.logic.GameInfo
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import uncivai.player.gameFinished
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.nio.file.StandardOpenOption
import java.nio.file.attribute.PosixFilePermissions
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
    private var exportDirty = true
    private var lastPaused: Boolean? = null
    internal var exportError: String? = null
        private set

    private fun key() = "${runtime.epoch}/${runtime.handoff}"

    fun changed() {
        exportDirty = true
    }

    fun beginTurn(game: GameInfo, activationId: String) {
        exportDirty = true
        val turn = state.turns.getOrPut(key()) { TurnMetrics(runtime.epoch, runtime.handoff, game.turns, game.currentPlayer, System.currentTimeMillis()) }
        turn.activationIds.add(activationId)
    }

    fun restored() {
        exportDirty = true
        activeKey = null
        state.turns.values.filter { it.endedAtMs == null }.forEach { it.timingComplete = false }
        for (pending in state.pendingCalls.values) {
            for (name in pending.keys) state.windows.getValue(pending.window).actors.getValue(pending.civilization).getValue(name).incomplete++
        }
        state.pendingCalls.clear()
        callNanos.clear()
    }

    fun synchronize(game: GameInfo) {
        val now = System.nanoTime()
        activeKey?.let { state.turns[it]?.let { turn -> turn.activeMs += ((now - activeNanos) / 1_000_000).coerceAtLeast(0) } }
        val finished = gameFinished(game)
        state.result?.takeIf { it.epoch != runtime.epoch }?.let {
            state.priorResults.add(it)
            state.result = null
        }
        val wall = System.currentTimeMillis()
        if (lastPaused != runtime.isPaused()) exportDirty = true
        lastPaused = runtime.isPaused()
        for ((id, turn) in state.turns) {
            if (turn.endedAtMs == null && (turn.epoch != runtime.epoch || turn.handoff != runtime.handoff || finished)) {
                turn.endedAtMs = wall
                exportDirty = true
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
            exportDirty = true
            val victory = game.victoryData
            state.result = NativeGameResult(runtime.epoch, wall, victory?.winningCiv, victory?.victoryType, victory?.victoryTurn, scores(game))
        }
    }

    fun startCall(civilization: String, tool: String, arguments: JsonObject?): String? = runtime.accessOperatorGame { game ->
        // A halted game's in-memory mutation may differ from disk. Never checkpoint it for diagnostics.
        if (runtime.mutationFailure != null) return@accessOperatorGame null
        val args = if (tool == "player_communication") arguments?.get("request") as? JsonObject else arguments
        fun field(name: String) = (args?.get(name) as? JsonPrimitive)?.content?.takeIf { it.length <= 128 }
        if (tool == "player_communication" && field("op") in setOf("job_claim", "job_report", "job_release", "job_heartbeat")) exportDirty = true
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
        val counts = window.actors.getOrPut(civilization) { linkedMapOf() }
        names.forEach { counts.getOrPut(it) { ApiCounts() }.calls++ }
        val id = UUID.randomUUID().toString()
        state.pendingCalls[id] = PendingApiCall(key(), civilization, names, replay)
        callNanos[id] = System.nanoTime()
        runtime.commitCheckpoint()
        id
    }

    fun finishCall(id: String, outcome: String) = runtime.accessOperatorGame { game ->
        val pending = state.pendingCalls.remove(id) ?: return@accessOperatorGame
        if (runtime.isPaused() || gameFinished(game) || state.turns[pending.window]?.endedAtMs != null || pending.keys.any { it.startsWith("communication:job_") }) exportDirty = true
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
        if (runtime.mutationFailure == null) runtime.commitCheckpoint()
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

    private fun usage(jobs: Collection<CommunicationJob>): JsonObject {
        val keys = listOf("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
        val totals = keys.associateWith { name -> jobs.sumOf { it.usage?.tokens(name) ?: 0L } }
        return obj(*totals.map { it.key to it.value }.toTypedArray(), "total_tokens" to (totals.getValue("input_tokens") + totals.getValue("output_tokens")), "complete" to jobs.all { it.telemetry?.usageComplete == true })
    }

    private fun aggregateCalls(groups: Collection<Map<String, ApiCounts>>): JsonObject {
        val totals = linkedMapOf<String, ApiCounts>()
        groups.forEach { group ->
            group.forEach { (name, count) ->
                val total = totals.getOrPut(name) { ApiCounts() }
                total.calls += count.calls
                total.succeeded += count.succeeded
                total.errors += count.errors
                total.replayed += count.replayed
                total.incomplete += count.incomplete
                total.elapsedMs += count.elapsedMs
            }
        }
        return JsonObject(totals.mapValues { it.value.recordJson() })
    }

    /** Exports are replaceable views; the checkpoint is the commit authority. */
    fun export(game: GameInfo, checkpoint: Path) {
        if (!exportDirty && exportError == null) return
        try {
            val directory = checkpoint.parent.resolve("telemetry")
            Files.createDirectories(directory)
            Files.setPosixFilePermissions(directory, PosixFilePermissions.fromString("rwx------"))
            val jobs = runtime.communicationState.jobs
            val turns = state.turns.map { (id, turn) ->
                val attempts = jobs.filter { (_, job) -> job.purpose == "gameplay" && job.epoch == turn.epoch && job.handoff == turn.handoff && job.civilization == turn.civilization }
                val elapsed = turn.endedAtMs?.let { (it - turn.startedAtMs).coerceAtLeast(0) }
                obj(
                    "schemaVersion" to 1, "matchId" to game.gameId, "turnId" to id, "turn" to turn.recordJson(),
                    "usage" to usage(attempts.values), "jobs" to attempts.keys.toList(), "attempts" to turn.activationIds.size,
                    "accountingComplete" to (turn.endedAtMs != null && turn.activationIds.all { activation -> attempts[activation]?.telemetry?.usageComplete == true }),
                    "agentCalls" to aggregateCalls(attempts.values.map { it.telemetry?.apiCalls.orEmpty() }),
                    "hostCalls" to aggregateCalls(listOf(state.windows[id]?.actors?.get(turn.civilization).orEmpty())),
                    "elapsedMs" to elapsed, "inactiveOrRecoveryMs" to elapsed?.let { (it - turn.activeMs).coerceAtLeast(0) },
                    "jobElapsedMs" to attempts.values.sumOf { it.telemetry?.elapsedMs ?: 0 },
                    "gameplayElapsedMs" to attempts.values.mapNotNull { it.telemetry?.gameplayElapsedMs }.takeIf { it.isNotEmpty() }?.sum(),
                    "finalizationElapsedMs" to attempts.values.mapNotNull { it.telemetry?.finalizationElapsedMs }.takeIf { it.isNotEmpty() }?.sum(),
                    "negotiationWaitMs" to attempts.values.sumOf { it.telemetry?.negotiationWaitMs ?: 0 },
                )
            }
            write(directory.resolve("turns.jsonl"), turns.joinToString("") { "$it\n" })
            write(directory.resolve("jobs.jsonl"), jobs.map { (id, job) -> obj("schemaVersion" to 1, "matchId" to game.gameId, "jobId" to id, "job" to job.recordJson()) }.joinToString("") { "$it\n" })
            write(directory.resolve("host-api.jsonl"), state.windows.values.joinToString("") { "${it.recordJson()}\n" })
            val result = obj(
                "schemaVersion" to 1, "matchId" to game.gameId, "epoch" to runtime.epoch,
                "status" to if (state.result != null) {
                    "completed"
                } else if (runtime.isPaused()) {
                    "incomplete"
                } else {
                    "running"
                },
                "result" to state.result?.recordJson(), "scores" to (state.result?.scores ?: scores(game)).map { it.recordJson() },
                "usage" to usage(jobs.values), "modelJobs" to jobs.size,
                "agentCalls" to aggregateCalls(jobs.values.map { it.telemetry?.apiCalls.orEmpty() }),
                "hostCalls" to aggregateCalls(state.windows.values.flatMap { it.actors.values }),
                "players" to game.civilizations.filter { it.isMajorCiv() }.map { player ->
                    val owned = jobs.values.filter { it.civilization == player.civID }
                    obj(
                        "civilization" to player.civID, "usage" to usage(owned), "modelJobs" to owned.size,
                        "gameplayUsage" to usage(owned.filter { it.purpose == "gameplay" }), "chatUsage" to usage(owned.filter { it.purpose == "communication" }),
                        "agentCalls" to aggregateCalls(owned.map { it.telemetry?.apiCalls.orEmpty() }),
                        "hostCalls" to aggregateCalls(state.windows.values.mapNotNull { it.actors[player.civID] }),
                        "activeMs" to state.turns.values.filter { it.civilization == player.civID }.sumOf { it.activeMs },
                        "jobElapsedMs" to owned.sumOf { it.telemetry?.elapsedMs ?: 0 },
                    )
                },
                "startedAtMs" to state.startedAtMs, "elapsedMs" to ((state.result?.recordedAtMs ?: System.currentTimeMillis()) - state.startedAtMs).coerceAtLeast(0),
                "hostRevision" to state.hostRevision, "engineRevision" to state.engineRevision,
                "seed" to game.tileMap.mapParameters.seed, "ruleset" to game.gameParameters.baseRuleset,
                "speed" to game.gameParameters.speed, "difficulty" to game.gameParameters.difficulty,
                "victoryTypes" to game.gameParameters.victoryTypes, "maxTurns" to game.gameParameters.maxTurns,
            )
            write(directory.resolve("game-result.json"), "$result\n")
            exportError = null
            exportDirty = false
        } catch (failure: Exception) {
            // A failed export cannot undo a committed command. A later commit/restart repairs it.
            exportError = failure.javaClass.simpleName
        }
    }

    private fun write(path: Path, content: String) {
        val temp = Files.createTempFile(path.parent, ".telemetry-", ".tmp", PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rw-------")))
        try {
            FileChannel.open(temp, StandardOpenOption.WRITE).use { channel ->
                val bytes = ByteBuffer.wrap(content.toByteArray(Charsets.UTF_8))
                while (bytes.hasRemaining()) channel.write(bytes)
                channel.force(true)
            }
            Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            FileChannel.open(path.parent, StandardOpenOption.READ).use { it.force(true) }
        } finally {
            Files.deleteIfExists(temp)
        }
    }
}
