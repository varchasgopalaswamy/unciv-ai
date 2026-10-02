package uncivai

import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.PopupAlert
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.civilization.diplomacy.DiplomaticModifiers
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

/** Exact native effects, including off-turn consent, city authority and durable retries. */
object BilateralTradeProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val checkpoint = root.resolve("game.json")
        val interrupted = root.resolve("interrupted.json")
        lateinit var friendship: JsonObject
        lateinit var cityOffer: JsonObject
        lateinit var accepted: JsonObject
        lateinit var request: JsonObject
        lateinit var embassyRequest: JsonObject
        lateinit var embassyReceipt: JsonObject
        lateinit var cityId: String
        lateinit var restorePoint: GameCheckpoint
        var expectedRomeGold = 0
        var expectedGreekGold = 0
        HeadlessRuntime.createBilateralExercise(root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls1")).use { api ->
                api.acquire()
                api.activate()
                api.operator(OperatorRequest("barrier", api.lease))
                restorePoint = checkNotNull(api.operator(OperatorRequest("snapshot", api.lease)).checkpoint)
                api.operator(OperatorRequest("unbarrier", api.lease))
                api.activate()
                val calls = Calls(runtime, api)
                val options = calls.options()
                check(options["friendship"]!!.jsonObject["available"]!!.jsonPrimitive.boolean)
                check(options["unavailableGive"]!!.toString().contains("embassies"))
                check(!options.toString().contains(runtime.game.getCivilization("Rome").cities.first().id))
                cityOffer = calls.item(options, "give", "City")
                cityId = runtime.game.getCivilization("Rome").cities.single { it.name == "Antium" }.id
                check(cityOffer["name"]!!.jsonPrimitive.content.startsWith("tc"))
                check(options["give"]!!.jsonArray.map { it.jsonObject }.single { it["type"]!!.jsonPrimitive.content == "City" }["city"]!!.jsonObject.keys == setOf("name", "population"))
                check(calls.command("Rome", "trade_options", "partner" to "Egypt")["error"] != null)
                friendship = calls.command("Rome", "friendship_offer", "partner" to "Greece")["trade"]!!.jsonObject
                check(friendship["friendship"]!!.jsonObject["duration"]!!.jsonPrimitive.int == 30)
                check(!runtime.game.getCivilization("Rome").getDiplomacyManager("Greece")!!.hasFlag(DiplomacyFlags.DeclarationOfFriendship))
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restarted")).use { runtime ->
            GameplayTestApi(runtime, root.resolve("tls2")).use { api ->
                api.acquire()
                val active = checkNotNull(api.activate().playerToken)
                val calls = Calls(runtime, api)
                val rome = runtime.game.getCivilization("Rome")
                val greece = runtime.game.getCivilization("Greece")
                check(calls.item(calls.options(), "give", "City") == cityOffer)
                val beforeFriendship = api.turn(active)
                val handoff = runtime.executor.handoff
                val consent = calls.decide(friendship)
                check(consent["outcome"]!!.jsonObject["action"]!!.jsonPrimitive.content == "DECLARE_FRIENDSHIP")
                check(runtime.game.currentPlayer == "Rome" && runtime.executor.handoff == handoff)
                check(rome.getDiplomacyManager(greece)!!.getFlag(DiplomacyFlags.DeclarationOfFriendship) == 30)
                check(greece.getDiplomacyManager(rome)!!.getFlag(DiplomacyFlags.DeclarationOfFriendship) == 30)
                check(calls.decide(friendship)["outcome"] == consent["outcome"])
                api.reject(active, TurnCommandRequest("stale-friendship", beforeFriendship.epoch, beforeFriendship.revision, TurnCommandKind.END_TURN).reviewedFor(beforeFriendship), "STALE_REVISION")
                check(calls.command("Rome", "friendship_offer", "partner" to "Greece")["error"] != null)

                val beforeEmbassy = calls.options()
                val embassy = calls.item(beforeEmbassy, "give", "Embassy")
                val embassyProposal = calls.offer(listOf(embassy), emptyList())
                embassyRequest = obj("op" to "trade_decide", "epoch" to runtime.epoch, "requestId" to "embassy-accept", "proposal" to embassyProposal["proposal"], "version" to 1, "decision" to "accept")
                embassyReceipt = api.communicate(api.playerToken("Greece"), embassyRequest)
                val embassyOutcome = embassyReceipt.getValue("outcome").jsonObject
                checkTradeConfirmation(embassyOutcome, romeMayEnter = false, greeceMayEnter = false, romeHasEmbassy = false, greeceHasEmbassy = true)
                calls.assertSharedOutcome(embassyOutcome)
                check(greece.getDiplomacyManager(rome)!!.hasModifier(DiplomaticModifiers.EstablishedEmbassy))
                check(!rome.getDiplomacyManager(greece)!!.hasModifier(DiplomaticModifiers.EstablishedEmbassy))
                check(rome.getCapital()!!.getCenterTile().isExplored(greece))
                check(calls.command("Rome", "trade_offer", "partner" to "Greece", "give" to listOf(embassy), "receive" to emptyList<String>())["error"] != null)
                val reciprocal = calls.item(calls.options(), "receive", "Embassy")
                calls.decide(calls.offer(emptyList(), listOf(reciprocal)))
                check(rome.getDiplomacyManager(greece)!!.hasModifier(DiplomaticModifiers.SharedEmbassies))
                // Historical confirmations retain the original direction after reciprocal access changes.
                check(api.communicate(api.playerToken("Greece"), embassyRequest) == embassyReceipt)
                calls.assertSharedOutcome(embassyOutcome)

                val options = calls.options()
                val borders = calls.item(options, "give", "Agreement")
                val research = calls.item(options, "give", "Treaty", "Research Agreement")
                val defense = calls.item(options, "give", "Treaty", "Defensive Pact")
                val gold = calls.item(options, "receive", "Gold").let { JsonObject(it + obj("amount" to 100)) }
                val cost = research["amount"]!!.jsonPrimitive.int
                val beforeMoney = rome.gold to greece.gold
                fun rejected(give: List<JsonObject>, receive: List<JsonObject>) {
                    check(calls.command("Rome", "trade_offer", "partner" to "Greece", "give" to give, "receive" to receive)["error"] != null)
                    check(rome.gold == beforeMoney.first && greece.gold == beforeMoney.second)
                }
                rejected(listOf(research), emptyList())
                val cheaper = JsonObject(research + obj("amount" to cost - 1))
                rejected(listOf(cheaper), listOf(cheaper))
                rejected(listOf(defense), listOf(JsonObject(defense + obj("duration" to 999))))
                rejected(listOf(cityOffer, cityOffer), emptyList())
                rejected(listOf(JsonObject(cityOffer + obj("name" to rome.getCapital()!!.id))), emptyList())
                rejected(listOf(obj("type" to "WarDeclaration", "name" to "Egypt", "amount" to 1, "duration" to -1)), emptyList())

                val staleCity = calls.offer(listOf(cityOffer), emptyList())
                val city = rome.cities.single { it.id == cityId }
                // A conquered city still has its old owner until the captor chooses a
                // disposition. Off-turn diplomacy must not sell it from under that popup.
                val captured = PopupAlert(AlertType.CityConquered, city.id)
                city.hasJustBeenConquered = true
                greece.popupAlerts.add(captured)
                val staleCityResult = calls.decide(staleCity)
                check(staleCityResult["error"] != null && !staleCityResult.toString().contains(cityId))
                check(city.civ === rome && greece.popupAlerts.any { it === captured })
                check(calls.options()["give"]!!.jsonArray.none { it.jsonObject["type"]!!.jsonPrimitive.content == "City" })
                city.hasJustBeenConquered = false
                greece.popupAlerts.remove(captured)
                val transferredCity = calls.offer(listOf(cityOffer), emptyList())
                city.moveToCiv(greece) // A competing engine action before consent.
                check(calls.decide(transferredCity)["error"] != null)
                city.moveToCiv(rome)
                check(calls.decide(transferredCity)["error"] != null)
                check(city.civ === rome)
                val view = api.turn(active)
                val ownedHandle = view.cities.single { it.name == "Antium" }.handle
                val proposal = calls.offer(listOf(borders, research, defense, cityOffer), listOf(borders, research, defense, gold))
                check(calls.read("Rome", proposal)["clauses"] == calls.read("Greece", proposal)["clauses"])
                expectedRomeGold = rome.gold + 100 - cost
                expectedGreekGold = greece.gold - 100 - cost
                request = obj("op" to "trade_decide", "epoch" to runtime.executor.epoch, "requestId" to "final-accept", "proposal" to proposal["proposal"], "version" to 1, "decision" to "accept")
                accepted = api.communicate(api.playerToken("Greece"), request)
                check(accepted["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "ENACTED")
                check(!accepted.toString().contains(cityId))
                check(rome.gold == expectedRomeGold && greece.gold == expectedGreekGold)
                check(city.civ === greece && city.id == cityId)
                check(city.getCenterTile() !in rome.viewableTiles && city.getCenterTile() in greece.viewableTiles)
                check(rome.cities.size == 1 && greece.cities.size == 3)
                check(rome.getDiplomacyManager(greece)!!.hasOpenBorders && greece.getDiplomacyManager(rome)!!.hasOpenBorders)
                check(rome.getDiplomacyManager(greece)!!.getFlag(DiplomacyFlags.ResearchAgreement) == research["duration"]!!.jsonPrimitive.int)
                check(greece.getDiplomacyManager(rome)!!.getFlag(DiplomacyFlags.DefensivePact) == defense["duration"]!!.jsonPrimitive.int)
                check(rome.getDiplomacyManager(greece)!!.diplomaticStatus.name == "DefensivePact")
                check(BilateralScenarioSetup.phase(runtime.game) == "ENACTED")
                api.reject(active, TurnCommandRequest("stale-city", view.epoch, view.revision, TurnCommandKind.RENAME_CITY, ownedHandle, "Stolen"), "STALE_REVISION")
                val refreshed = api.turn(active)
                api.reject(active, TurnCommandRequest("no-longer-owned", refreshed.epoch, refreshed.revision, TurnCommandKind.RENAME_CITY, ownedHandle, "Stolen"), "CITY_UNAVAILABLE")
                check(api.turn(api.playerToken("Greece")).cities.any { it.name == "Antium" })
                check(calls.options()["activeAgreements"]!!.toString().contains(cityOffer["name"]!!.jsonPrimitive.content))
                check(api.communicate(api.playerToken("Greece"), request) == accepted)
                Files.copy(checkpoint, interrupted)
                api.finishTurn(active)
                api.finishTurn(checkNotNull(api.activate().playerToken))
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("final-restart")).use { runtime ->
            val rome = runtime.game.getCivilization("Rome")
            val greece = runtime.game.getCivilization("Greece")
            check(greece.cities.any { it.id == cityId } && rome.cities.none { it.id == cityId })
            val before = rome.gold to greece.gold
            check(PlayerHost(runtime).bindPlayer("Greece").communication(request) == accepted)
            check(rome.gold == before.first && greece.gold == before.second)
            check(BilateralScenarioSetup.phase(runtime.game) == "ENACTED")
            GameplayTestApi(runtime, root.resolve("tls3")).use { api ->
                check(api.communicate(api.playerToken("Greece"), embassyRequest) == embassyReceipt)
                Calls(runtime, api).assertSharedOutcome(embassyReceipt.getValue("outcome").jsonObject)
            }
            runtime.executor.pause()
            runtime.executor.communicationPaused = true
            val failure = runCatching { runtime.executor.restoreTurn(restorePoint, "completed-turn") }.exceptionOrNull()
            check(failure is OperatorFailure && failure.code == "TURN_ALREADY_COMMITTED")
            check(greece.cities.any { it.id == cityId })
        }
        HeadlessRuntime.loadCheckpoint(interrupted, root.resolve("interrupted-restart")).use { runtime ->
            runtime.executor.pause()
            runtime.executor.communicationPaused = true
            runtime.executor.restoreTurn(restorePoint, "bilateral-restored")
            runtime.executor.accessGame { game ->
                check(game.getCivilization("Rome").cities.any { it.id == cityId })
                check(game.getCivilization("Greece").cities.none { it.id == cityId })
                check(game.getCivilization("Rome").gold == 1000 && game.getCivilization("Greece").gold == 1000)
                check(game.getCivilization("Rome").getDiplomacyManager("Greece")!!.trades.isEmpty())
                check(BilateralScenarioSetup.phase(game) == "FRIENDSHIP")
            }
            check(runtime.executor.communicationState.trades.proposals.isEmpty())
            check(runCatching { PlayerHost(runtime).bindPlayer("Greece").communication(request) }.isFailure)
        }
        funding(root.resolve("funding"))
        friendshipPopup(root.resolve("friendship-popup"))
    }

    private fun funding(root: Path) {
        HeadlessRuntime.createBilateralExercise(root).use { runtime ->
            runtime.executor.enableCheckpoints(root.resolve("funding.json"))
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                api.activate()
                val calls = Calls(runtime, api)
                calls.decide(calls.command("Rome", "friendship_offer", "partner" to "Greece")["trade"]!!.jsonObject)
                val embassy = calls.item(calls.options(), "give", "Embassy")
                calls.decide(calls.offer(listOf(embassy), listOf(embassy)))
                val rome = runtime.game.getCivilization("Rome")
                val greece = runtime.game.getCivilization("Greece")
                val borders = calls.item(calls.options(), "give", "Agreement")
                val borderOutcome = calls.decide(calls.offer(listOf(borders), emptyList())).getValue("outcome").jsonObject
                checkTradeConfirmation(borderOutcome, romeMayEnter = false, greeceMayEnter = true, romeHasEmbassy = true, greeceHasEmbassy = true)
                calls.assertSharedOutcome(borderOutcome)
                check(greece.getDiplomacyManager(rome)!!.hasOpenBorders)
                check(!rome.getDiplomacyManager(greece)!!.hasOpenBorders)
                val greekView = calls.command("Greece", "trade_options", "partner" to "Rome").getValue("relationship").jsonObject
                check(greekView.getValue("youMayEnterTheirTerritory").jsonPrimitive.boolean)
                check(!greekView.getValue("theyMayEnterYourTerritory").jsonPrimitive.boolean)
                greece.addGold(0 - greece.gold)
                val options = calls.options()
                val research = calls.item(options, "give", "Treaty", "Research Agreement")
                val cost = research["amount"]!!.jsonPrimitive.int
                check(calls.command("Rome", "trade_offer", "partner" to "Greece", "give" to listOf(research), "receive" to listOf(research))["error"] != null)
                val gold = calls.item(options, "give", "Gold").let { JsonObject(it + obj("amount" to cost)) }
                val funded = calls.offer(listOf(gold, research), listOf(research))
                rome.addGold(cost - rome.gold)
                check(calls.decide(funded)["error"] != null) // Cannot fund both signatures any more.
                check(rome.gold == cost && greece.gold == 0)
                rome.addGold(cost)
                val valid = calls.offer(listOf(gold, research), listOf(research))
                // The latest proposer and accepter can swap while borders remain directional.
                val counter = calls.command("Greece", "trade_offer", "partner" to "Rome", "proposal" to valid["proposal"], "version" to 1, "give" to listOf(research), "receive" to listOf(gold, research)).getValue("trade").jsonObject
                val fundedOutcome = calls.command("Rome", "trade_decide", "proposal" to counter["proposal"], "version" to 2, "decision" to "accept").getValue("outcome").jsonObject
                checkTradeConfirmation(fundedOutcome, romeMayEnter = false, greeceMayEnter = true, romeHasEmbassy = true, greeceHasEmbassy = true)
                calls.assertSharedOutcome(fundedOutcome)
                check(rome.gold == 0 && greece.gold == 0)
                check(rome.getDiplomacyManager(greece)!!.hasFlag(DiplomacyFlags.ResearchAgreement))
            }
        }
    }

    private fun friendshipPopup(root: Path) {
        HeadlessRuntime.createTradeExercise(root).use { runtime ->
            val rome = runtime.game.getCivilization("Rome")
            rome.popupAlerts.add(PopupAlert(AlertType.DeclarationOfFriendship, "Greece"))
            val player = PlayerHost(runtime).bindPlayer("Rome")
            val before = player.turn()
            val alert = before.alerts.single { it.friendship != null }
            check(alert.friendship!!.content.paragraphs.last().isNotEmpty())
            check(!alert.canAcknowledge)
            val receipt = player.execute(TurnCommandRequest("friendship-popup", before.epoch, before.revision, TurnCommandKind.RESPOND_FRIENDSHIP, alert.handle, "Accept"))
            check(receipt.kind == TurnCommandKind.RESPOND_FRIENDSHIP)
            check(rome.getDiplomacyManager("Greece")!!.hasFlag(DiplomacyFlags.DeclarationOfFriendship))
            val outcome = runtime.executor.communicationState.outcomes.single()
            check(outcome.action == "DECLARE_FRIENDSHIP")
            checkTradeConfirmation(outcome.recordJson(), romeMayEnter = false, greeceMayEnter = false, romeHasEmbassy = false, greeceHasEmbassy = false)
            check(outcome.confirmation.relationships.values.all { it.friendshipTurns == 30 })
        }
    }

    private class Calls(val runtime: HeadlessRuntime, val api: GameplayTestApi) {
        private var serial = 0
        fun command(civ: String, op: String, vararg fields: Pair<String, Any?>): JsonObject = api.communicate(api.playerToken(civ), obj("op" to op, "epoch" to runtime.executor.epoch, *(if (op in setOf("trade_read", "trade_options", "state", "read")) emptyArray() else arrayOf("requestId" to "bilateral-${++serial}")), *fields))
        fun options() = command("Rome", "trade_options", "partner" to "Greece")
        fun item(options: JsonObject, side: String, type: String, name: String? = null): JsonObject {
            val choice = options[side]!!.jsonArray.map { it.jsonObject }.single { it["type"]!!.jsonPrimitive.content == type && (name == null || it["name"]!!.jsonPrimitive.content == name) }
            return JsonObject(choice.filterKeys { it in setOf("type", "name", "amount", "duration") })
        }
        fun offer(give: List<JsonObject>, receive: List<JsonObject>): JsonObject {
            val result = command("Rome", "trade_offer", "partner" to "Greece", "give" to give, "receive" to receive)
            check(result["trade"] != null) { result.toString() }
            return result["trade"]!!.jsonObject
        }
        fun decide(proposal: JsonObject) = command("Greece", "trade_decide", "proposal" to proposal["proposal"], "version" to proposal["version"], "decision" to "accept")
        fun read(civ: String, proposal: JsonObject) = command(civ, "trade_read", "proposal" to proposal["proposal"])["trade"]!!.jsonObject
        fun assertSharedOutcome(outcome: JsonObject) {
            for (civ in listOf("Rome", "Greece")) {
                check(read(civ, outcome).getValue("outcome") == outcome)
                val state = command(civ, "state")
                check(state.getValue("outcomes").jsonArray.single { it.jsonObject["proposal"] == outcome["proposal"] } == outcome)
                val page = command(civ, "read", "limit" to 100)
                check(!page.getValue("hasMore").jsonPrimitive.boolean)
                val events = page.getValue("events").jsonArray
                check(events.single { it.jsonObject["kind"]!!.jsonPrimitive.content == "engine" && it.jsonObject.getValue("body").jsonObject["proposal"] == outcome["proposal"] }.jsonObject.getValue("body") == outcome)
            }
        }
    }
}
