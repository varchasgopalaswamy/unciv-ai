package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import java.nio.file.Path
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** Derived publication owns its own thread and copies; it never acquires the game executor. */
internal class AccountingMonitoring(path: Path) : AutoCloseable {
    private val store = MonitoringStore(path)
    private val worker = Executors.newSingleThreadScheduledExecutor { task -> Thread(task, "host-monitoring").apply { isDaemon = true } }
    private val pending = ConcurrentHashMap<Pair<String, String>, AccountingRow>()
    private val direct = ConcurrentLinkedQueue<List<MonitoringStore.Record>>()
    private val jobs = mutableMapOf<String, CommunicationJob>()
    private val turns = mutableMapOf<String, TurnMetrics>()
    private val windows = mutableMapOf<String, ApiWindow>()
    private val byTurn = mutableMapOf<Triple<String?, Long?, String>, MutableMap<String, CommunicationJob>>()
    private val total = Totals()
    private val players = mutableMapOf<String, Totals>()
    private val dirtyTurns = mutableSetOf<String>()
    private val dirtyJobs = mutableSetOf<String>()
    private val dirtyWindows = mutableSetOf<String>()
    private val retained = mutableMapOf<Pair<String, String>, MonitoringStore.Record>()
    private var game: JsonObject? = null
    private var contextDirty = false

    @Volatile var error: String? = null
        private set
    private val timer = worker.scheduleWithFixedDelay({ publish() }, 1, 1, TimeUnit.SECONDS)

    fun enqueue(rows: List<AccountingRow>) {
        rows.filter { it.collection != "pending" }.forEach { pending[it.collection to it.key] = it }
    }

    fun enqueueMonitoring(rows: List<MonitoringStore.Record>) {
        direct.add(rows)
    }

    private fun accept(row: AccountingRow) {
        val payload = row.payload ?: return
        when (row.collection) {
            "meta" -> if (row.key == "game") {
                game = payload
                contextDirty = true
            }

            "jobs" -> {
                val next = Json.decodeFromJsonElement<CommunicationJob>(payload)
                jobs.put(row.key, next)?.let { old ->
                    total.job(old, -1)
                    players.getValue(old.civilization).job(old, -1)
                    byTurn[Triple(old.epoch, old.handoff, old.civilization)]?.remove(row.key)
                }
                total.job(next, 1)
                players.getOrPut(next.civilization) { Totals() }.job(next, 1)
                byTurn.getOrPut(Triple(next.epoch, next.handoff, next.civilization)) { mutableMapOf() }[row.key] = next
                dirtyJobs.add(row.key)
                if (next.handoff != null) {
                    dirtyTurns.add("${next.epoch}/${next.handoff}")
                } else {
                    turns.filterValues { it.epoch == next.epoch && it.gameTurn == 0 && it.civilization == next.civilization }.keys.forEach(dirtyTurns::add)
                }
            }

            "turns" -> {
                val next = Json.decodeFromJsonElement<TurnMetrics>(payload)
                turns.put(row.key, next)?.let { old -> players.getOrPut(old.civilization) { Totals() }.activeMs -= old.activeMs }
                players.getOrPut(next.civilization) { Totals() }.activeMs += next.activeMs
                dirtyTurns.add(row.key)
            }

            "windows" -> {
                val next = Json.decodeFromJsonElement<ApiWindow>(payload)
                windows.put(row.key, next)?.actors?.forEach { (actor, calls) ->
                    total.hostCalls.add(calls, -1)
                    players.getOrPut(actor) { Totals() }.hostCalls.add(calls, -1)
                }
                next.actors.forEach { (actor, calls) ->
                    total.hostCalls.add(calls, 1)
                    players.getOrPut(actor) { Totals() }.hostCalls.add(calls, 1)
                }
                dirtyWindows.add(row.key)
                dirtyTurns.add(row.key)
            }
        }
    }

