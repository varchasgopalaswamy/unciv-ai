package uncivai

import com.unciv.logic.VictoryData
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path

/** Real HTTPS calls, ordinary native AI turns, and telemetry after a checkpoint restart. */
object TelemetryProbe {
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
                val turns = Files.readAllLines(root.resolve("telemetry/turns.jsonl")).map { Json.parseToJsonElement(it).jsonObject }
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
            val snapshot = runtime.executor.roundSnapshot()
            runtime.executor.restoreRound(snapshot, "restored-epoch")
            check(state.jobs.values.sumOf { it.usage!!.inputTokens } == 300L)
            check(runtime.executor.communicationState.telemetry.turns.size == 3)
            runtime.executor.accessGame { game ->
                game.getCivilization("France").units.getCivUnits().toList().forEach { it.destroy() }
                game.getCivilization("Rome").tech.techsResearched.addAll(game.ruleset.technologies.keys)
                game.victoryData = VictoryData(game.getCivilization("Egypt"), "Scientific", game.turns)
                runtime.executor.commitCheckpoint()
            }
            val result = Json.parseToJsonElement(Files.readString(root.resolve("telemetry/game-result.json"))).jsonObject
            check(result.getValue("status").jsonPrimitive.content == "completed")
            check(result.getValue("result").jsonObject.getValue("winner").jsonPrimitive.content == "Egypt")
            val scores = result.getValue("scores").jsonArray.map { it.jsonObject }.associateBy { it.getValue("civilization").jsonPrimitive.content }
            check(scores.keys == (civilizations + "Egypt").toSet())
            check(scores.getValue("Egypt").getValue("controller").jsonPrimitive.content == "BUILT_IN_AI")
            check(scores.getValue("France").getValue("defeated").jsonPrimitive.boolean)
            check(result.getValue("usage").jsonObject.getValue("total_tokens").jsonPrimitive.long == 390L)
            val memory = result.getValue("memoryEdits").jsonObject
            check(memory.getValue("accepted").jsonPrimitive.long == 9L && memory.getValue("rejected").jsonPrimitive.long == 3L)
            check(memory.getValue("batches").jsonPrimitive.long == 6L && memory.getValue("complete").jsonPrimitive.boolean)
            check(memory.getValue("rejectionReasons").jsonObject.getValue("SOURCE_NOT_DELIVERED").jsonPrimitive.long == 3L)
            check(result.getValue("players").jsonArray.filter { it.jsonObject.getValue("civilization").jsonPrimitive.content in civilizations }.all { it.jsonObject.getValue("memoryEdits") == edits.recordJson() })
            check(scores.getValue("Rome").getValue("score").jsonPrimitive.long > scores.getValue("Egypt").getValue("score").jsonPrimitive.long)
            // Exports are replaceable and terminal scores remain frozen across restart.
            Files.writeString(root.resolve("telemetry/game-result.json"), "damaged export")
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("final-restart")).use {
            val result = Json.parseToJsonElement(Files.readString(root.resolve("telemetry/game-result.json"))).jsonObject
            check(result.getValue("result").jsonObject.getValue("winner").jsonPrimitive.content == "Egypt")
            check(result.getValue("usage").jsonObject.getValue("total_tokens").jsonPrimitive.long == 390L)
            check(result.getValue("memoryEdits").jsonObject.getValue("rejected").jsonPrimitive.long == 3L)
        }
    }
}
