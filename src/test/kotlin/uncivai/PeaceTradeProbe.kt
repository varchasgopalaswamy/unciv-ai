package uncivai

import com.unciv.json.json
import com.unciv.logic.trade.Trade
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import com.unciv.logic.trade.TradeRequest
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerHost
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path

/** Consent crosses HTTPS while native assertions verify peace, payment, combat and rollback. */
object PeaceTradeProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        val checkpoint = root.resolve("peace.json")
        lateinit var round: GameCheckpoint
        lateinit var accepted: JsonObject
        lateinit var receipt: JsonObject
        HeadlessRuntime.createPeaceExercise(root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                var token = checkNotNull(api.activate().playerToken)
                val epoch = runtime.executor.epoch
                val rome = runtime.game.getCivilization("Rome")
                val greece = runtime.game.getCivilization("Greece")
                var sequence = 0
                fun query(civ: String, op: String, vararg fields: Pair<String, Any?>) = api.communicate(api.playerToken(civ), obj("op" to op, "epoch" to epoch, *fields))
                fun command(civ: String, op: String, vararg fields: Pair<String, Any?>) = query(civ, op, "requestId" to "peace${++sequence}", *fields)
                val options = query("Rome", "trade_options", "partner" to "Greece")
                check(options["available"]!!.jsonPrimitive.boolean && options["requiresPeaceTreaty"]!!.jsonPrimitive.boolean)
                check(query("Greece", "trade_options", "partner" to "Rome")["available"]!!.jsonPrimitive.boolean)
                fun term(side: String, type: String, amount: Int = 1): JsonObject {
                    val item = options.getValue(side).jsonArray.map { it.jsonObject }.single { it["type"]!!.jsonPrimitive.content == type }
                    return obj("type" to item["type"], "name" to item["name"], "amount" to amount, "duration" to item["duration"])
                }
                val treaty = term("give", "Treaty")
                val silk = term("give", "Luxury_Resource")
                val gold = term("receive", "Gold", 20)
                val gpt = term("receive", "Gold_Per_Turn", 2)
                fun offer(give: List<JsonObject>, receive: List<JsonObject>) = command("Rome", "trade_offer", "partner" to "Greece", "give" to give, "receive" to receive)
                fun decide(civ: String, proposal: JsonObject, decision: String = "accept") = command(civ, "trade_decide", "proposal" to proposal["proposal"], "version" to proposal["version"], "decision" to decision)

                // Every malformed peace leaves the engine (including war and treasury) untouched.
                for ((give, receive) in listOf(
                    listOf(gold) to emptyList(),
                    listOf(treaty) to emptyList(),
                    emptyList<JsonObject>() to listOf(treaty),
                    listOf(treaty, treaty) to listOf(treaty),
                    listOf(JsonObject(treaty + obj("amount" to 2))) to listOf(treaty),
                    listOf(treaty) to listOf(JsonObject(treaty + obj("duration" to 999))),
                    listOf(JsonObject(treaty + obj("name" to "Research Agreement"))) to listOf(treaty),
                    listOf(treaty, JsonObject(gold + obj("amount" to Int.MAX_VALUE))) to listOf(treaty),
                )) {
                    val before = json().toJson(runtime.game)
                    check(offer(give, receive)["error"] != null)
                    check(json().toJson(runtime.game) == before)
                }
                val costly = offer(listOf(treaty), listOf(treaty, JsonObject(gold + obj("amount" to 100))))["trade"]!!.jsonObject
                greece.addGold(50 - greece.gold) // A concurrent expense before consent.
                check(decide("Greece", costly)["error"] != null)
                check(rome.isAtWarWith(greece) && rome.gold == 1000 && greece.gold == 50)
                greece.addGold(950)
                check(decide("Greece", costly)["error"] != null) // Cannot revive an invalid proposal.

                val before = api.turn(token)
                val warrior = before.map.units.single { it.name == "Warrior" }
                val attack = warrior.combat!!.attacks.first()
                val socialVersion = runtime.executor.communicationState.peaceVersions["Rome"] ?: 0
                val staleAttack = TurnCommandRequest("stale-attack", epoch, before.revision, TurnCommandKind.ATTACK, target = warrior.handle, destination = attack.destination, attackFrom = attack.attackFrom, socialVersion = socialVersion)
                val initial = offer(listOf(treaty, silk), listOf(treaty, JsonObject(gpt + obj("amount" to 4))))["trade"]!!.jsonObject
                val counter = command("Greece", "trade_offer", "partner" to "Rome", "proposal" to initial["proposal"], "version" to initial["version"], "give" to listOf(treaty, gpt, gold), "receive" to listOf(treaty, silk))["trade"]!!.jsonObject
                check(decide("Greece", initial)["error"] != null)
                check(rome.isAtWarWith(greece) && rome.getDiplomacyManager(greece)!!.trades.isEmpty())
                check(query("Rome", "trade_read", "proposal" to counter["proposal"])["trade"]!!.jsonObject["clauses"] == counter["clauses"])
                check(offer(listOf(treaty), listOf(treaty))["error"] != null) // No overlapping negotiation.

                api.reject(token, staleAttack, "NEGOTIATION_ACTIVE")
                api.reject(token, TurnCommandRequest("held-end", epoch, before.revision, TurnCommandKind.END_TURN).reviewedFor(before), "NEGOTIATION_ACTIVE")
                check(runtime.game.currentPlayer == "Rome")
                api.operator(OperatorRequest("barrier", api.lease))
                round = api.operator(OperatorRequest("snapshot", api.lease)).checkpoint!!
                api.operator(OperatorRequest("unbarrier", api.lease))
                token = checkNotNull(api.activate().playerToken)
                check(before.scenario == null)
                val held = api.turn(token)
                command("Rome", "send", "to" to "Greece", "text" to "Hello")
                check(api.turn(token).revision == held.revision) { "An unrelated greeting invalidated gameplay" }
                val balances = rome.gold to greece.gold
                val health = listOf(rome, greece).map { it.units.getCivUnits().single().health }
                val income = rome.stats.statsForNextTurn.gold to greece.stats.statsForNextTurn.gold
                val handoff = runtime.executor.handoff
                accepted = obj("op" to "trade_decide", "epoch" to epoch, "requestId" to "accept-peace", "proposal" to counter["proposal"], "version" to counter["version"], "decision" to "accept")
                receipt = api.communicate(api.playerToken("Rome"), accepted)
                check(receipt["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "ENACTED")
                check(runtime.game.currentPlayer == "Rome" && runtime.executor.handoff == handoff)
                check(!rome.isAtWarWith(greece) && !greece.isAtWarWith(rome))
                check(rome.gold == balances.first + 20 && greece.gold == balances.second - 20)
                check(rome.stats.statsForNextTurn.gold == income.first + 2 && greece.stats.statsForNextTurn.gold == income.second - 2)
                check(rome.getCivResourcesByName()["Silk"] == 1 && greece.getCivResourcesByName()["Silk"] == 1)
                val native = greece.getDiplomacyManager(rome)!!.trades.single()
                check(native.reverse().equalTrade(rome.getDiplomacyManager(greece)!!.trades.single()))
                check(native.ourOffers.single { it.type == TradeOfferType.Treaty }.duration == treaty["duration"]!!.jsonPrimitive.int)
                check(native.ourOffers.single { it.type == TradeOfferType.Gold_Per_Turn }.duration == gpt["duration"]!!.jsonPrimitive.int)
                check(rome.tradeRequests.isEmpty() && greece.tradeRequests.isEmpty())
                check(offer(listOf(treaty), listOf(treaty))["error"] != null)
                check(api.communicate(api.playerToken("Rome"), accepted) == receipt)
                api.reject(token, staleAttack, "STALE_REVISION")
                val after = api.turn(token)
                api.reject(token, staleAttack.copy(revision = after.revision), "STALE_REVISION") // Diplomacy fence too.
                check(after.map.units.all { it.combat!!.attacks.isEmpty() })
                check(after.diplomacy.knownCivilizations.single().let { !it.declareWar.available && it.peaceTreatyTurns > 0 })
                check(listOf(rome, greece).map { it.units.getCivUnits().single().health } == health)
                // Refresh only affected observations and continue ordinary scheduled gameplay.
                api.finishTurn(token)
                token = checkNotNull(api.activate().playerToken)
                check(api.turn(token).scenario == null)
                api.finishTurn(token)
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            val rome = PlayerHost(runtime).bindPlayer("Rome")
            val before = json().toJson(runtime.game)
            check(rome.communication(accepted) == receipt)
            check(json().toJson(runtime.game) == before)
            runtime.executor.pause()
            runtime.executor.communicationPaused = true
            runtime.executor.restoreRound(round, "restored-peace-epoch")
            runtime.executor.accessGame { game ->
                check(game.getCivilization("Rome").isAtWarWith(game.getCivilization("Greece")))
                check(game.getCivilization("Rome").getDiplomacyManager("Greece")!!.trades.isEmpty())
            }
            check(runtime.executor.communicationState.outcomes.none { it.action == "ENACT_TRADE" })
            check(runCatching { rome.communication(accepted) }.isFailure)
        }
        verifyRequestCleanup(root.resolve("requests"))
        for (afterReplace in listOf(false, true)) EngineTradeProbe.verifyFailedCheckpoint(root.resolve("failure-$afterReplace"), afterReplace, peace = true)
    }

    private fun verifyRequestCleanup(root: Path) {
        HeadlessRuntime.createPeaceExercise(root).use { runtime ->
            val a = runtime.game.getCivilization("Rome")
            val b = runtime.game.getCivilization("Greece")
            fun nativePeace() = Trade().apply {
                ourOffers.add(TradeOffer("Peace Treaty", TradeOfferType.Treaty, 1, runtime.game.speed.peaceDealDuration))
                theirOffers.add(TradeOffer("Peace Treaty", TradeOfferType.Treaty, 1, runtime.game.speed.peaceDealDuration))
            }
            a.tradeRequests.add(TradeRequest(b.civID, nativePeace()))
            b.tradeRequests.add(TradeRequest(a.civID, nativePeace()))
            val unrelated = TradeRequest(b.civID, Trade().apply { theirOffers.add(TradeOffer("Gold", TradeOfferType.Gold, 5, -1)) })
            a.tradeRequests.add(unrelated)
            val sessions = listOf(a, b).associate { it.civID to PlayerHost(runtime).bindPlayer(it.civID) }
            val treaty = obj("type" to "Treaty", "name" to "Peace Treaty", "amount" to 1, "duration" to runtime.game.speed.peaceDealDuration)
            val offer = sessions.getValue("Rome").communication(obj("op" to "trade_offer", "epoch" to runtime.executor.epoch, "requestId" to "offer", "partner" to "Greece", "give" to listOf(treaty), "receive" to listOf(treaty)))["trade"]!!.jsonObject
            val accepted = sessions.getValue("Greece").communication(obj("op" to "trade_decide", "epoch" to runtime.executor.epoch, "requestId" to "accept", "proposal" to offer["proposal"], "version" to offer["version"], "decision" to "accept"))
            check(accepted["outcome"] != null && !a.isAtWarWith(b))
            check(a.tradeRequests.single() === unrelated && b.tradeRequests.isEmpty())
        }
    }
}