    private fun publish() {
        try {
            for ((key, row) in pending) if (pending.remove(key, row)) accept(row)
            // Event insertion order is the dashboard's chronology. Coalesce state
            // rows only after draining committed batches in their original order.
            while (true) {
                val rows = direct.poll() ?: break
                rows.forEach { retained[it.collection to it.key] = it }
            }
            val context = game ?: return
            if (!contextDirty && dirtyTurns.isEmpty() && dirtyJobs.isEmpty() && dirtyWindows.isEmpty() && retained.isEmpty() && error == null) return
            val epoch = context.getValue("epoch").jsonPrimitive.content
            val matchId = context.getValue("matchId").jsonPrimitive.content
            val humans = context.getValue("humanPlayers").jsonArray.map { it.jsonPrimitive.content }.toSet()
            val updates = retained.values.toMutableList()
            for (id in dirtyTurns) {
                val turn = turns[id] ?: continue
                val attempts = buildMap {
                    putAll(byTurn[Triple(turn.epoch, turn.handoff, turn.civilization)].orEmpty().filterValues { it.purpose in setOf("gameplay", "economy", "military", "consultation", "review") })
                    if (turn.gameTurn == 0) putAll(byTurn[Triple(turn.epoch, null, turn.civilization)].orEmpty().filterValues { it.purpose == "review" && it.gameTurn == 0 })
                }
                val sums = Totals().apply { attempts.values.forEach { job(it, 1) } }
                val elapsed = turn.endedAtMs?.let { (it - turn.startedAtMs).coerceAtLeast(0) }
                val human = turn.civilization in humans
                val value = obj(
                    "schemaVersion" to 1, "matchId" to matchId, "turnId" to id, "turn" to turn.recordJson(),
                    "controller" to if (human) "HUMAN" else "AGENT", "usage" to sums.usage.json(), "jobs" to attempts.keys.toList(),
                    "attempts" to turn.activationIds.size, "modelRequests" to sums.requests,
                    "accountingComplete" to (turn.endedAtMs != null && (human || turn.activationIds.all { it in attempts }) && sums.usage.complete()),
                    "agentCalls" to sums.agentCalls.json(), "memoryEdits" to sums.memory.json(),
                    "hostCalls" to (windows[id]?.actors?.get(turn.civilization).orEmpty().mapValues { it.value.recordJson() }.let(::JsonObject)),
                    "elapsedMs" to elapsed, "inactiveOrRecoveryMs" to elapsed?.let { (it - turn.activeMs).coerceAtLeast(0) },
                    "jobElapsedMs" to sums.elapsed, "gameplayElapsedMs" to attempts.values.mapNotNull { it.telemetry?.gameplayElapsedMs }.takeIf { it.isNotEmpty() }?.sum(),
                    "finalizationElapsedMs" to attempts.values.mapNotNull { it.telemetry?.finalizationElapsedMs }.takeIf { it.isNotEmpty() }?.sum(),
                    "negotiationWaitMs" to attempts.values.sumOf { it.telemetry?.negotiationWaitMs ?: 0 },
                    "roleWaitMs" to attempts.values.sumOf { it.telemetry?.roleWaitMs ?: 0 },
                )
                updates.add(MonitoringStore.Record("turns", id, value, turn.epoch, turn.civilization, turn.gameTurn))
            }
            dirtyJobs.forEach { id -> jobs[id]?.let { job -> updates.add(MonitoringStore.Record("jobs", id, obj("schemaVersion" to 1, "matchId" to matchId, "jobId" to id, "job" to job.recordJson()), job.epoch, job.civilization, job.gameTurn)) } }
            dirtyWindows.forEach { id -> windows[id]?.let { window -> updates.add(MonitoringStore.Record("host-api", id, window.recordJson(), window.epoch, window.activeCivilization, window.gameTurn)) } }
            val result = JsonObject(
                context.filterKeys { it != "humanPlayers" } + obj(
                    "elapsedMs" to (((context["result"] as? JsonObject)?.get("recordedAtMs")?.jsonPrimitive?.long ?: System.currentTimeMillis()) - context.getValue("startedAtMs").jsonPrimitive.long).coerceAtLeast(0),
                    "usage" to total.usage.json(),
                    "modelJobs" to total.usage.count,
                    "modelRequests" to total.requests,
                    "agentCalls" to total.agentCalls.json(),
                    "memoryEdits" to total.memory.json(),
                    "hostCalls" to total.hostCalls.json(),
                    "players" to context.getValue("scores").jsonArray.map { score ->
                        val civ = score.jsonObject.getValue("civilization").jsonPrimitive.content
                        val sums = players[civ] ?: Totals()
                        obj(
                            "civilization" to civ, "usage" to sums.usage.json(), "modelJobs" to sums.usage.count, "modelRequests" to sums.requests,
                            "gameplayUsage" to sums.purposes.getOrPut("gameplay") { Usage() }.json(), "economyUsage" to sums.purposes.getOrPut("economy") { Usage() }.json(), "militaryUsage" to sums.purposes.getOrPut("military") { Usage() }.json(), "consultationUsage" to sums.purposes.getOrPut("consultation") { Usage() }.json(), "reviewUsage" to sums.purposes.getOrPut("review") { Usage() }.json(), "chatUsage" to sums.purposes.getOrPut("communication") { Usage() }.json(),
                            "agentCalls" to sums.agentCalls.json(), "memoryEdits" to sums.memory.json(), "hostCalls" to sums.hostCalls.json(), "activeMs" to sums.activeMs, "jobElapsedMs" to sums.elapsed,
                        )
                    },
                    "hostTimings" to JsonObject(HostTimings.snapshot()),
                ),
            )
            updates.add(MonitoringStore.Record("state", "result", result, epoch))
            HostTimings.measure("monitoring.publish") { store.write(updates) }
            dirtyTurns.clear()
            contextDirty = false
            dirtyJobs.clear()
            dirtyWindows.clear()
            retained.clear()
            error = null
        } catch (failure: Exception) {
            error = failure.javaClass.simpleName
        }
    }

