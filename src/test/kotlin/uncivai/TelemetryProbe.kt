package uncivai

import com.unciv.logic.VictoryData
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import uncivai.player.PlayerHost
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path
import java.sql.DriverManager

/** Real HTTPS calls, ordinary native AI turns, and telemetry after a checkpoint restart. */
object TelemetryProbe {
    private fun monitoring(root: Path, collection: String): List<JsonObject> = DriverManager.getConnection("jdbc:sqlite:${root.resolve("runner/monitoring.sqlite3")}").use { db ->
        db.prepareStatement("SELECT payload FROM monitor_records WHERE collection=? ORDER BY revision").use { statement ->
            statement.setString(1, collection)
            statement.executeQuery().use { rows ->
                buildList { while (rows.next()) add(Json.parseToJsonElement(rows.getString(1)).jsonObject) }
            }
        }
    }

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        val checkpoint = root.resolve("game.json")
        val civilizations = listOf("Rome", "Greece", "France")
        val tokens = civilizations.associateWith { it.first().toString().repeat(43) }
        lateinit var originalEpoch: String
        val edits = MemoryEditCounts(2, 3, 1, 1, mapOf("SOURCE_NOT_DELIVERED" to 1))
        val finalReport = JobTelemetry(2, "COMPLETED", 1000, 2000, 1000, 50, true, true, "gpt-5.6-luna", "medium", mapOf("tool:unit_details" to ApiCounts(3, 2, 1), "startup:player_turn" to ApiCounts(1, 1)), gameplayElapsedMs = 850, finalizationElapsedMs = 150, memoryEdits = edits)
        HeadlessRuntime.createVanillaGame(root.resolve("initial"), 42, 2, listOf("Egypt")).use { runtime ->
            check(runtime.game.getCivilization("Egypt").isAI())
            check(runtime.game.civilizations.count { it.isCityState } == 2)
            check(runtime.game.civilizations.filter { it.isHuman() && it.isMajorCiv() }.map { it.civID } == civilizations)
            runtime.executor.enableCheckpoints(checkpoint)
            originalEpoch = runtime.executor.epoch
            GameplayTestApi(runtime, root.resolve("tls"), tokens).use { api ->
                api.acquire()
                for (civilization in civilizations) {
                    val activation = api.activate()
                    val token = checkNotNull(activation.playerToken)
                    val job = checkNotNull(activation.activationId)
                    val credential = api.playerToken(civilization)
                    fun report(sequence: Long) = obj("op" to "job_report", "epoch" to originalEpoch, "job" to job, "usage" to ModelUsage(100, 20, 30, 10).recordJson(), "telemetry" to finalReport.copy(sequence = sequence).recordJson())
                    check(api.communicate(credential, obj("op" to "job_claim", "epoch" to originalEpoch, "job" to job, "purpose" to "gameplay")).getValue("granted").jsonPrimitive.boolean)
                    val before = api.turn(token)
                    val invalid = TurnCommandRequest("bad", before.epoch, before.revision, TurnCommandKind.FOUND_CITY, "missing-unit")
                    api.reject(token, invalid, "UNIT_UNAVAILABLE")
                    api.finishTurn(token)
                    // Native handoff happens before final provider usage. Delivery remains legal afterwards.
                    api.communicate(credential, report(2))
                    api.communicate(credential, report(2))
                    api.communicate(credential, report(1))
                    val decreased = JsonObject(report(3) + ("telemetry" to finalReport.copy(sequence = 3, memoryEdits = edits.copy(accepted = 1)).recordJson()))
                    check(api.communicate(credential, decreased).containsKey("error"))
                    val inconsistent = JsonObject(report(3) + ("telemetry" to finalReport.copy(sequence = 3, memoryEdits = edits.copy(rejected = 2)).recordJson()))
                    check(api.communicate(credential, inconsistent).containsKey("error"))
                    api.communicate(credential, obj("op" to "job_release", "epoch" to originalEpoch, "job" to job))
                    val metrics = runtime.executor.communicationState.telemetry.turns.getValue("$originalEpoch/${activation.status.handoff}")
                    check(metrics.endedAtMs != null && metrics.activeMs >= 0 && metrics.civilization == civilization)
                }
                check(api.status().turn == 1 && api.status().activeCivilization == "Rome")
                check(runtime.game.getCivilization("Egypt").cities.isNotEmpty()) { "Native AI did not take its ordinary turn" }
                val status = api.status()
                check(status.modelUsage["input_tokens"] == 300L && status.modelUsage["output_tokens"] == 90L)
                val turns = monitoring(root, "turns")
                check(turns.size == 3 && turns.all { it.getValue("usage").jsonObject.getValue("total_tokens").jsonPrimitive.long == 130L })
                check(turns.all { it.getValue("accountingComplete").jsonPrimitive.boolean })
                check(turns.all { it.getValue("gameplayElapsedMs").jsonPrimitive.long == 850L })
                check(turns.all { it.getValue("finalizationElapsedMs").jsonPrimitive.long == 150L })
                check(turns.all { it.getValue("agentCalls").jsonObject.getValue("startup:player_turn").jsonObject.getValue("calls").jsonPrimitive.long == 1L })
                check(turns.all { it.getValue("agentCalls").jsonObject.getValue("tool:unit_details").jsonObject.getValue("calls").jsonPrimitive.long == 3L })
                check(turns.all { it.getValue("memoryEdits") == edits.recordJson() })
                val allCalls = runtime.executor.communicationState.telemetry.windows.values.flatMap { it.actors.values }.flatMap { it.entries }
                check(allCalls.filter { it.key == "command:FOUND_CITY" }.sumOf { it.value.errors } == 3L)
                check(allCalls.filter { it.key == "command:END_TURN" }.sumOf { it.value.replayed } == 3L)
                check(!api.turn(tokens.getValue("Rome")).toString().contains("unit_details"))
                // Persist an uncompleted read; recovery must preserve its count as uncertain.
                runtime.executor.telemetry.startCall("Rome", "player_turn", obj())
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            val state = runtime.executor.communicationState
            check(state.jobs.size == 3 && state.jobs.values.sumOf { it.usage!!.inputTokens } == 300L)
            check(state.telemetry.pendingCalls.isEmpty())
            check(state.telemetry.windows.values.flatMap { it.actors.values }.sumOf { it["tool:player_turn"]?.incomplete ?: 0 } == 1L)
            // A restore starts a new history branch without refunding calls or tokens.
            runtime.executor.communicationPaused = true
            val snapshot = runtime.executor.turnSnapshot()
            runtime.executor.restoreTurn(snapshot, "restored-epoch")
            check(state.jobs.values.sumOf { it.usage!!.inputTokens } == 300L)
            check(runtime.executor.communicationState.telemetry.turns.size == 3)
            check(runtime.executor.communicationState.jobs.values.all { it.expires == 0L })
            // A background reviewer belongs to its original turn across an epoch change.
            runtime.executor.communicationPaused = false
            // A single-slot configuration serializes work but must still admit reviews.
            runtime.executor.communicationState.maxConcurrent = 1
            val parent = state.jobs.entries.single { it.value.civilization == "Rome" }.key
            val roman = PlayerHost(runtime).bindPlayer("Rome")
            check(roman.communication(obj("op" to "job_claim", "epoch" to runtime.epoch, "job" to "review-retry", "purpose" to "review", "parentJob" to parent)).getValue("granted").jsonPrimitive.boolean)
            val retry = runtime.executor.communicationState.jobs.getValue("review-retry")
            check(retry.epoch == originalEpoch && retry.handoff == state.jobs.getValue(parent).handoff)
            val reviewReport = finalReport.copy(apiCalls = emptyMap(), gameplayElapsedMs = 0, finalizationElapsedMs = 60, memoryEdits = MemoryEditCounts())
            roman.communication(obj("op" to "job_report", "epoch" to runtime.epoch, "job" to "review-retry", "usage" to ModelUsage(50, 5, 10, 0).recordJson(), "telemetry" to reviewReport.recordJson()))
            roman.communication(obj("op" to "job_release", "epoch" to runtime.epoch, "job" to "review-retry"))
            val romanTurn = monitoring(root, "turns").single { it.getValue("turn").jsonObject.getValue("civilization").jsonPrimitive.content == "Rome" }
            check(romanTurn.getValue("usage").jsonObject.getValue("total_tokens").jsonPrimitive.long == 190L)
            runtime.executor.accessGame { game ->
                game.getCivilization("France").units.getCivUnits().toList().forEach { it.destroy() }
                game.getCivilization("Rome").tech.techsResearched.addAll(game.ruleset.technologies.keys)
                game.victoryData = VictoryData(game.getCivilization("Egypt"), "Scientific", game.turns)
                runtime.executor.commitCheckpoint()
            }
            val result = monitoring(root, "state").single { "result" in it }
            check(result.getValue("status").jsonPrimitive.content == "completed")
            check(result.getValue("result").jsonObject.getValue("winner").jsonPrimitive.content == "Egypt")
            val scores = result.getValue("scores").jsonArray.map { it.jsonObject }.associateBy { it.getValue("civilization").jsonPrimitive.content }
            check(scores.keys == (civilizations + "Egypt").toSet())
            check(scores.getValue("Egypt").getValue("controller").jsonPrimitive.content == "BUILT_IN_AI")
            check(scores.getValue("France").getValue("defeated").jsonPrimitive.boolean)
            check(result.getValue("usage").jsonObject.getValue("total_tokens").jsonPrimitive.long == 450L)
            val memory = result.getValue("memoryEdits").jsonObject
            check(memory.getValue("accepted").jsonPrimitive.long == 9L && memory.getValue("rejected").jsonPrimitive.long == 3L)
            check(memory.getValue("batches").jsonPrimitive.long == 6L && memory.getValue("complete").jsonPrimitive.boolean)
            check(memory.getValue("rejectionReasons").jsonObject.getValue("SOURCE_NOT_DELIVERED").jsonPrimitive.long == 3L)
            check(result.getValue("players").jsonArray.filter { it.jsonObject.getValue("civilization").jsonPrimitive.content in civilizations }.all { it.jsonObject.getValue("memoryEdits") == edits.recordJson() })
            check(scores.getValue("Rome").getValue("score").jsonPrimitive.long > scores.getValue("Egypt").getValue("score").jsonPrimitive.long)
            // Only operator telemetry can be rebuilt; scheduler records are untouched.
            DriverManager.getConnection("jdbc:sqlite:${root.resolve("runner/monitoring.sqlite3")}").use { db ->
                db.createStatement().use { it.executeUpdate("UPDATE monitor_records SET payload='{}' WHERE collection='state' AND key='result'") }
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("final-restart")).use {
            val result = monitoring(root, "state").single { "result" in it }
            check(result.getValue("result").jsonObject.getValue("winner").jsonPrimitive.content == "Egypt")
            check(result.getValue("usage").jsonObject.getValue("total_tokens").jsonPrimitive.long == 450L)
            check(result.getValue("memoryEdits").jsonObject.getValue("rejected").jsonPrimitive.long == 3L)
        }
    }
}
