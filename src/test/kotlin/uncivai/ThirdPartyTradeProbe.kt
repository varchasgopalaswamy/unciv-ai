package uncivai

import com.unciv.json.json
import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.trade.Trade
import com.unciv.logic.trade.TradeLogic
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import com.unciv.models.metadata.BaseRuleset
import kotlinx.serialization.json.Json
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
import java.nio.file.Path
import java.util.UUID

/** Native trade-screen equivalence, private consent and HTTPS recovery for multi-player wars. */
object ThirdPartyTradeProbe {
    private val war = obj("type" to "WarDeclaration", "name" to "France", "amount" to 1, "duration" to -1)
    private val gold = obj("type" to "Gold", "name" to "Gold", "amount" to 30, "duration" to -1)
    private val tokens = ThirdPartyTradeScenarioSetup.civilizations.associateWith { it.first().toString().repeat(43) }

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        lifecycle(root.resolve("lifecycle"))
        privacyAndLegality(root.resolve("privacy"))
        commitments(root.resolve("commitments"))
        for (mode in listOf("joint", "join", "defensive", "counter")) nativeEffects(root.resolve(mode), mode)
        nativeEffects(root.resolve("gnk"), "joint", BaseRuleset.Civ_V_GnK)
        for (afterReplace in listOf(false, true)) storageFailure(root.resolve("failure-$afterReplace"), afterReplace)
    }

    private fun create(root: Path, ruleset: BaseRuleset = BaseRuleset.Civ_V_Vanilla) = HeadlessRuntime.create(root) { ThirdPartyTradeScenarioSetup.create(ruleset) }

    private fun lifecycle(root: Path) {
        val checkpoint = root.resolve("game.json")
        lateinit var round: GameCheckpoint
        lateinit var proposal: JsonObject
        lateinit var accept: JsonObject
        lateinit var receipt: JsonObject
        create(root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls1"), tokens).use { api ->
                api.acquire()
                api.activate()
                api.operator(OperatorRequest("barrier", api.lease))
                round = checkNotNull(api.operator(OperatorRequest("snapshot", api.lease)).checkpoint)
                api.operator(OperatorRequest("unbarrier", api.lease))
                api.activate()
                val calls = Calls(runtime, api)
                for ((side, offers) in listOf("give" to TradeLogic(runtime.game.getCivilization("Rome"), runtime.game.getCivilization("Greece")).ourAvailableOffers, "receive" to TradeLogic(runtime.game.getCivilization("Rome"), runtime.game.getCivilization("Greece")).theirAvailableOffers)) {
                    val native = offers.filter { it.type == TradeOfferType.WarDeclaration }.map { TradeSelection(it.type, it.name, it.amount, it.duration) }
                    val exposed = calls.options().getValue(side).jsonArray.map { it.jsonObject }.filter { it["type"]!!.jsonPrimitive.content == "WarDeclaration" }.map { TradeSelection(TradeOfferType.WarDeclaration, it["name"]!!.jsonPrimitive.content, it["amount"]!!.jsonPrimitive.int, it["duration"]!!.jsonPrimitive.int) }
                    check(exposed == native && exposed.single().name == "France")
                }
                proposal = calls.offer().getValue("trade").jsonObject
                check(proposal["status"]!!.jsonPrimitive.content == "PENDING")
                check(!runtime.game.getCivilization("Greece").isAtWarWith(runtime.game.getCivilization("France")))
                check(calls.query("France", "state")["trades"]!!.jsonArray.isEmpty())
                val hidden = calls.query("France", "trade_read", "proposal" to proposal["proposal"])
                check(hidden == calls.query("France", "trade_read", "proposal" to "nonexistent"))
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("pending-restart")).use { runtime ->
            GameplayTestApi(runtime, root.resolve("tls2"), tokens).use { api ->
                api.acquire()
                val token = checkNotNull(api.activate().playerToken)
                val calls = Calls(runtime, api)
                val before = api.turn(token)
                val uninvolved = api.turn(api.playerToken("Egypt"))
                val expected = nativeTrade(runtime.game, listOf(gold), listOf(war))
                val handoff = runtime.executor.handoff
                check(calls.query("Rome", "trade_read", "proposal" to proposal["proposal"])["trade"]!!.jsonObject["clauses"] == proposal["clauses"])
                val warnings = calls.query("Greece", "trade_read", "proposal" to proposal["proposal"])["trade"]!!.jsonObject["yourWarDeclarations"]!!.jsonArray
                check(warnings.single().jsonObject["confirmation"].toString().contains("France"))
                accept = calls.request("trade_decide", "proposal" to proposal["proposal"], "version" to 1, "decision" to "accept")
                receipt = api.communicate(api.playerToken("Greece"), accept)
                check(receipt["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "ENACTED")
                sameEffects(expected, runtime.game)
                val outcome = receipt.getValue("outcome").jsonObject
                check(outcome.getValue("wars").jsonArray.single().jsonObject == obj("declaringCivilization" to "Greece", "targetCivilization" to "France", "atWar" to true))
                check(runtime.game.currentPlayer == "Rome" && runtime.executor.handoff == handoff)
                check(runtime.game.getCivilization("Rome").gold == 970 && runtime.game.getCivilization("Greece").gold == 1030)
                check(api.communicate(api.playerToken("Greece"), accept) == receipt)
                check(calls.decide(proposal)["outcome"] == outcome)
                val afterUninvolved = api.turn(api.playerToken("Egypt"))
                // Every player sees the global gameplay hold open/close, without its terms.
                check(uninvolved.negotiation.keys == setOf("active") && afterUninvolved.negotiation.keys == setOf("active"))
                check(afterUninvolved.copy(revision = uninvolved.revision, negotiation = uninvolved.negotiation) == uninvolved)
                check(calls.query("Egypt", "read")["events"]!!.jsonArray.isEmpty())
                val targetState = calls.query("France", "state")
                check(targetState["trades"]!!.jsonArray.isEmpty())
                val targetOutcome = targetState["outcomes"]!!.jsonArray.single().jsonObject
                check(targetOutcome["action"]!!.jsonPrimitive.content == "DECLARE_WAR")
                check(targetOutcome["participants"]!!.jsonArray.map { it.jsonPrimitive.content }.toSet() == setOf("Greece", "France"))
                check(targetOutcome["proposal"] == JsonNull && targetOutcome["clauses"]!!.jsonArray.isEmpty())
                check(!targetOutcome.toString().contains(proposal["proposal"]!!.jsonPrimitive.content))
                check(calls.query("France", "read")["events"]!!.jsonArray.single().jsonObject["wake"]!!.jsonPrimitive.boolean)
                api.reject(token, TurnCommandRequest("stale-war", before.epoch, before.revision, TurnCommandKind.END_TURN).reviewedFor(before), "STALE_REVISION")
                val france = api.turn(api.playerToken("France"))
                check(france.alerts.any { it.kind == "WarDeclaration" && it.content != null })
                api.finishTurn(token)
                repeat(3) { api.finishTurn(checkNotNull(api.activate().playerToken)) }
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("settled-restart")).use { runtime ->
            val greece = PlayerHost(runtime).bindPlayer("Greece")
            val balance = runtime.game.getCivilization("Greece").gold
            check(greece.communication(accept) == receipt)
            check(runtime.game.getCivilization("Greece").gold == balance)
            check(runtime.game.getCivilization("Greece").isAtWarWith(runtime.game.getCivilization("France")))
            runtime.executor.pause()
            runtime.executor.communicationPaused = true
            runtime.executor.restoreRound(round, "third-party-restored")
            check(runtime.executor.communicationState.trades.proposals.isEmpty())
            check(runtime.executor.communicationState.outcomes.isEmpty())
            runtime.executor.accessGame { game ->
                check(game.getCivilization("Rome").gold == 1000 && game.getCivilization("Greece").gold == 1000)
                check(!game.getCivilization("Greece").isAtWarWith(game.getCivilization("France")))
            }
            check(runCatching { greece.communication(accept) }.isFailure)
        }
    }

    private fun privacyAndLegality(root: Path) {
        create(root).use { runtime ->
            GameplayTestApi(runtime, root.resolve("tls"), tokens).use { api ->
                val calls = Calls(runtime, api)
                val original = calls.options()
                val revision = api.turn(api.playerToken("Rome")).revision
                val france = runtime.game.getCivilization("France")
                france.addGold(1234)
                // Completed rival technologies can legitimately change our displayed
                // research costs; a private research order cannot.
                france.tech.techsToResearch = arrayListOf("Mining")
                france.cities.single().cityConstructions.constructionQueue.clear()
                france.cities.single().cityConstructions.addToQueue("Monument")
                check(calls.options() == original)
                check(api.turn(api.playerToken("Rome")).revision == revision)
                check(original.getValue("give").jsonArray.none { it.jsonObject["type"]!!.jsonPrimitive.content in setOf("Introduction", "PeaceProposal") })
                for (invalid in listOf(
                    JsonObject(war + obj("name" to "Egypt")),
                    JsonObject(war + obj("name" to "Atlantis")),
                    JsonObject(war + obj("name" to "Rome")),
                    JsonObject(war + obj("name" to "Greece")),
                    JsonObject(war + obj("amount" to 2)),
                    JsonObject(war + obj("amount" to 0)),
                    JsonObject(war + obj("duration" to 10)),
                    JsonObject(war + obj("type" to "PeaceProposal")),
                    JsonObject(war + obj("type" to "Introduction")),
                )) {
                    val before = json().toJson(runtime.game)
                    check(calls.offer(receive = listOf(invalid))["error"] != null)
                    check(json().toJson(runtime.game) == before)
                }
                check(calls.offer(receive = listOf(war, war))["error"] != null)
                runtime.game.getCivilization("Greece").diplomacyFunctions.makeCivilizationsMeet(runtime.game.getCivilization("Egypt"))
                check(calls.offer(receive = listOf(JsonObject(war + obj("name" to "Egypt"))))["error"] != null)
                check(calls.options().getValue("receive").jsonArray.none { it.jsonObject["type"]!!.jsonPrimitive.content == "WarDeclaration" && it.jsonObject["name"]!!.jsonPrimitive.content == "Egypt" })
                check(calls.command("Greece", "trade_offer", "partner" to "Rome", "give" to listOf(war), "receive" to listOf(gold))["error"] != null)
                val pending = calls.offer().getValue("trade").jsonObject
                val counter = calls.command("Greece", "trade_offer", "partner" to "Rome", "proposal" to pending["proposal"], "version" to 1, "give" to listOf(war), "receive" to listOf(JsonObject(gold + obj("amount" to 40)))).getValue("trade").jsonObject
                check(calls.decide(pending)["error"] != null)
                check(runtime.game.getCivilization("Rome").gold == 1000)
                // Engine state can change at a loaded/operator boundary; consent revalidates.
                runtime.game.getCivilization("Greece").getDiplomacyManager(france)!!.declareWar()
                check(calls.decide(counter, "Rome")["error"] != null)
                val greece = runtime.game.getCivilization("Greece")
                greece.getDiplomacyManager(france)!!.makePeace()
                val treaty = Trade().apply {
                    ourOffers.add(TradeOffer("Peace Treaty", TradeOfferType.Treaty, 1, 3))
                    theirOffers.add(TradeOffer("Peace Treaty", TradeOfferType.Treaty, 1, 3))
                }
                greece.getDiplomacyManager(france)!!.trades.add(treaty)
                france.getDiplomacyManager(greece)!!.trades.add(treaty.reverse())
                check(calls.offer()["error"] != null) // An active peace treaty forbids the declaration.
                check(calls.options().getValue("receive").jsonArray.none { it.jsonObject["type"]!!.jsonPrimitive.content == "WarDeclaration" })
                france.cities.single().moveToCiv(runtime.game.getCivilization("Rome"))
                check(france.isDefeated())
                check(calls.options().getValue("give").jsonArray.none { it.jsonObject["type"]!!.jsonPrimitive.content == "WarDeclaration" })
                check(runtime.game.getCivilization("Rome").gold == 1000)
                check(calls.options().getValue("receive").jsonArray.none { it.jsonObject["type"]!!.jsonPrimitive.content == "WarDeclaration" })
                check(calls.decide(counter, "Rome")["error"] != null)
            }
        }
    }

    private fun commitments(root: Path) {
        create(root).use { runtime ->
            GameplayTestApi(runtime, root.resolve("tls"), tokens).use { api ->
                val calls = Calls(runtime, api)
                fun promise(civ: String): JsonObject {
                    val result = calls.command(civ, "offer", "participants" to listOf(civ, "France"), "kind" to "peace", "terms" to "Do not attack", "conditions" to "Unconditional").getValue("offer").jsonObject
                    calls.command("France", "accept", "proposal" to result["proposal"], "version" to 1)
                    return result
                }
                fun reconsider(civ: String, offer: JsonObject) = calls.command(civ, "reconsider", "proposal" to offer["proposal"], "version" to 1, "reason" to "Deliberately change our foreign policy")
                val sponsorPromise = promise("Rome")
                check(calls.offer()["error"] != null) // Hiring a proxy cannot bypass a peace commitment.
                reconsider("Rome", sponsorPromise)
                val pending = calls.offer().getValue("trade").jsonObject
                val before = calls.query("Rome", "trade_read", "proposal" to pending["proposal"])
                val signerPromise = promise("Greece")
                check(calls.query("Rome", "trade_read", "proposal" to pending["proposal"]) == before)
                check(calls.decide(pending)["error"] != null)
                check(calls.query("Rome", "trade_read", "proposal" to pending["proposal"]) == before)
                reconsider("Greece", signerPromise)
                val renewedPromise = promise("Rome")
                check(calls.decide(pending)["error"] != null) // The author's earlier consent is fenced.
                val closed = calls.query("Greece", "trade_read", "proposal" to pending["proposal"])
                check(closed["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "INVALID")
                check(!closed.toString().contains(renewedPromise["proposal"]!!.jsonPrimitive.content))
                check(!closed.toString().contains("Do not attack"))
                check(runtime.game.getCivilization("Rome").gold == 1000)
                reconsider("Rome", renewedPromise)
                val fresh = calls.offer().getValue("trade").jsonObject
                check(calls.decide(fresh)["outcome"] != null)
            }
        }
    }

    private fun nativeEffects(root: Path, mode: String, ruleset: BaseRuleset = BaseRuleset.Civ_V_Vanilla) {
        create(root, ruleset).use { runtime ->
            val game = runtime.game
            val rome = game.getCivilization("Rome")
            val greece = game.getCivilization("Greece")
            val france = game.getCivilization("France")
            val egypt = game.getCivilization("Egypt")
            if (mode == "join") rome.getDiplomacyManager(france)!!.declareWar()
            if (mode == "defensive") {
                france.diplomacyFunctions.makeCivilizationsMeet(egypt)
                greece.diplomacyFunctions.makeCivilizationsMeet(egypt)
                france.getDiplomacyManager(egypt)!!.signDefensivePact(20)
            }
            val give = if (mode == "joint") listOf(gold, war) else listOf(gold)
            GameplayTestApi(runtime, root.resolve("tls"), tokens).use { api ->
                val calls = Calls(runtime, api)
                val expected = if (mode == "counter") nativeTrade(game, listOf(war), give, "Greece", "Rome") else nativeTrade(game, give, listOf(war))
                var proposal = calls.offer(give).getValue("trade").jsonObject
                if (mode == "counter") {
                    proposal = calls.command("Greece", "trade_offer", "partner" to "Rome", "proposal" to proposal["proposal"], "version" to 1, "give" to listOf(war), "receive" to give).getValue("trade").jsonObject
                }
                if (mode == "defensive") {
                    val forRome = calls.query("Rome", "trade_read", "proposal" to proposal["proposal"])
                    val forGreece = calls.query("Greece", "trade_read", "proposal" to proposal["proposal"])
                    check(!forRome.toString().contains("Egypt"))
                    check(forGreece["trade"]!!.jsonObject["yourWarDeclarations"].toString().contains("Egypt"))
                    check(!calls.options().toString().contains("Egypt"))
                }
                val result = calls.decide(proposal, if (mode == "counter") "Rome" else "Greece")
                check(result["outcome"] != null) { result.toString() }
                sameEffects(expected, game)
                check(greece.isAtWarWith(france))
                check(rome.isAtWarWith(france) == (mode in setOf("joint", "join")))
                if (mode == "defensive") check(greece.isAtWarWith(egypt) && !rome.knows(egypt))
                if (mode == "joint") check(france.popupAlerts.count { it.type == AlertType.WarDeclaration } == 2)
                check(greece.getDiplomacyManager(france)!!.getFlag(DiplomacyFlags.DeclaredWar) > 0)
            }
        }
    }

    private fun storageFailure(root: Path, afterReplace: Boolean) {
        val checkpoint = root.resolve("game.json")
        lateinit var request: JsonObject
        var fail = false
        create(root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(CheckpointStore(checkpoint, beforeReplace = { if (fail && !afterReplace) error("Storage failure") }, afterReplace = { if (fail && afterReplace) error("Lost response") }))
            val rome = PlayerHost(runtime).bindPlayer("Rome")
            val greece = PlayerHost(runtime).bindPlayer("Greece")
            val proposal = rome.communication(obj("op" to "trade_offer", "epoch" to runtime.epoch, "requestId" to "offer", "partner" to "Greece", "give" to listOf(gold), "receive" to listOf(war))).getValue("trade").jsonObject
            request = obj("op" to "trade_decide", "epoch" to runtime.epoch, "requestId" to "accept", "proposal" to proposal["proposal"], "version" to 1, "decision" to "accept")
            fail = true
            check(runCatching { greece.communication(request) }.isFailure)
            check(runtime.executor.mutationFailure != null)
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            val greece = runtime.game.getCivilization("Greece")
            val session = PlayerHost(runtime).bindPlayer("Greece")
            check(greece.gold == if (afterReplace) 1030 else 1000)
            check(greece.isAtWarWith(runtime.game.getCivilization("France")) == afterReplace)
            check(runtime.executor.communicationState.outcomes.size == if (afterReplace) 2 else 0)
            check(session.communication(request)["outcome"] != null)
            check(greece.gold == 1030 && runtime.game.getCivilization("Rome").gold == 970)
            check(runtime.executor.communicationState.outcomes.size == 2)
            check(greece.getDiplomacyManager("Rome")!!.trades.size == 1)
        }
    }

    /** The exact operations invoked by the native trade screen on an equivalent world. */
    private fun nativeTrade(game: GameInfo, give: List<JsonObject>, receive: List<JsonObject>, author: String = "Rome", partner: String = "Greece"): GameInfo = game.clone().apply {
        setTransients()
        val trade = Trade()
        fun item(value: JsonObject) = TradeOffer(value["name"]!!.jsonPrimitive.content, TradeOfferType.valueOf(value["type"]!!.jsonPrimitive.content), value["amount"]!!.jsonPrimitive.int, value["duration"]!!.jsonPrimitive.int)
        trade.ourOffers.addAll(give.map(::item))
        trade.theirOffers.addAll(receive.map(::item))
        TradeLogic(getCivilization(author), getCivilization(partner)).apply {
            currentTrade.set(trade)
            acceptTrade()
        }
    }

    private fun sameEffects(expected: GameInfo, actual: GameInfo) {
        fun native(value: Any) = Json.parseToJsonElement(json().toJson(value))
        for (id in ThirdPartyTradeScenarioSetup.civilizations) {
            val a = expected.getCivilization(id)
            val b = actual.getCivilization(id)
            check(a.gold == b.gold) { "$id gold differs from desktop" }
            check(native(a.diplomacy) == native(b.diplomacy)) { "$id diplomacy differs from desktop" }
            check(native(a.popupAlerts) == native(b.popupAlerts)) { "$id alerts differ from desktop" }
            check(native(a.notifications) == native(b.notifications)) { "$id notifications differ from desktop" }
            check(a.viewableTiles.map { it.position }.toSet() == b.viewableTiles.map { it.position }.toSet())
        }
    }

    private class Calls(val runtime: HeadlessRuntime, val api: GameplayTestApi) {
        fun request(op: String, vararg fields: Pair<String, Any?>) = obj("op" to op, "epoch" to runtime.epoch, "requestId" to UUID.randomUUID().toString(), *fields)
        fun command(civ: String, op: String, vararg fields: Pair<String, Any?>) = api.communicate(api.playerToken(civ), request(op, *fields))
        fun query(civ: String, op: String, vararg fields: Pair<String, Any?>) = api.communicate(api.playerToken(civ), obj("op" to op, "epoch" to runtime.epoch, *fields))
        fun options() = query("Rome", "trade_options", "partner" to "Greece")
        fun offer(give: List<JsonObject> = listOf(gold), receive: List<JsonObject> = listOf(war)) = command("Rome", "trade_offer", "partner" to "Greece", "give" to give, "receive" to receive)
        fun decide(proposal: JsonObject, civ: String = "Greece") = command(civ, "trade_decide", "proposal" to proposal["proposal"], "version" to proposal["version"], "decision" to "accept")
    }
}