    fun flush() {
        worker.submit { publish() }.get()
    }

    override fun close() {
        timer.cancel(false)
        try {
            flush()
        } finally {
            worker.shutdown()
        }
    }

    private class Calls {
        private val values = mutableMapOf<String, ApiCounts>()
        fun add(calls: Map<String, ApiCounts>, sign: Int) {
            calls.forEach { (key, v) ->
                values.getOrPut(key) { ApiCounts() }.let {
                    it.calls += sign * v.calls
                    it.succeeded += sign * v.succeeded
                    it.errors += sign * v.errors
                    it.replayed += sign * v.replayed
                    it.incomplete += sign * v.incomplete
                    it.elapsedMs += sign * v.elapsedMs
                }
            }
        }
        fun json() = JsonObject(values.filterValues { it.calls != 0L }.mapValues { it.value.recordJson() })
    }

    private class Usage {
        var count = 0L
        private var finished = 0L
        private val tokens = mutableMapOf("input_tokens" to 0L, "cached_input_tokens" to 0L, "output_tokens" to 0L, "reasoning_output_tokens" to 0L)
        fun add(job: CommunicationJob, sign: Int) {
            count += sign
            if (job.telemetry?.usageComplete == true) finished += sign
            tokens.keys.forEach { tokens[it] = tokens.getValue(it) + sign * (job.usage?.tokens(it) ?: 0L) }
        }
        fun complete() = count == finished
        fun json() = obj(*tokens.map { it.key to it.value }.toTypedArray(), "total_tokens" to tokens.getValue("input_tokens") + tokens.getValue("output_tokens"), "complete" to complete())
    }

    private class Memory {
        private var groups = 0L
        private var measured = 0L
        private var complete = 0L
        private var batches = 0L
        private var accepted = 0L
        private var rejected = 0L
        private var rejectedBatches = 0L
        private val reasons = mutableMapOf<String, Long>()
        fun add(value: MemoryEditCounts?, sign: Int) {
            groups += sign
            if (value == null) return
            measured += sign
            if (value.complete) complete += sign
            batches += sign * value.batches
            accepted += sign * value.accepted
            rejected += sign * value.rejected
            rejectedBatches += sign * value.rejectedBatches
            value.rejectionReasons.forEach { (key, amount) -> reasons[key] = (reasons[key] ?: 0) + sign * amount }
        }
        fun json() = if (measured == 0L) null else MemoryEditCounts(batches, accepted, rejected, rejectedBatches, reasons.filterValues { it != 0L }, complete == groups).recordJson()
    }

    private class Totals {
        val usage = Usage()
        val purposes = mutableMapOf<String, Usage>()
        val agentCalls = Calls()
        val hostCalls = Calls()
        val memory = Memory()
        var activeMs = 0L
        var requests = 0L
        var elapsed = 0L
        fun job(job: CommunicationJob, sign: Int) {
            usage.add(job, sign)
            purposes.getOrPut(job.purpose) { Usage() }.add(job, sign)
            requests += sign * (job.telemetry?.modelRequests ?: 0)
            elapsed += sign * (job.telemetry?.elapsedMs ?: 0)
            agentCalls.add(job.telemetry?.apiCalls.orEmpty(), sign)
            memory.add(job.telemetry?.memoryEdits, sign)
        }
    }
}
