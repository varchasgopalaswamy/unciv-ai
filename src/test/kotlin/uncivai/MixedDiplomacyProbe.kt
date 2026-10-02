package uncivai

import com.unciv.json.json
import com.unciv.logic.automation.civilization.TradeAutomation
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.PopupAlert
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.trade.Trade
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import com.unciv.logic.trade.TradeRequest
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerHost
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

/** HTTPS actions with real native AI decisions, five majors and a durable pending-request restart. */
object MixedDiplomacyProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        Files.createDirectories(root)
        val checkpoint = root.resolve("mixed.json")
        val tokens = mapOf("Rome" to "R".repeat(43), "Greece" to "G".repeat(43), "France" to "F".repeat(43), "Egypt" to "E".repeat(43))
        lateinit var pending: JsonObject
        lateinit var sent: JsonObject
        lateinit var receipt: JsonObject
        lateinit var friendship: JsonObject
        var goldBefore = 0
        HeadlessRuntime.createMixedDiplomacyExercise(root.resolve("initial")).use { runtime ->
            check(runtime.game.civilizations.count { it.isMajorCiv() } == 5)
            check(runtime.game.civilizations.any { it.isCityState })
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls"), tokens).use { api ->
                api.acquire()
                api.activate()
                val rome = runtime.game.getCivilization("Rome")
                val china = runtime.game.getCivilization("China")
                val greece = runtime.game.getCivilization("Greece")
                fun command(op: String, vararg fields: Pair<String, Any?>) = api.communicate(tokens.getValue("Rome"), obj("epoch" to runtime.epoch, "op" to op, *if (op in setOf("state", "trade_read", "trade_options")) emptyArray() else arrayOf("requestId" to UUID.randomUUID().toString()), *fields))
                command("send", "to" to "Greece", "text" to "We denounce your conduct.")
                check(!rome.getDiplomacyManager(greece)!!.hasFlag(DiplomacyFlags.Denunciation))
                val before = api.turn(tokens.getValue("Rome"))
                val native = obj("kind" to "ISSUE_DEMAND", "target" to "China", "selection" to "DoNotSettleNearUs", "revision" to before.revision)
                val demandRequest = obj("op" to "send", "epoch" to runtime.epoch, "requestId" to "native-demand", "to" to "China", "text" to "Please settle elsewhere.", "nativeAction" to native)
                val demandReceipt = api.communicate(tokens.getValue("Rome"), demandRequest)
                check(demandReceipt["nativeOutcome"]?.jsonObject?.get("status")?.jsonPrimitive?.content == "QUEUED") { demandReceipt.toString() }
                check(api.communicate(tokens.getValue("Rome"), demandRequest) == demandReceipt)
                check(china.popupAlerts.count { it.type == AlertType.DemandToStopSettlingCitiesNear } == 1)
                // Imported native AI demands remain decisions, not inferred social promises.
                rome.popupAlerts.add(PopupAlert(AlertType.DemandToStopSettlingCitiesNear, china.civID))
                val demandTurn = api.turn(tokens.getValue("Rome"))
                val alert = demandTurn.alerts.single { it.nativeDiplomacy?.civilizationId == "China" }
                check(alert.nativeDiplomacy!!.sender == "China" && alert.nativeDiplomacy!!.recipient == "Rome")
                check(alert.nativeDiplomacy!!.event == AlertType.DemandToStopSettlingCitiesNear.name)
                check(alert.nativeDiplomacy!!.demand == "DoNotSettleNearUs")
                check(alert.nativeDiplomacy!!.options.none { it.declaresWar })
                val response = command("send", "to" to "China", "text" to "We accept.", "nativeAction" to obj("kind" to "RESPOND_DIPLOMACY", "target" to alert.handle, "selection" to "Accept", "revision" to demandTurn.revision))
                check(response["error"] == null) { response.toString() }
                check(china.getDiplomacyManager(rome)!!.hasFlag(DiplomacyFlags.AgreedToNotSettleNearUs))
                // A human-facing offer remains on the shared route and creates no native request.
                val shared = command("trade_offer", "partner" to "France", "give" to listOf(gold(5)), "receive" to emptyList<JsonObject>())["trade"]!!.jsonObject
                check(shared["mode"]!!.jsonPrimitive.content == "SHARED")
                check(runtime.game.getCivilization("France").tradeRequests.isEmpty())
                check(runtime.executor.communicationState.streams.getValue("France").any { it.kind == "trade" && it.body["proposal"] == shared["proposal"] && it.wake })
                val accept = api.communicate(tokens.getValue("France"), obj("op" to "trade_decide", "epoch" to runtime.epoch, "requestId" to "human-accept", "proposal" to shared["proposal"], "version" to shared["version"], "decision" to "accept"))
                check(accept["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "ENACTED")
                // Native AI requests never acquire the shared pause and have no wall-clock deadline.
                val options = command("trade_options", "partner" to "China")
                check(options["mode"]!!.jsonPrimitive.content == "NATIVE_AI")
                goldBefore = rome.gold
                sent = obj("epoch" to runtime.epoch, "op" to "trade_offer", "requestId" to "native-gift", "partner" to "China", "give" to listOf(gold(25)), "receive" to emptyList<JsonObject>())
                receipt = api.communicate(tokens.getValue("Rome"), sent)
                pending = receipt["trade"]!!.jsonObject
                check(pending["proposal"]!!.jsonPrimitive.content.matches(Regex("d[0-9a-z]{5}")))
                check(pending["proposal"] != shared["proposal"])
                check(pending["status"]!!.jsonPrimitive.content == "PENDING") { receipt.toString() }
                check(pending["pausesGameplay"]!!.jsonPrimitive.boolean.not())
                check(runtime.executor.communicationState.trades.activeProposal == null)
                check(rome.gold == goldBefore)
                check(china.tradeRequests.single { it.requestingCiv == "Rome" }.requestId.isNotEmpty())
                val franceLedger = api.communicate(tokens.getValue("France"), obj("op" to "state", "epoch" to runtime.epoch))
                check(pending["proposal"].toString() !in franceLedger.toString())
                // Unknown and private references give the same diagnostics, containing
                // only this reader's pending proposals and no private existence signal.
                val privateRead = api.communicate(tokens.getValue("France"), obj("op" to "trade_read", "epoch" to runtime.epoch, "proposal" to pending["proposal"]))
                val absentRead = api.communicate(tokens.getValue("France"), obj("op" to "trade_read", "epoch" to runtime.epoch, "proposal" to "dabsent"))
                check(privateRead["error"]!!.jsonPrimitive.content == "UNKNOWN_TRADE")
                check(privateRead - "proposal" == absentRead - "proposal")
                check(privateRead["pendingTrades"]!!.jsonArray.isEmpty())
                check(api.communicate(tokens.getValue("Rome"), sent) == receipt)
                friendship = command("friendship_offer", "partner" to "China")["trade"]!!.jsonObject
                check(friendship["status"]!!.jsonPrimitive.content == "PENDING")
                verifyNativeDelivery(runtime)
                runtime.executor.commitCheckpoint()
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("resumed")).use { runtime ->
            GameplayTestApi(runtime, root.resolve("tls-resumed"), tokens).use { api ->
                api.acquire()
                val activeRome = checkNotNull(api.activate().playerToken)
                check(api.communicate(tokens.getValue("Rome"), sent) == receipt)
                val rome = runtime.game.getCivilization("Rome")
                val china = runtime.game.getCivilization("China")
                runtime.executor.accessGame {
                    TradeAutomation.respondToTradeRequests(china, true)
                    runtime.executor.commitCheckpoint()
                }
                check(rome.gold == goldBefore - 25)
                fun command(op: String, vararg fields: Pair<String, Any?>) = api.communicate(tokens.getValue("Rome"), obj("epoch" to runtime.epoch, "op" to op, *if (op in setOf("state", "trade_read", "trade_options")) emptyArray() else arrayOf("requestId" to UUID.randomUUID().toString()), *fields))
                val enacted = command("trade_read", "proposal" to pending["proposal"])["trade"]!!.jsonObject
                check(enacted["status"]!!.jsonPrimitive.content == "ENACTED") { enacted.toString() }
                val transferred = rome.gold
                runtime.executor.accessGame { TradeAutomation.respondToTradeRequests(china, true) }
                check(rome.gold == transferred)
                check(api.communicate(tokens.getValue("Rome"), sent) == receipt)
                // A native incoming offer becomes a canonical decision and is accepted once.
                runtime.executor.accessGame {
                    rome.tradeRequests.add(
                        TradeRequest(
                            china.civID,
                            Trade().apply {
                                theirOffers.add(TradeOffer("Gold", TradeOfferType.Gold, 13, runtime.game.speed))
                            },
                        ),
                    )
                }
                val snapshot = api.turn(tokens.getValue("Rome"))
                check(snapshot.pending.single { it.kind == "TradeRequests" }.supported)
                val incoming = command("state")["trades"]!!.jsonArray.map { it.jsonObject }.single { it["author"]!!.jsonPrimitive.content == "China" && it["status"]!!.jsonPrimitive.content == "PENDING" }
                val reference = incoming["proposal"]!!.jsonPrimitive.content
                check(reference.matches(Regex("d[0-9a-z]{5}")))
                val beforeTypo = rome.gold
                val typo = command("trade_read", "proposal" to reference + "2")
                check(typo["error"]!!.jsonPrimitive.content == "UNKNOWN_TRADE")
                check(typo["pendingTrades"]!!.jsonArray.any { it.jsonObject["proposal"] == incoming["proposal"] })
                val rejectedDecision = command("trade_decide", "proposal" to reference + "2", "version" to 1, "decision" to "accept")
                check(rejectedDecision == typo)
                val rejectedCounter = command("trade_offer", "proposal" to reference + "2", "version" to 1, "partner" to "China", "give" to listOf(gold(1)), "receive" to emptyList<JsonObject>())
                check(rejectedCounter == typo)
                check(rome.gold == beforeTypo && rome.tradeRequests.any { it.requestingCiv == china.civID })
                val read = command("trade_read", "proposal" to incoming["proposal"])["trade"]!!.jsonObject
                check(read["acceptAvailable"]!!.jsonPrimitive.boolean) { read.toString() }
                verifyNativeDelivery(runtime)
                val queued = rome.tradeRequests.single { it.requestingCiv == china.civID }
                val changedTerm = queued.trade.theirOffers.first()
                val originalDuration = changedTerm.duration
                changedTerm.duration = originalDuration + 1
                val changedConsent = command("trade_decide", "proposal" to read["proposal"], "version" to read["version"], "decision" to "accept")
                check(changedConsent["error"] != null && runtime.executor.mutationFailure == null)
                check(rome.gold == beforeTypo && rome.tradeRequests.any { it === queued })
                changedTerm.duration = originalDuration
                val accepted = command("trade_decide", "proposal" to read["proposal"], "version" to read["version"], "decision" to "accept")
                check(accepted["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "ENACTED")
                check(rome.gold == transferred + 13)
                exerciseOutcomes(runtime, api, tokens.getValue("Rome"))
                // Explicit denouncement is atomic with its message; stale retries cannot duplicate it.
                val current = api.turn(tokens.getValue("Rome"))
                val denounce = obj("op" to "send", "epoch" to runtime.epoch, "requestId" to "denounce-greece", "to" to "Greece", "text" to "We denounce you.", "nativeAction" to obj("kind" to "DENOUNCE", "target" to "Greece", "revision" to current.revision))
                val denunciation = api.communicate(tokens.getValue("Rome"), denounce)
                check(denunciation["error"] == null) { denunciation.toString() }
                val nativeState = json().toJson(runtime.game)
                check(api.communicate(tokens.getValue("Rome"), denounce) == denunciation)
                check(json().toJson(runtime.game) == nativeState)
                check(runtime.game.getCivilization("Greece").popupAlerts.count { it.type == AlertType.Denounced } == 1)
                // Let actual native next-turn processing answer a new gift and friendship request.
                val finalGift = command("trade_offer", "partner" to "China", "give" to listOf(gold(9)), "receive" to emptyList<JsonObject>())["trade"]!!.jsonObject
                finishDiplomacy(api, activeRome, runtime.epoch)
                api.finishTurn(activeRome)
                check(api.status().activeCivilization == "Greece")
                for (civilization in listOf("Greece", "France", "Egypt")) {
                    check(api.status().activeCivilization == civilization)
                    val token = checkNotNull(api.activate().playerToken)
                    finishDiplomacy(api, token, runtime.epoch)
                    try {
                        api.finishTurn(token)
                    } catch (failure: Throwable) {
                        throw runtime.executor.mutationFailure ?: failure
                    }
                }
                check(api.status().activeCivilization == "Rome" && runtime.game.turns == 1)
                val nativeTurnResult = command("trade_read", "proposal" to finalGift["proposal"])["trade"]!!.jsonObject
                check(nativeTurnResult["status"]!!.jsonPrimitive.content == "ENACTED") { nativeTurnResult.toString() }
                val friendshipResult = command("trade_read", "proposal" to friendship["proposal"])["trade"]!!.jsonObject
                val friendshipStatus = friendshipResult["status"]!!.jsonPrimitive.content
                check(friendshipStatus in setOf("ENACTED", "DECLINED")) { friendshipResult.toString() }
                check(rome.getDiplomacyManager(china)!!.hasFlag(DiplomacyFlags.DeclarationOfFriendship) == (friendshipStatus == "ENACTED"))
                verifyNativeDelivery(runtime)
                val nativeEndings = runtime.executor.communicationState.streams.getValue("Rome").filter { it.kind == "agreement" && it.body["partner"]?.jsonPrimitive?.content == "China" }
                check(nativeEndings.isNotEmpty() && nativeEndings.none { it.wake })
                Files.writeString(root.resolve("evidence.json"), obj("pending" to pending, "enacted" to enacted, "incomingAcceptance" to accepted, "denunciation" to denunciation, "nativeTurnResult" to nativeTurnResult, "friendshipResult" to friendshipResult, "nativeTurn" to runtime.game.turns).toString())
            }
        }
        for (afterReplace in listOf(false, true)) verifyFailedMessage(root.resolve("failed-message-$afterReplace"), afterReplace)
        verifyMediatedPeace(root.resolve("mediated-peace"))
        verifyResponseAdvice(root.resolve("response-advice"))
        verifyIncidentFacts(root.resolve("incident-facts"))
    }

    private fun gold(amount: Int) = obj("type" to "Gold", "name" to "Gold", "amount" to amount, "duration" to -1)

    private fun verifyNativeDelivery(runtime: HeadlessRuntime) {
        val state = runtime.executor.communicationState
        val nativeIds = state.trades.proposals.values.map { it.last() }.filter { it.mode == "NATIVE_AI" }.map { it.proposal }.toSet()
        val events = state.streams.values.flatten().filter { it.body["proposal"]?.jsonPrimitive?.content in nativeIds }
        check(events.isNotEmpty() && events.none { it.wake }) { "Native offers and outcomes must remain recorded without waking communication jobs" }
    }

    private fun verifyResponseAdvice(root: Path) {
        for ((type, choice) in listOf(AlertType.Denounced to "DeclareWar", AlertType.DemandToNotAttackUs to "Refuse")) {
            HeadlessRuntime.createMixedDiplomacyExercise(root.resolve(choice)).use { runtime ->
                val roman = runtime.game.getCivilization("Rome")
                val greek = runtime.game.getCivilization("Greece")
                val rome = PlayerHost(runtime).bindPlayer("Rome")
                val greece = PlayerHost(runtime).bindPlayer("Greece")
                val terms = "We intend to keep the border peaceful; exceptions are open to discussion."
                val offered = rome.communication(obj("op" to "offer", "epoch" to runtime.epoch, "requestId" to "social-peace", "participants" to listOf("Rome", "Greece"), "kind" to "peace", "terms" to terms, "conditions" to "Unconditional"))["offer"]!!.jsonObject
                greece.communication(obj("op" to "accept", "epoch" to runtime.epoch, "requestId" to "social-accept", "proposal" to offered["proposal"], "version" to 1))
                roman.popupAlerts.add(PopupAlert(type, greek.civID))
                val turn = rome.turn()
                val alert = turn.alerts.single { it.nativeDiplomacy != null }
                val response = alert.nativeDiplomacy!!.options.single { it.name == choice }
                check(response.available && response.socialAdvisories.single().terms == terms)
                check(response.declaresWar)
                val result = rome.communication(obj("op" to "send", "epoch" to runtime.epoch, "requestId" to "native-war-response", "to" to "Greece", "text" to "We choose war despite that social promise.", "nativeAction" to obj("kind" to "RESPOND_DIPLOMACY", "target" to alert.handle, "selection" to choice, "revision" to turn.revision)))
                check(result["nativeOutcome"]!!.jsonObject["atWar"]!!.jsonPrimitive.boolean)
                check(roman.isAtWarWith(greek))
                check(rome.turn().socialAdvisories.single().terms == terms)
                check(runtime.executor.communicationState.outcomes.count { it.action == "DECLARE_WAR" } == 1)
            }
        }
    }

    private fun verifyIncidentFacts(root: Path) {
        HeadlessRuntime.createMixedDiplomacyExercise(root).use { runtime ->
            val roman = runtime.game.getCivilization("Rome")
            val greek = runtime.game.getCivilization("Greece")
            val minor = runtime.game.civilizations.first { it.isCityState }
            roman.diplomacyFunctions.makeCivilizationsMeet(minor)
            roman.popupAlerts.clear()
            val rome = PlayerHost(runtime).bindPlayer("Rome")
            roman.popupAlerts.add(PopupAlert(AlertType.DeclarationOfFriendship, greek.civID))
            val friendship = rome.turn().alerts.single().friendship!!
            check(friendship.duration == com.unciv.logic.civilization.PlayerFriendshipOperations.DURATION)
            check(friendship.content.paragraphs.isNotEmpty()) { "Human-facing dialogue was removed" }
            roman.popupAlerts.clear()
            roman.popupAlerts.add(PopupAlert(AlertType.AttackedProtectedMinor, "${greek.civID}@${minor.civID}"))
            val incident = rome.turn().alerts.single().nativeDiplomacy!!
            check(incident.incidentCityState!!.civilizationId == minor.civID && !incident.incidentCityState!!.unmet)
            // The native message can reference an incident involving an unmet minor.
            roman.diplomacy.remove(minor.civID)
            val hidden = rome.turn().alerts.single().nativeDiplomacy!!.incidentCityState!!
            check(hidden.civilizationId == null && hidden.unmet && minor.civName !in hidden.name)
        }
    }

    private fun verifyMediatedPeace(root: Path) {
        HeadlessRuntime.createMixedDiplomacyExercise(root).use { runtime ->
            val roman = runtime.game.getCivilization("Rome")
            val china = runtime.game.getCivilization("China")
            val france = runtime.game.getCivilization("France")
            for (enemy in listOf(china, france)) {
                roman.getDiplomacyManager(enemy)!!.diplomaticStatus = com.unciv.logic.civilization.diplomacy.DiplomaticStatus.War
                enemy.getDiplomacyManager(roman)!!.diplomaticStatus = com.unciv.logic.civilization.diplomacy.DiplomaticStatus.War
            }
            china.units.getCivUnits().toList().forEach { it.destroy() }
            val rome = PlayerHost(runtime).bindPlayer("Rome")
            val greece = PlayerHost(runtime).bindPlayer("Greece")
            val options = rome.communication(obj("op" to "trade_options", "epoch" to runtime.epoch, "partner" to "Greece"))
            val peace = options["give"]!!.jsonArray.map { it.jsonObject }.filter { it["type"]!!.jsonPrimitive.content == "PeaceProposal" }
            check(peace.single { it["name"]!!.jsonPrimitive.content == "China" }["available"]!!.jsonPrimitive.boolean) { options.toString() }
            check(!peace.single { it["name"]!!.jsonPrimitive.content == "France" }["available"]!!.jsonPrimitive.boolean)
            fun offer(target: String) = rome.communication(
                obj(
                    "op" to "trade_offer",
                    "epoch" to runtime.epoch,
                    "requestId" to target,
                    "partner" to "Greece",
                    "give" to listOf(obj("type" to "PeaceProposal", "name" to target, "amount" to 1, "duration" to -1)),
                    "receive" to listOf(gold(1)),
                ),
            )
            check(offer("France")["error"] != null)
            val proposal = offer("China")["trade"]!!.jsonObject
            val result = greece.communication(obj("op" to "trade_decide", "epoch" to runtime.epoch, "requestId" to "peace-accept", "proposal" to proposal["proposal"], "version" to 1, "decision" to "accept"))
            check(result["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "ENACTED")
            check(!roman.isAtWarWith(china) && roman.isAtWarWith(france))
            check(roman.getDiplomacyManager(china)!!.turnsToPeaceTreaty() == runtime.game.speed.peaceDealDuration)
            Files.writeString(root.resolve("evidence.json"), result.toString())
        }
    }

    private fun verifyFailedMessage(root: Path, afterReplace: Boolean) {
        val checkpoint = root.resolve("checkpoint.json")
        lateinit var request: JsonObject
        var failCommit = false
        HeadlessRuntime.createMixedDiplomacyExercise(root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(
                CheckpointStore(
                    checkpoint,
                    beforeReplace = { if (failCommit && !afterReplace) error("Simulated write failure") },
                    afterReplace = { if (failCommit && afterReplace) error("Lost acknowledgement") },
                ),
            )
            val rome = PlayerHost(runtime).bindPlayer("Rome")
            val turn = rome.turn()
            request = obj(
                "op" to "send",
                "epoch" to turn.epoch,
                "requestId" to "durable-denounce",
                "to" to "Greece",
                "text" to "We denounce you.",
                "nativeAction" to obj("kind" to "DENOUNCE", "target" to "Greece", "revision" to turn.revision),
            )
            // Routing errors and stale observations have no message or native effect.
            val original = json().toJson(runtime.game)
            check(runCatching { rome.communication(JsonObject(request + obj("to" to "China"))) }.isFailure)
            check(json().toJson(runtime.game) == original)
            check(runtime.executor.communicationState.streams.values.flatten().none { it.kind == "message" })
            failCommit = true
            check(runCatching { rome.communication(request) }.isFailure)
            check(runtime.executor.mutationFailure != null)
            check(runCatching { rome.turn() }.isFailure)
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("resumed")).use { runtime ->
            val rome = PlayerHost(runtime).bindPlayer("Rome")
            val greece = runtime.game.getCivilization("Greece")
            check(runtime.game.getCivilization("Rome").getDiplomacyManager(greece)!!.hasFlag(DiplomacyFlags.Denunciation) == afterReplace)
            val fresh = rome.turn()
            val retry = if (afterReplace || request["nativeAction"]!!.jsonObject["revision"]!!.jsonPrimitive.content == fresh.revision) {
                request
            } else {
                check(runCatching { rome.communication(request) }.isFailure)
                JsonObject(request + obj("requestId" to "refreshed-denounce", "nativeAction" to JsonObject(request["nativeAction"]!!.jsonObject + obj("revision" to fresh.revision))))
            }
            val response = rome.communication(retry)
            check(response["nativeOutcome"] != null)
            check(rome.communication(retry) == response)
            check(greece.popupAlerts.count { it.type == AlertType.Denounced } == 1)
            for (civ in listOf("Rome", "Greece")) check(runtime.executor.communicationState.streams.getValue(civ).count { it.kind == "message" } == 1)
            check(runtime.executor.communicationState.streams["France"].orEmpty().isEmpty())
        }
    }

    private fun finishDiplomacy(api: GameplayTestApi, token: String, epoch: String) {
        repeat(30) {
            val turn = api.readAlerts(token)
            val alert = turn.alerts.firstOrNull { it.nativeDiplomacy != null } ?: return
            val choice = alert.nativeDiplomacy!!.options.firstOrNull { it.name == "Acknowledge" }
                ?: alert.nativeDiplomacy!!.options.first { it.name == "Accept" }
            val result = api.communicate(
                token,
                obj(
                    "op" to "send",
                    "epoch" to epoch,
                    "requestId" to UUID.randomUUID().toString(),
                    "text" to "Acknowledged for the exercise.",
                    "to" to alert.nativeDiplomacy!!.civilizationId,
                    "nativeAction" to obj("kind" to "RESPOND_DIPLOMACY", "target" to alert.handle, "selection" to choice.name, "revision" to turn.revision),
                ),
            )
            check(result["error"] == null) { result.toString() }
        }
        error("Native alert budget exhausted")
    }

    private fun exerciseOutcomes(runtime: HeadlessRuntime, api: GameplayTestApi, token: String) {
        val rome = runtime.game.getCivilization("Rome")
        val china = runtime.game.getCivilization("China")
        fun send(op: String, vararg fields: Pair<String, Any?>) = api.communicate(
            token,
            obj(
                "epoch" to runtime.epoch,
                "op" to op,
                *if (op in setOf("state", "trade_read", "trade_options")) emptyArray() else arrayOf("requestId" to UUID.randomUUID().toString()),
                *fields,
            ),
        )
        fun incoming() = send("state")["trades"]!!.jsonArray.map { it.jsonObject }.single { it["author"]!!.jsonPrimitive.content == "China" && it["status"]!!.jsonPrimitive.content == "PENDING" }
        fun read(proposal: JsonObject) = send("trade_read", "proposal" to proposal["proposal"])["trade"]!!.jsonObject
        fun decide(proposal: JsonObject, decision: String) = send("trade_decide", "proposal" to proposal["proposal"], "version" to proposal["version"], "decision" to decision)
        fun native() = runtime.executor.accessGame { TradeAutomation.respondToTradeRequests(china, true) }

        // Native AI counters a request for an embassy with its own exact price.
        runtime.executor.accessGame {
            fun research(name: String, player: com.unciv.logic.civilization.Civilization) {
                if (player.tech.isResearched(name)) return
                runtime.game.ruleset.technologies.getValue(name).prerequisites.forEach { research(it, player) }
                player.tech.addTechnology(name, false)
            }
            research("Writing", rome)
            research("Writing", china)
        }
        val embassy = send("trade_options", "partner" to "China")["receive"]!!.jsonArray.map { it.jsonObject }.first { it["type"]!!.jsonPrimitive.content == "Embassy" }
        val exactEmbassy = JsonObject(embassy.filterKeys { it in setOf("type", "name", "amount", "duration") })
        val requested = send("trade_offer", "partner" to "China", "give" to emptyList<JsonObject>(), "receive" to listOf(exactEmbassy))["trade"]!!.jsonObject
        native()
        val counter = read(requested)
        check(counter["version"]!!.jsonPrimitive.content == "2" && counter["author"]!!.jsonPrimitive.content == "China") { counter.toString() }
        check(counter["acceptAvailable"]!!.jsonPrimitive.boolean) { counter.toString() }
        check(decide(counter, "decline")["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "DECLINED")

        // An agent counter replaces exactly the imported version; stale consent cannot enact it.
        runtime.executor.accessGame { rome.tradeRequests.add(TradeRequest(china.civID, Trade().apply { theirOffers.add(TradeOffer("Gold", TradeOfferType.Gold, 3, runtime.game.speed)) })) }
        val original = read(incoming())
        val replacement = send("trade_offer", "partner" to "China", "proposal" to original["proposal"], "version" to original["version"], "give" to listOf(gold(2)), "receive" to emptyList<JsonObject>())["trade"]!!.jsonObject
        check(rome.tradeRequests.isEmpty())
        check(decide(original, "accept")["error"] != null)
        check(decide(replacement, "withdraw")["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "WITHDRAWN")
        check(china.tradeRequests.isEmpty())

        // Unavailable finances invalidate at native processing; absence is never called acceptance.
        val invalid = send("trade_offer", "partner" to "China", "give" to listOf(gold(20)), "receive" to emptyList<JsonObject>())["trade"]!!.jsonObject
        val savedGold = rome.gold
        runtime.executor.accessGame { rome.addGold(-rome.gold) }
        native()
        check(read(invalid)["status"]!!.jsonPrimitive.content == "INVALID")
        runtime.executor.accessGame { rome.addGold(savedGold) }

        // Unsupported or hidden imported terms are decline-only and never disclose the raw target.
        runtime.executor.accessGame { rome.tradeRequests.add(TradeRequest(china.civID, Trade().apply { theirOffers.add(TradeOffer("HIDDEN-TARGET", TradeOfferType.PeaceProposal, 1, runtime.game.speed)) })) }
        val unsafe = read(incoming())
        check(!unsafe["acceptAvailable"]!!.jsonPrimitive.boolean && "HIDDEN-TARGET" !in unsafe.toString())
        check("HIDDEN-TARGET" !in api.turn(token).toString())
        check(decide(unsafe, "decline")["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "DECLINED")

        // Human controllers cannot enter the agent-only bridge.
        val human = api.communicate(api.playerToken("France"), obj("op" to "trade_options", "epoch" to runtime.epoch, "partner" to "China"))
        check(!human["available"]!!.jsonPrimitive.boolean)
    }
}
