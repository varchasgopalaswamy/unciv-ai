package uncivai

import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.civilization.diplomacy.DiplomaticModifiers
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import uncivai.TradeCycleScenarioSetup.Step
import uncivai.player.PlayerHost
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

/** The live fixture driven through authenticated HTTPS, with native checks at each settlement. */
object TradeCycleProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single()).toAbsolutePath()
        Files.createDirectories(root)
        val checkpoint = root.resolve("exploration.json")
        require(!Files.exists(checkpoint)) { "Choose a fresh tradeCycleDir" }
        var runtime = HeadlessRuntime.createTradeCycleExercise(root.resolve("initial"))
        runtime.executor.enableCheckpoints(checkpoint)
        var api = GameplayTestApi(runtime, root.resolve("tls0"))
        api.acquire()
        var token = checkNotNull(api.activate().playerToken)
        var restarts = 0
        fun restart() {
            api.close()
            runtime.close()
            runtime = HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart${++restarts}"))
            api = GameplayTestApi(runtime, root.resolve("tls$restarts"))
            api.acquire()
            token = checkNotNull(api.activate().playerToken)
        }
        fun call(civ: String, op: String, vararg fields: Pair<String, Any?>): JsonObject {
            val identity = if (op in setOf("trade_options", "trade_read")) emptyArray() else arrayOf("requestId" to UUID.randomUUID().toString())
            val result = api.communicate(api.playerToken(civ), obj("op" to op, "epoch" to runtime.epoch, *identity, *fields))
            check(result["error"] == null) { result.toString() }
            return result
        }
        fun options() = call("Rome", "trade_options", "partner" to "Greece")
        fun item(type: String, name: String? = null, amount: Int? = null, side: String = "give"): JsonObject {
            val chosen = options()[side]!!.jsonArray.map { it.jsonObject }.single {
                it["type"]!!.jsonPrimitive.content == type &&
                    (name == null || (if (type == "City") it["city"]!!.jsonObject["name"] else it["name"])!!.jsonPrimitive.content == name)
            }
            return JsonObject(chosen.filterKeys { it in setOf("type", "name", "amount", "duration") } + if (amount == null) emptyMap() else obj("amount" to amount))
        }
        fun gold(amount: Int, side: String = "give") = item("Gold", amount = amount, side = side)
        fun offer(give: List<JsonObject>, receive: List<JsonObject> = emptyList(), civ: String = "Rome", previous: JsonObject? = null): JsonObject {
            val fields = previous?.let { arrayOf("proposal" to it["proposal"], "version" to it["version"]) } ?: emptyArray()
            return call(civ, "trade_offer", "partner" to if (civ == "Rome") "Greece" else "Rome", "give" to give, "receive" to receive, *fields)["trade"]!!.jsonObject
        }
        fun read(civ: String, proposal: JsonObject) = call(civ, "trade_read", "proposal" to proposal["proposal"])["trade"]!!.jsonObject
        fun decide(proposal: JsonObject, decision: String = "accept", civ: String = "Greece"): JsonObject {
            val current = read(civ, proposal)
            check(current["clauses"] == read(if (civ == "Rome") "Greece" else "Rome", proposal)["clauses"])
            return call(civ, "trade_decide", "proposal" to current["proposal"], "version" to current["version"], "decision" to decision)
        }
        fun money() = listOf("Rome", "Greece").map { runtime.game.getCivilization(it).gold }
        fun resolveCities(active: String) {
            while (true) {
                val view = api.readAlerts(active)
                val alert = view.alerts.firstOrNull { it.capture != null } ?: return
                val capture = checkNotNull(alert.capture)
                check(capture.title.isNotBlank())
                val keep = capture.options.single { it.name == "KeepCity" && it.available }
                api.execute(active, TurnCommandRequest(UUID.randomUUID().toString(), view.epoch, view.revision, TurnCommandKind.RESOLVE_CAPTURED_CITY, alert.handle, keep.name))
            }
        }
        fun finishRound() {
            resolveCities(token)
            api.finishTurn(token)
            token = checkNotNull(api.activate().playerToken)
            resolveCities(token)
            api.finishTurn(token)
            token = checkNotNull(api.activate().playerToken)
        }
        lateinit var initial: GameCheckpoint
        lateinit var finalRequest: JsonObject
        lateinit var finalResponse: JsonObject
        try {
            api.operator(OperatorRequest("barrier", api.lease))
            initial = checkNotNull(api.operator(OperatorRequest("snapshot", api.lease)).checkpoint)
            val rounds = Files.createDirectories(root.resolve("round-checkpoints"))
            Files.writeString(rounds.resolve("${initial.epoch}-round-0.json"), obj("host" to initial.recordJson()).toString())
            api.operator(OperatorRequest("unbarrier", api.lease))
            token = checkNotNull(api.activate().playerToken)
            for (step in Step.entries) {
                println("Trade cycle: $step")
                if (step != Step.PAID_PEACE) {
                    Files.writeString(rounds.resolve("${runtime.epoch}-round-${runtime.game.turns}.json"), obj("host" to Json.parseToJsonElement(Files.readString(checkpoint))).toString())
                }
                check(api.turn(token).scenario!!.phase == step.name)
                check(TradeCycleScenarioSetup.scenario(runtime.executor, runtime.game, "Greece").phase == step.name)
                val before = money()
                val handoff = runtime.executor.handoff
                when (step) {
                    Step.PAID_PEACE -> {
                        check(runtime.game.getCivilization("Rome").isAtWarWith(runtime.game.getCivilization("Greece")))
                        val peace = item("Treaty", "Peace Treaty")
                        val initialOffer = offer(listOf(peace, gold(40)), listOf(peace))
                        check(api.turn(token).negotiation["active"]!!.jsonPrimitive.content == "true")
                        val frozen = api.turn(token)
                        api.reject(token, TurnCommandRequest("blocked-end", frozen.epoch, frozen.revision, TurnCommandKind.END_TURN).reviewedFor(frozen), "NEGOTIATION_ACTIVE")
                        read("Greece", initialOffer)
                        val counter = offer(listOf(peace), listOf(peace, gold(30)), "Greece", initialOffer)
                        decide(counter, civ = "Rome")
                        check(money() == listOf(before[0] - 30, before[1] + 30))
                        check(runtime.game.getCivilization("Rome").getDiplomacyManager("Greece")!!.turnsToPeaceTreaty() > 0)
                    }

                    Step.DECLINE -> {
                        decide(offer(listOf(gold(7))), "decline")
                        check(money() == before)
                    }

                    Step.WITHDRAW -> {
                        val pending = offer(listOf(gold(11)))
                        read("Greece", pending)
                        call("Greece", "send", "to" to "Rome", "text" to "Please withdraw this offer.")
                        decide(pending, "withdraw", "Rome")
                        check(money() == before)
                    }

                    Step.DEADLINE -> {
                        val pending = offer(listOf(gold(13)))
                        read("Greece", pending)
                        restart()
                        check(read("Greece", pending)["deadlineMillis"] == pending["deadlineMillis"])
                        runtime.executor.clockMillis = { pending["deadlineMillis"]!!.jsonPrimitive.long }
                        check(read("Greece", pending)["status"]!!.jsonPrimitive.content == "EXPIRED")
                        runtime.executor.clockMillis = System::currentTimeMillis
                        check(money() == before)
                    }

                    Step.GIFT -> {
                        decide(offer(listOf(gold(17))))
                        check(money() == listOf(before[0] - 17, before[1] + 17))
                    }

                    Step.RESOURCE_BARGAIN -> {
                        val romeGoods = listOf(item("Luxury_Resource", "Silk", 1), item("Strategic_Resource", "Iron", 2))
                        fun greekGoods(gpt: Int, cash: Int = 0) = listOf(item("Luxury_Resource", "Gold Ore", 1, "receive"), item("Gold_Per_Turn", amount = gpt, side = "receive")) + if (cash == 0) emptyList() else listOf(gold(cash, "receive"))
                        var pending = offer(romeGoods, greekGoods(4))
                        read("Greece", pending)
                        pending = offer(greekGoods(3, 10), romeGoods, "Greece", pending)
                        read("Rome", pending)
                        pending = offer(romeGoods, greekGoods(2, 20), "Rome", pending)
                        restart()
                        read("Greece", pending)
                        pending = offer(greekGoods(2, 25), romeGoods, "Greece", pending)
                        check(read("Rome", pending)["counteroffersRemaining"]!!.jsonPrimitive.int == 0)
                        decide(pending, civ = "Rome")
                        check(money() == listOf(before[0] + 25, before[1] - 25))
                        check(runtime.game.getCivilization("Rome").getDiplomacyManager("Greece")!!.goldPerTurn() == 2)
                        check(runtime.game.getCivilization("Greece").getDiplomacyManager("Rome")!!.goldPerTurn() == -2)
                    }

                    Step.FRIENDSHIP -> {
                        decide(call("Rome", "friendship_offer", "partner" to "Greece")["trade"]!!.jsonObject)
                        check(runtime.game.getCivilization("Rome").getDiplomacyManager("Greece")!!.getFlag(DiplomacyFlags.DeclarationOfFriendship) == 30)
                    }

                    Step.EMBASSY_IN_ROME -> {
                        val outcome = decide(offer(listOf(item("Embassy")))).getValue("outcome").jsonObject
                        checkTradeConfirmation(outcome, romeMayEnter = false, greeceMayEnter = false, romeHasEmbassy = false, greeceHasEmbassy = true)
                        check(runtime.game.getCivilization("Greece").getDiplomacyManager("Rome")!!.hasModifier(DiplomaticModifiers.EstablishedEmbassy))
                        check(!runtime.game.getCivilization("Rome").getDiplomacyManager("Greece")!!.hasModifier(DiplomaticModifiers.EstablishedEmbassy))
                    }

                    Step.EMBASSY_IN_ATHENS -> {
                        val outcome = decide(offer(emptyList(), listOf(item("Embassy", side = "receive")))).getValue("outcome").jsonObject
                        checkTradeConfirmation(outcome, romeMayEnter = false, greeceMayEnter = false, romeHasEmbassy = true, greeceHasEmbassy = true)
                        check(runtime.game.getCivilization("Rome").getDiplomacyManager("Greece")!!.hasModifier(DiplomaticModifiers.SharedEmbassies))
                    }

                    Step.GREEK_ACCESS -> {
                        val outcome = decide(offer(listOf(item("Agreement")))).getValue("outcome").jsonObject
                        checkTradeConfirmation(outcome, romeMayEnter = false, greeceMayEnter = true, romeHasEmbassy = true, greeceHasEmbassy = true)
                        check(runtime.game.getCivilization("Greece").getDiplomacyManager("Rome")!!.hasOpenBorders)
                        check(!runtime.game.getCivilization("Rome").getDiplomacyManager("Greece")!!.hasOpenBorders)
                    }

                    Step.FUNDED_TREATIES_AND_CITIES -> {
                        val oldRome = api.turn(token)
                        val oldCity = oldRome.cities.single { it.name == "Antium" }.handle
                        val research = item("Treaty", "Research Agreement")
                        val defense = item("Treaty", "Defensive Pact")
                        val cost = research["amount"]!!.jsonPrimitive.int
                        check(before[1] < cost) { "The fixture must exercise an otherwise underfunded research signature" }
                        val pending = offer(listOf(research, defense, gold(cost), item("City", "Antium")), listOf(research, defense, item("Agreement", side = "receive"), item("City", "Sparta", side = "receive")))
                        read("Greece", pending)
                        finalRequest = obj("op" to "trade_decide", "epoch" to runtime.epoch, "requestId" to "final-city-accept", "proposal" to pending["proposal"], "version" to pending["version"], "decision" to "accept")
                        finalResponse = api.communicate(api.playerToken("Greece"), finalRequest)
                        check(finalResponse["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "ENACTED")
                        checkTradeConfirmation(finalResponse.getValue("outcome").jsonObject, romeMayEnter = true, greeceMayEnter = true, romeHasEmbassy = true, greeceHasEmbassy = true)
                        check(money() == listOf(before[0] - 2 * cost, before[1]))
                        check(runtime.game.getCivilization("Rome").cities.map { it.name }.toSet() == setOf("Rome", "Sparta"))
                        check(runtime.game.getCivilization("Greece").cities.map { it.name }.toSet() == setOf("Athens", "Antium"))
                        val fresh = api.turn(token)
                        api.reject(token, TurnCommandRequest("sold-city", fresh.epoch, fresh.revision, TurnCommandKind.RENAME_CITY, oldCity, "Stolen"), "CITY_UNAVAILABLE")
                        check(api.communicate(api.playerToken("Greece"), finalRequest) == finalResponse)
                        check(money() == listOf(before[0] - 2 * cost, before[1]))
                    }
                }
                check(runtime.game.currentPlayer == "Rome" && runtime.executor.handoff == handoff)
                val phase = api.turn(token).scenario!!.phase
                check(phase == if (step == Step.entries.last()) "TRADES_ENACTED" else "CONTINUE_TURN") { phase }
                Files.copy(checkpoint, root.resolve("${step.name.lowercase()}.json"))
                finishRound()
            }
            // Two further complete rounds, native treaty ticking, then exact acceptance replay.
            repeat(2) { finishRound() }
            restart()
            val before = money()
            check(api.communicate(api.playerToken("Greece"), finalRequest) == finalResponse)
            check(money() == before)
            check(api.turn(token).scenario!!.phase == "TRADES_ENACTED")
            check(runtime.game.getCivilization("Rome").getDiplomacyManager("Greece")!!.hasFlag(DiplomacyFlags.ResearchAgreement))
            Files.copy(checkpoint, root.resolve("verified-final.json"))
            // Restore on a separate copy so the operator can still inspect the completed exercise.
            api.close()
            runtime.close()
            val rollback = root.resolve("rollback.json")
            Files.copy(checkpoint, rollback)
            HeadlessRuntime.loadCheckpoint(rollback, root.resolve("rollback-data")).use { restored ->
                restored.executor.pause()
                restored.executor.communicationPaused = true
                restored.executor.restoreRound(initial, "trade-cycle-rollback")
                check(PlayerHost(restored).bindPlayer("Rome").turn().scenario!!.phase == "PAID_PEACE")
                check(restored.game.getCivilization("Rome").gold == 2000 && restored.game.getCivilization("Greece").gold == 0)
                check(restored.game.getCivilization("Rome").cities.any { it.name == "Antium" })
                check(restored.executor.communicationState.trades.proposals.isEmpty())
            }
            println("Trade cycle verified: $root")
        } finally {
            api.close()
            runtime.close()
        }
    }
}
