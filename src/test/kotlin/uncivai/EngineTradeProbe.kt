package uncivai

import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerHost
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path

/** Player writes go through HTTPS; assertions inspect actual engine balances, supplies and trades. */
object EngineTradeProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val checkpoint = root.resolve("trade.json")
        var restorePoint: GameCheckpoint? = null
        var acceptedRequest: JsonObject? = null
        var acceptedReceipt: JsonObject? = null
        var pending: JsonObject? = null
        var savedGold = 0
        HeadlessRuntime.createTradeExercise(root.resolve("initial")).use { runtime ->
            fun healthyCities() {
                for (civ in listOf("Rome", "Greece").map(runtime.game::getCivilization)) {
                    val city = civ.cities.single()
                    check(city.population.population == 5)
                    check(city.cityStats.currentCityStats.food >= 0)
                    check(civ.stats.statsForNextTurn.gold >= 4)
                    check(city.cityConstructions.currentConstructionName() == "Barracks")
                    check(city.cityConstructions.getCurrentConstruction().isBuildable(city.cityConstructions))
                    check(civ.diplomacy.values.all { it.diplomaticStatus.name == "Peace" })
                }
            }
            healthyCities()
            check(
                runtime.game.civilizations.filter { it.isMajorCiv() }.all { civ ->
                    civ.diplomacy.values.all { relation -> relation.diplomaticModifiers.isEmpty() && DiplomacyFlags.entries.none { relation.hasFlag(it) } }
                },
            )
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                healthyCities()
                var active = checkNotNull(api.activate().playerToken)
                check(runtime.game.currentPlayer == "Rome")
                val epoch = runtime.executor.epoch
                var sequence = 0
                fun command(civ: String, op: String, vararg fields: Pair<String, Any?>): JsonObject = api.communicate(api.playerToken(civ), obj("op" to op, "epoch" to epoch, "requestId" to "trade${++sequence}", *fields))
                fun read(civ: String, proposal: String) = api.communicate(api.playerToken(civ), obj("op" to "trade_read", "epoch" to epoch, "proposal" to proposal)).getValue("trade").jsonObject
                val options = api.communicate(api.playerToken("Rome"), obj("op" to "trade_options", "epoch" to epoch, "partner" to "Greece"))
                val context = api.communicate(api.playerToken("Rome"), obj("op" to "state", "epoch" to epoch)).getValue("context").jsonObject
                check(context.getValue("turn").jsonPrimitive.int == 0)
                check(context.getValue("communicationAvailable").jsonPrimitive.boolean)
                check(context.getValue("economy").jsonObject.getValue("treasury").jsonPrimitive.int == runtime.game.getCivilization("Rome").gold)
                check(context.getValue("knownCivilizations").jsonArray.single().jsonObject.getValue("civilizationId").jsonPrimitive.content == "Greece")
                check(context.getValue("scenario") == JsonNull)
                check(context.keys.none { it in setOf("map", "cities", "researchOptions", "turnReview") })
                check(context.toString().length < 6000)
                fun item(side: String, type: String, name: String, amount: Int): JsonObject {
                    val offered = options.getValue(side).jsonArray.map { it.jsonObject }.single { it["type"]!!.jsonPrimitive.content == type && it["name"]!!.jsonPrimitive.content == name }
                    check(offered["amount"]!!.jsonPrimitive.int >= amount)
                    return obj("type" to type, "name" to name, "duration" to offered["duration"], "amount" to amount)
                }
                val silk = item("give", "Luxury_Resource", "Silk", 1)
                val iron = item("give", "Strategic_Resource", "Iron", 2)
                val gold = item("receive", "Gold", "Gold", 20)
                val gpt = item("receive", "Gold_Per_Turn", "Gold per turn", 2)
                val rome = runtime.game.getCivilization("Rome")
                val greece = runtime.game.getCivilization("Greece")
                val beforeGold = rome.gold to greece.gold
                val beforeGpt = rome.stats.statsForNextTurn.gold to greece.stats.statsForNextTurn.gold
                val beforeSilk = rome.getCivResourcesByName()["Silk"]!!
                api.operator(OperatorRequest("barrier", api.lease))
                restorePoint = api.operator(OperatorRequest("snapshot", api.lease)).checkpoint!!
                api.operator(OperatorRequest("unbarrier", api.lease))
                active = checkNotNull(api.activate().playerToken)
                val beforeView = api.turn(active)
                val handoff = runtime.executor.handoff
                val initial = command("Rome", "trade_offer", "partner" to "Greece", "give" to listOf(silk), "receive" to listOf(JsonObject(gpt + obj("amount" to 4))))["trade"]!!.jsonObject
                val id = initial["proposal"]!!.jsonPrimitive.content
                check(rome.getDiplomacyManager(greece)!!.trades.isEmpty())
                val counter = command("Greece", "trade_offer", "proposal" to id, "version" to 1, "partner" to "Rome", "give" to listOf(gpt, gold), "receive" to listOf(silk))["trade"]!!.jsonObject
                check(counter["version"]!!.jsonPrimitive.int == 2)
                check(read("Rome", id)["clauses"] == read("Greece", id)["clauses"])
                val stale = command("Greece", "trade_decide", "proposal" to id, "version" to 1, "decision" to "accept")
                check(stale["error"]!!.jsonObject["code"]!!.jsonPrimitive.content == "STALE_REVISION")
                check(rome.gold == beforeGold.first && greece.gold == beforeGold.second)
                acceptedRequest = obj("op" to "trade_decide", "epoch" to epoch, "requestId" to "accept-counter", "proposal" to id, "version" to 2, "decision" to "accept")
                acceptedReceipt = api.communicate(api.playerToken("Rome"), acceptedRequest)
                check(acceptedReceipt["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "ENACTED")
                check(runtime.executor.handoff == handoff && runtime.game.currentPlayer == "Rome")
                check(rome.gold == beforeGold.first + 20 && greece.gold == beforeGold.second - 20)
                check(rome.getCivResourcesByName()["Silk"] == beforeSilk - 1)
                check(greece.getCivResourcesByName()["Silk"] == 1)
                check(rome.stats.statsForNextTurn.gold == beforeGpt.first + 2)
                check(greece.stats.statsForNextTurn.gold == beforeGpt.second - 2)
                val native = greece.getDiplomacyManager(rome)!!.trades.single()
                check(native.ourOffers.single { it.type.name == "Gold_Per_Turn" }.duration == gpt["duration"]!!.jsonPrimitive.int)
                check(native.theirOffers.single().name == "Silk")
                check(api.communicate(api.playerToken("Rome"), acceptedRequest) == acceptedReceipt)
                check(command("Rome", "trade_decide", "proposal" to id, "version" to 2, "decision" to "accept")["outcome"] == acceptedReceipt["outcome"])
                check(greece.getDiplomacyManager(rome)!!.trades.size == 1)
                api.reject(active, TurnCommandRequest("stale-game", epoch, beforeView.revision, TurnCommandKind.END_TURN).reviewedFor(beforeView), "STALE_REVISION")

                fun offer(give: List<JsonObject>, receive: List<JsonObject>): JsonObject = command("Rome", "trade_offer", "partner" to "Greece", "give" to give, "receive" to receive)["trade"]!!.jsonObject
                fun decide(proposal: JsonObject, decision: String, civ: String = "Greece") = command(civ, "trade_decide", "proposal" to proposal["proposal"], "version" to proposal["version"], "decision" to decision)
                for (decision in listOf("decline", "withdraw")) {
                    val proposal = offer(listOf(iron), listOf(gold))
                    decide(proposal, decision, if (decision == "withdraw") "Rome" else "Greece")
                    check(decide(proposal, "accept")["error"] != null)
                }
                check(command("Rome", "trade_offer", "partner" to "Greece", "give" to listOf(iron, iron), "receive" to listOf(gold))["error"] != null)
                check(command("Rome", "trade_offer", "partner" to "Greece", "give" to listOf(JsonObject(iron + obj("duration" to 999))), "receive" to listOf(gold))["error"] != null)
                val costly = offer(listOf(iron), listOf(JsonObject(gold + obj("amount" to 100))))
                val originalGold = greece.gold
                greece.addGold(50 - greece.gold) // An intervening engine expense, before acceptance.
                check(decide(costly, "accept")["error"] != null)
                check(rome.getCivResourcesByName()["Iron"] == 4 && greece.gold == 50)
                greece.addGold(originalGold - greece.gold)
                check(decide(costly, "accept")["error"] != null) // Invalid proposals never revive.
                val scarce = offer(listOf(iron), listOf(gold))
                val ironTile = rome.cities.single().getTiles().single { it.resource == "Iron" }
                ironTile.improvementIsPillaged = true
                rome.cache.updateCivResources()
                check(decide(scarce, "accept")["error"] != null)
                check(greece.getDiplomacyManager(rome)!!.trades.size == 1)
                ironTile.improvementIsPillaged = false
                rome.cache.updateCivResources()
                val expired = offer(listOf(iron), listOf(gold))
                runtime.executor.clockMillis = { expired["deadlineMillis"]!!.jsonPrimitive.content.toLong() }
                check(decide(expired, "accept")["error"] != null)
                runtime.executor.clockMillis = System::currentTimeMillis
                api.finishTurn(active)
                val next = checkNotNull(api.activate().playerToken)
                api.finishTurn(next)
                healthyCities()
                check(rome.getDiplomacyManager(greece)!!.trades.single().ourOffers.single().duration < silk["duration"]!!.jsonPrimitive.int)
                api.activate()
                pending = offer(listOf(iron), listOf(gold))
                savedGold = rome.gold
                runtime.executor.commitCheckpoint()
                Files.writeString(root.resolve("verified.json"), obj("canonicalCounteroffer" to counter, "enacted" to acceptedReceipt, "handoffs" to runtime.executor.handoff).toString())
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            val rome = runtime.game.getCivilization("Rome")
            val greece = runtime.game.getCivilization("Greece")
            val sessions = listOf("Rome", "Greece").associateWith { PlayerHost(runtime).bindPlayer(it) }
            check(sessions.getValue("Rome").communication(acceptedRequest!!) == acceptedReceipt)
            check(rome.gold == savedGold && rome.getDiplomacyManager(greece)!!.trades.size == 1)
            val proposal = pending!!
            val request = obj("op" to "trade_decide", "epoch" to runtime.executor.epoch, "requestId" to "after-restart", "proposal" to proposal["proposal"], "version" to proposal["version"], "decision" to "accept")
            val receipt = sessions.getValue("Greece").communication(request)
            check(receipt["outcome"] != null)
            check(rome.gold == savedGold + 20 && rome.getCivResourcesByName()["Iron"] == 2)
            check(greece.getCivResourcesByName()["Iron"] == 6)
            check(sessions.getValue("Greece").communication(request) == receipt)
            runtime.executor.pause()
            runtime.executor.communicationPaused = true
            runtime.executor.restoreRound(restorePoint!!, "restored-trade-epoch")
            check(runtime.executor.communicationState.trades.proposals.isEmpty())
            runtime.executor.accessGame { game ->
                check(game.getCivilization("Rome").getDiplomacyManager("Greece")!!.trades.isEmpty())
                val original = com.unciv.json.json().fromJson(com.unciv.logic.GameInfo::class.java, restorePoint.gameJson)
                check(game.getCivilization("Rome").gold == original.getCivilization("Rome").gold)
                check(game.getCivilization("Greece").gold == original.getCivilization("Greece").gold)
            }
            check(runCatching { sessions.getValue("Greece").communication(request) }.isFailure)
            runtime.executor.communicationPaused = false
            runtime.executor.resume()
            val open = sessions.getValue("Rome").communication(obj("op" to "trade_offer", "epoch" to runtime.executor.epoch, "requestId" to "before-war", "partner" to "Greece", "give" to emptyList<String>(), "receive" to listOf(obj("type" to "Gold", "name" to "Gold", "amount" to 10, "duration" to -1))))["trade"]!!.jsonObject
            val frozen = sessions.getValue("Rome").turn()
            val blocked = runCatching { sessions.getValue("Rome").execute(TurnCommandRequest("war", frozen.epoch, frozen.revision, TurnCommandKind.DECLARE_WAR, target = "Greece", confirmWar = true)) }.exceptionOrNull()
            check((blocked as uncivai.player.PlayerSessionException).reason == uncivai.player.PlayerSessionFailure.NEGOTIATION_ACTIVE)
            sessions.getValue("Rome").communication(obj("op" to "trade_decide", "epoch" to runtime.executor.epoch, "requestId" to "withdraw-before-war", "proposal" to open["proposal"], "version" to open["version"], "decision" to "withdraw"))
            val beforeWar = sessions.getValue("Rome").turn()
            sessions.getValue("Rome").execute(TurnCommandRequest("war", beforeWar.epoch, beforeWar.revision, TurnCommandKind.DECLARE_WAR, target = "Greece", confirmWar = true))
            val invalid = sessions.getValue("Rome").communication(obj("op" to "trade_read", "epoch" to runtime.executor.epoch, "proposal" to open["proposal"]))
            check(invalid["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "WITHDRAWN")
            check(runtime.executor.communicationState.outcomes.last().action == "DECLARE_WAR")
        }
        for (afterReplace in listOf(false, true)) verifyFailedCheckpoint(root.resolve("failure-$afterReplace"), afterReplace)
    }

    internal fun verifyFailedCheckpoint(root: Path, afterReplace: Boolean, peace: Boolean = false) {
        val checkpoint = root.resolve("checkpoint.json")
        lateinit var request: JsonObject
        var failCommit = false
        (if (peace) HeadlessRuntime.createPeaceExercise(root.resolve("initial")) else HeadlessRuntime.createTradeExercise(root.resolve("initial"))).use { runtime ->
            val store = CheckpointStore(checkpoint, beforeReplace = { if (failCommit && !afterReplace) error("Simulated storage failure") }, afterReplace = { if (failCommit && afterReplace) error("Lost acknowledgement after replacement") })
            runtime.executor.enableCheckpoints(store)
            val rome = PlayerHost(runtime).bindPlayer("Rome")
            val greece = PlayerHost(runtime).bindPlayer("Greece")
            val treaty = if (peace) listOf(obj("type" to "Treaty", "name" to "Peace Treaty", "amount" to 1, "duration" to runtime.game.speed.peaceDealDuration)) else emptyList()
            val proposal = rome.communication(obj("op" to "trade_offer", "epoch" to runtime.executor.epoch, "requestId" to "offer", "partner" to "Greece", "give" to treaty + obj("type" to "Gold", "name" to "Gold", "amount" to 50, "duration" to -1), "receive" to treaty))["trade"]!!.jsonObject
            request = obj("op" to "trade_decide", "epoch" to runtime.executor.epoch, "requestId" to "accept", "proposal" to proposal["proposal"], "version" to 1, "decision" to "accept")
            failCommit = true
            check(runCatching { greece.communication(request) }.isFailure)
            check(runtime.executor.mutationFailure != null)
            check(runCatching { rome.turn() }.isFailure)
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restarted")).use { runtime ->
            val greece = PlayerHost(runtime).bindPlayer("Greece")
            val gold = runtime.game.getCivilization("Greece").gold
            check(gold == if (afterReplace) 1050 else 1000)
            if (peace) check(runtime.game.getCivilization("Rome").isAtWarWith(runtime.game.getCivilization("Greece")) == !afterReplace)
            val prior = greece.communication(obj("op" to "receipt", "epoch" to runtime.executor.epoch, "requestId" to "accept"))
            check((prior["outcome"] != null) == afterReplace)
            check(greece.communication(request)["outcome"] != null)
            check(runtime.game.getCivilization("Greece").gold == 1050)
            check(runtime.game.getCivilization("Rome").gold == 950)
            check(runtime.game.getCivilization("Greece").getDiplomacyManager("Rome")!!.trades.size == 1)
            check(!runtime.game.getCivilization("Rome").isAtWarWith(runtime.game.getCivilization("Greece")))
        }
    }
}
