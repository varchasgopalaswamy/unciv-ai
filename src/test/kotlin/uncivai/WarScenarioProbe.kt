package uncivai

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerHost
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

/** Exercises the production opening, consent guards, private contexts and persisted settlement. */
object WarScenarioProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single()).toAbsolutePath()
        for (variant in listOf("war-paid", "war-joint")) {
            val directory = Files.createDirectories(root.resolve(variant))
            val checkpoint = directory.resolve("exploration.json")
            var runtime = HeadlessRuntime.createWarExercise(directory.resolve("data"), variant)
            try {
                runtime.executor.enableCheckpoints(checkpoint)
                fun call(civ: String, op: String, vararg fields: Pair<String, Any?>): JsonObject = runtime.executor.communication.handle(civ, obj("op" to op, "epoch" to runtime.epoch, "requestId" to UUID.randomUUID().toString(), *fields))
                fun gold(n: Int) = obj("type" to "Gold", "name" to "Gold", "amount" to n, "duration" to -1)
                val war = obj("type" to "WarDeclaration", "name" to "France", "amount" to 1, "duration" to -1)
                fun offer(civ: String, give: List<JsonObject>, receive: List<JsonObject>, previous: JsonObject? = null): JsonObject {
                    val counter = previous?.let { arrayOf("proposal" to it["proposal"], "version" to it["version"]) } ?: emptyArray()
                    return call(civ, "trade_offer", "partner" to if (civ == "Rome") "Greece" else "Rome", "give" to give, "receive" to receive, *counter).getValue("trade").jsonObject
                }
                fun read(civ: String, p: JsonObject) = runtime.executor.communication.handle(civ, obj("op" to "trade_read", "epoch" to runtime.epoch, "proposal" to p["proposal"]))
                fun accept(civ: String, p: JsonObject): JsonObject {
                    read(civ, p)
                    return call(civ, "trade_decide", "proposal" to p["proposal"], "version" to p["version"], "decision" to "accept")
                }
                fun context(civ: String) = PlayerHost(runtime).bindPlayer(civ).turn().scenario
                fun money() = listOf("Rome", "Greece", "France").map { runtime.game.getCivilization(it).gold }
                fun wars() = listOf("Rome", "Greece").map { runtime.game.getCivilization(it).isAtWarWith(runtime.game.getCivilization("France")) }
                val initialFrance = context("France")
                val initialHandoff = runtime.executor.handoff
                check(money() == listOf(500, 500, 500) && wars() == listOf(false, false))
                val france = runtime.game.getCivilization("France")
                check(runtime.game.tileMap.values.any { !it.isExplored(france) })
                val pending = offer("Rome", listOf(gold(30)) + if (variant == "war-joint") listOf(war) else emptyList(), listOf(war))
                val romeWithOffer = context("Rome")
                check(context("France") == initialFrance)
                val settled: JsonObject
                if (variant == "war-paid") {
                    check(runtime.executor.communicationState.streams["Rome"].orEmpty().none { it.kind == "offer" })
                    val promise = runtime.executor.communicationState.offers.values.single().single()
                    val advice = read("Greece", pending)["trade"]!!.jsonObject["socialAdvisories"]!!.jsonArray.single().jsonObject
                    check(advice["proposal"]!!.jsonPrimitive.content == promise.proposal)
                    check(context("Rome") == romeWithOffer)
                    check(context("France") == initialFrance)
                    val counter = offer("Greece", listOf(war), listOf(gold(45)), pending)
                    settled = accept("Rome", counter)
                    check(money() == listOf(455, 545, 500) && wars() == listOf(false, true))
                    check(runtime.executor.communicationState.consents.all { it.decision == "accept" })
                } else {
                    settled = accept("Greece", pending)
                    check(money() == listOf(470, 530, 500) && wars() == listOf(true, true))
                }
                check(runtime.game.currentPlayer == "Rome" && runtime.executor.handoff == initialHandoff)
                Files.copy(checkpoint, directory.resolve("first-settlement.json"))
                // France's stream has pairwise outcomes, never the private proposal or its price.
                val id = pending.getValue("proposal").toString()
                check(runtime.executor.communicationState.streams["France"].orEmpty().none { it.kind == "trade" || it.body.toString().contains(id) })
                check(settled["outcome"] != null && settled["settlement"] == null)
                val evidence = runtime.executor.communicationState.requests.values.last().audit.settlement
                checkNotNull(evidence)
                check(evidence.before.gold == mapOf("Rome" to 500, "Greece" to 500, "France" to 500))
                check(evidence.after.gold == listOf("Rome", "Greece", "France").zip(money()).toMap())
                check(runtime.executor.communicationState.streams.values.flatten().none { it.body["settlement"] != null })
                val before = money()
                val ledger = runtime.executor.communicationState.recordJson()
                runtime.close()
                runtime = HeadlessRuntime.loadCheckpoint(checkpoint, directory.resolve("restart"))
                check(money() == before && runtime.executor.communicationState.recordJson() == ledger)
                if (variant == "war-paid") {
                    // Use the player turn API to hand off to Greece before its recruitment offer.
                    GameplayTestApi(runtime, directory.resolve("tls"), mapOf("Rome" to "R".repeat(43), "Greece" to "G".repeat(43), "France" to "F".repeat(43))).use { api ->
                        api.acquire()
                        api.finishTurn(checkNotNull(api.activate().playerToken))
                        api.activate()
                        check(runtime.game.currentPlayer == "Greece")
                        val balances = money()
                        val joining = offer("Greece", listOf(gold(15)), listOf(war))
                        accept("Rome", joining)
                        check(money() == listOf(balances[0] + 15, balances[1] - 15, balances[2]))
                        check(wars() == listOf(true, true))
                        check(runtime.game.currentPlayer == "Greece")
                    }
                }
                check(context("Rome")!!.phase == "CONTINUE")
                Files.copy(checkpoint, directory.resolve("settled.json"))
            } finally {
                runtime.close()
            }
        }
    }
}
