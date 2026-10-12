package uncivai

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

/** Drives the exact live fixture through authenticated gameplay, with no mid-run fixture edits. */
object AgreementLifecycleScenarioProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Files.createDirectories(Path.of(args.single()).toAbsolutePath())
        val checkpoint = root.resolve("checkpoint.json")
        val tokens = mapOf("Rome" to "R".repeat(43), "Greece" to "G".repeat(43), "France" to "F".repeat(43))
        var runtime = HeadlessRuntime.createAgreementLifecycleExercise(root.resolve("data"))
        try {
            runtime.executor.enableCheckpoints(checkpoint)
            copyCheckpoint(checkpoint, root.resolve("initial.json"))
            val initialEpoch = runtime.epoch
            var acceptRequest: JsonObject? = null
            var accepted: JsonObject? = null
            var agreedId = ""
            var firstRoundLedger = JsonObject(emptyMap())
            var proposalId = ""
            fun call(api: GameplayTestApi, civ: String, op: String, vararg fields: Pair<String, Any?>): JsonObject {
                val mutation = if (op == "trade_offer") arrayOf("requestId" to UUID.randomUUID().toString()) else emptyArray()
                return api.communicate(tokens.getValue(civ), obj("op" to op, "epoch" to runtime.epoch, *mutation, *fields))
            }
            fun agreements(api: GameplayTestApi, civ: String) = call(api, civ, "agreement_list").getValue("agreements").jsonArray.map { it.jsonObject }
            GameplayTestApi(runtime, root.resolve("tls-initial"), tokens).use { api ->
                api.acquire()
                val firstToken = checkNotNull(api.activate().playerToken)
                val franceBefore = call(api, "France", "state").getValue("context")
                val ownInitial = agreements(api, "Rome").single()
                check(ownInitial.getValue("partner").jsonPrimitive.content == "France")
                check(ownInitial.getValue("remainingTurns").jsonPrimitive.content == "1")
                val duration = runtime.game.speed.dealDuration
                val iron = obj("type" to "Strategic_Resource", "name" to "Iron", "amount" to 1, "duration" to duration)
                fun payment(n: Int) = obj("type" to "Gold_Per_Turn", "name" to "Gold per turn", "amount" to n, "duration" to duration)
                call(api, "Rome", "trade_options", "partner" to "Greece")
                val offered = call(api, "Rome", "trade_offer", "partner" to "Greece", "give" to listOf(iron), "receive" to listOf(payment(2))).getValue("trade").jsonObject
                proposalId = offered.getValue("proposal").jsonPrimitive.content
                check(call(api, "France", "state").getValue("context") == franceBefore)
                call(api, "Greece", "trade_options", "partner" to "Rome")
                call(api, "Greece", "trade_read", "proposal" to proposalId)
                val counter = call(api, "Greece", "trade_offer", "partner" to "Rome", "give" to listOf(payment(3)), "receive" to listOf(iron), "proposal" to proposalId, "version" to offered["version"]).getValue("trade").jsonObject
                call(api, "Rome", "trade_read", "proposal" to proposalId)
                acceptRequest = obj("op" to "trade_decide", "epoch" to runtime.epoch, "requestId" to "accept-resource-deal", "proposal" to proposalId, "version" to counter["version"], "decision" to "accept")
                accepted = api.communicate(tokens.getValue("Rome"), checkNotNull(acceptRequest))
                check(accepted["outcome"] != null)
                check(api.communicate(tokens.getValue("Rome"), checkNotNull(acceptRequest)) == accepted)
                agreedId = agreements(api, "Rome").single { it.getValue("partner").jsonPrimitive.content == "Greece" }.getValue("agreement").jsonPrimitive.content
                for (civ in listOf("Rome", "Greece")) {
                    val row = agreements(api, civ).single { it.getValue("partner").jsonPrimitive.content == if (civ == "Rome") "Greece" else "Rome" }
                    call(api, civ, "agreement_read", "agreement" to row["agreement"])
                }
                check(call(api, "France", "state").getValue("context") == franceBefore)
                check(runtime.executor.communicationState.streams["France"].orEmpty().none { it.kind == "trade" || it.body.toString().contains(proposalId) })
                copyCheckpoint(checkpoint, root.resolve("negotiated.json"))
                for (civ in listOf("Rome", "Greece", "France")) {
                    check(runtime.game.currentPlayer == civ)
                    val activeToken = if (civ == "Rome") firstToken else checkNotNull(api.activate().playerToken)
                    api.finishTurn(activeToken)
                }
                check(runtime.game.turns == 1 && runtime.game.currentPlayer == "Rome")
                for (civ in listOf("Rome", "France")) {
                    val event = runtime.executor.communicationState.streams[civ].orEmpty().single { it.kind == "agreement" }
                    check(event.body.getValue("reason").jsonPrimitive.content == "EXPIRED")
                    check(event.wake) { "Shared agreement endings still wake their participants" }
                }
                copyCheckpoint(checkpoint, root.resolve("round-001.json"))
                firstRoundLedger = runtime.executor.communicationState.recordJson()
            }
            runtime.close()
            runtime = HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart"), AgreementLifecycleScenarioSetup.MAP_NAME)
            check(runtime.epoch == initialEpoch)
            check(runtime.executor.communicationState.recordJson() == firstRoundLedger)
            GameplayTestApi(runtime, root.resolve("tls-restarted"), tokens).use { api ->
                api.acquire()
                check(api.communicate(tokens.getValue("Rome"), checkNotNull(acceptRequest)) == accepted)
                val afterRestart = agreements(api, "Rome").single { it.getValue("agreement").jsonPrimitive.content == agreedId }
                check(afterRestart.getValue("status").jsonPrimitive.content == "ACTIVE")
                repeat(2) {
                    for (civ in listOf("Rome", "Greece", "France")) {
                        check(runtime.game.currentPlayer == civ)
                        val activeToken = checkNotNull(api.activate().playerToken)
                        if (civ == "France" && runtime.game.turns == 1) raid(api, activeToken, root)
                        api.finishTurn(activeToken)
                    }
                }
                check(runtime.game.turns == 3 && runtime.game.currentPlayer == "Rome")
                check(runtime.game.tileMap[AgreementLifecycleScenarioSetup.ironPosition].improvementIsPillaged)
                val rome = runtime.game.getCivilization("Rome")
                val greece = runtime.game.getCivilization("Greece")
                check(rome.isAtWarWith(runtime.game.getCivilization("France")) && !rome.isAtWarWith(greece))
                check(rome.getDiplomacyManager(greece)!!.trades.isEmpty() && greece.getDiplomacyManager(rome)!!.trades.isEmpty())
                check((greece.getCivResourcesByName()["Iron"] ?: 0) == 0)
                for (civ in listOf("Rome", "Greece")) {
                    val event = runtime.executor.communicationState.streams[civ].orEmpty().single { it.kind == "agreement" && it.body["reason"]?.jsonPrimitive?.content == "RESOURCE_UNAVAILABLE" }
                    val result = call(api, civ, "agreement_read", "agreement" to event.body["agreement"]).getValue("agreement").jsonObject
                    check(result.getValue("status").jsonPrimitive.content == "ENDED")
                    if (civ == "Rome") check(result.getValue("agreement").jsonPrimitive.content == agreedId)
                    Files.writeString(root.resolve("$civ-ended.json"), result.toString())
                }
                check(runtime.executor.communicationState.trades.proposals.size == 1)
                check(runtime.executor.communicationState.streams["France"].orEmpty().none { it.kind == "trade" || it.body.toString().contains(proposalId) })
                check(runtime.executor.communicationState.streams.values.flatten().count { it.kind == "agreement" } == 4)
                copyCheckpoint(checkpoint, root.resolve("round-003.json"))
            }
            Files.writeString(root.resolve("COMPLETE"), "Native expiry, negotiated resource trade, restart, legal French raid and native resource interruption passed.\n")
        } finally {
            runtime.close()
        }
    }

    private fun raid(api: GameplayTestApi, token: String, root: Path) {
        fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target)
        var turn = api.readAlerts(token)
        val war = command(turn, TurnCommandKind.DECLARE_WAR, "Rome").copy(confirmWar = true)
        val warReceipt = api.execute(token, war)
        check(api.execute(token, war) == warReceipt)
        turn = api.readAlerts(token)
        var unit = turn.map.units.single()
        val destination = TilePosition(-2, -2)
        check(unit.moves.any { it.destination == destination })
        api.execute(token, command(turn, TurnCommandKind.MOVE_UNIT, unit.handle).copy(destination = destination))
        turn = api.turn(token)
        unit = turn.map.units.single()
        check(unit.position == destination && unit.economy!!.pillage!!.available)
        val pillage = command(turn, TurnCommandKind.PILLAGE, unit.handle).copy(confirmAction = true)
        val receipt = api.execute(token, pillage)
        check(api.execute(token, pillage) == receipt)
        Files.writeString(root.resolve("pillage-receipt.json"), PlayerTurnJson.encode(receipt))
    }
}
