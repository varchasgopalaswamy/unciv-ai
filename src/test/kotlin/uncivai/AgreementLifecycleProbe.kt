package uncivai

import com.unciv.Constants
import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.trade.Trade
import com.unciv.logic.trade.TradeLogic
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path
import java.util.UUID

/** Actual scheduled turns and native trade changes, with player-facing reads and durable recovery. */
object AgreementLifecycleProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        durationsAndRestart(root.resolve("durations"))
        mixedTermsAndFriendship(root.resolve("mixed"))
        immediateCompletion(root.resolve("completed"))
        resourceLoss(root.resolve("resources"), peace = false)
        resourceLoss(root.resolve("peace-remnant"), peace = true)
        resourceLossRetainsRelationshipFlags(root.resolve("resource-flags"))
        warAndPrivateWorld(root.resolve("war"))
        for (afterReplace in listOf(false, true)) failedExpiration(root.resolve("failure-$afterReplace"), afterReplace)
        turnRecovery(root.resolve("turn"))
        println("Agreement lifecycle verified: $root")
    }

    private fun immediateCompletion(root: Path) {
        HeadlessRuntime.createTradeExercise(root.resolve("initial")).use { runtime ->
            nativeTrade(runtime.game, "Rome", "Greece", listOf(TradeOffer(Constants.flatGold, TradeOfferType.Gold, 5, -1), TradeOffer(Constants.acceptEmbassy, TradeOfferType.Embassy, 1, -1)), emptyList())
            runtime.executor.enableCheckpoints(root.resolve("game.json"))
            val trade = agreements(runtime, "Rome").single { it.text("kind") == "TRADE" }
            val embassy = agreements(runtime, "Rome").single { it.text("kind") == "EMBASSY" }
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                api.finishTurn(checkNotNull(api.activate().playerToken))
            }
            check(read(runtime, "Rome", id(trade)).text("status") == "ENDED")
            check(events(runtime, "Rome").single { it.text("agreement") == id(trade) }.text("reason") == "COMPLETED")
            check(read(runtime, "Rome", id(embassy)).text("status") == "ACTIVE")
            check(call(runtime, "Rome", "trade_options", "partner" to "Greece").getValue("relationship").jsonObject.getValue("theirEmbassyInYourCapital").jsonPrimitive.boolean)
        }
    }

    private fun mixedTermsAndFriendship(root: Path) {
        HeadlessRuntime.createTradeExercise(root.resolve("initial")).use { runtime ->
            nativeTrade(runtime.game, "Rome", "Greece", listOf(gpt(1, 1)), listOf(gpt(2, 4)))
            val rome = runtime.game.getCivilization("Rome")
            val greece = runtime.game.getCivilization("Greece")
            rome.getDiplomacyManager(greece)!!.signDeclarationOfFriendship()
            for ((a, b) in listOf(rome to greece, greece to rome)) a.getDiplomacyManager(b)!!.setFlag(DiplomacyFlags.DeclarationOfFriendship, 2)
            runtime.executor.enableCheckpoints(root.resolve("game.json"))
            val trade = agreements(runtime, "Rome").single { it.text("kind") == "TRADE" }
            val friendship = agreements(runtime, "Rome").single { it.text("kind") == "FRIENDSHIP" }
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                repeat(2) { api.finishTurn(checkNotNull(api.activate().playerToken)) }
                checkNativeRows(runtime, "Rome", "Greece")
                val partial = read(runtime, "Rome", id(trade))
                check(partial.text("status") == "ACTIVE")
                check(partial.getValue("give").jsonArray.single().jsonObject.getValue("duration").jsonPrimitive.int == 0)
                val event = events(runtime, "Rome").single { it.text("agreement") == id(trade) }
                check(event.text("change") == "TERMS_ENDED" && event.text("reason") == "EXPIRED")
                // Native GPT remains counted until the entire row disappears, even when one
                // clause's duration reaches zero. Report the engine's actual effect faithfully.
                check(event.getValue("effects").jsonObject.getValue("goldPerTurnWithPartner").jsonPrimitive.int == rome.getDiplomacyManager(greece)!!.goldPerTurn())
                repeat(2) { api.finishTurn(checkNotNull(api.activate().playerToken)) }
                check(read(runtime, "Rome", id(friendship)).text("status") == "ENDED")
                check(events(runtime, "Rome").single { it.text("agreement") == id(friendship) && it.text("change") == "ENDED" }.text("reason") == "EXPIRED")
            }
        }
    }

    private fun durationsAndRestart(root: Path) {
        val checkpoint = root.resolve("game.json")
        lateinit var original: List<String>
        lateinit var acceptedRequest: JsonObject
        lateinit var acceptedResponse: JsonObject
        lateinit var beforeRestart: Map<String, List<JsonObject>>
        HeadlessRuntime.createTradeExercise(root.resolve("initial")).use { runtime ->
            // Two indistinguishable rows must retain independent identities. Native mirrors tick
            // on their owner's turn and therefore need not show identical remaining durations.
            repeat(2) { nativeTrade(runtime.game, "Rome", "Greece", listOf(gpt(1, 3)), emptyList()) }
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls1")).use { api ->
                api.acquire()
                val token = checkNotNull(api.activate().playerToken)
                original = agreements(runtime, "Rome").map(::id)
                check(original.size == 2 && original.distinct().size == 2)
                checkNativeRows(runtime, "Rome", "Greece")
                checkNativeRows(runtime, "Greece", "Rome")
                val options = call(runtime, "Rome", "trade_options", "partner" to "Greece")
                val item = options.getValue("give").jsonArray.map { it.jsonObject }.single { it.text("type") == "Gold_Per_Turn" }
                val selection = JsonObject(item.filterKeys { it in setOf("type", "name", "duration") } + obj("amount" to 1))
                val proposal = call(runtime, "Rome", "trade_offer", "requestId" to "duration-offer", "partner" to "Greece", "give" to listOf(selection), "receive" to emptyList<String>()).getValue("trade").jsonObject
                acceptedRequest = obj("op" to "trade_decide", "epoch" to runtime.epoch, "requestId" to "duration-accept", "proposal" to proposal["proposal"], "version" to proposal["version"], "decision" to "accept")
                acceptedResponse = api.communicate(api.playerToken("Greece"), acceptedRequest)
                check(acceptedResponse["outcome"] is JsonObject)
                val records = agreements(runtime, "Rome")
                check(records.size == 3 && records.take(2).map(::id) == original)
                check(api.communicate(api.playerToken("Greece"), acceptedRequest) == acceptedResponse)
                check(agreements(runtime, "Rome") == records)
                api.finishTurn(token)
                checkNativeRows(runtime, "Rome", "Greece")
                checkNativeRows(runtime, "Greece", "Rome")
                beforeRestart = listOf("Rome", "Greece").associateWith { civ -> agreements(runtime, civ).map { read(runtime, civ, id(it)) } }
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            GameplayTestApi(runtime, root.resolve("tls2")).use { api ->
                for (civ in listOf("Rome", "Greece")) {
                    check(agreements(runtime, civ).map { read(runtime, civ, id(it)) } == beforeRestart.getValue(civ)) { "Reload changed $civ agreement references or history" }
                }
                val eventsBefore = runtime.executor.communicationState.streams.mapValues { it.value.toList() }
                check(api.communicate(api.playerToken("Greece"), acceptedRequest) == acceptedResponse)
                check(runtime.executor.communicationState.streams == eventsBefore) { "Exact acceptance retry duplicated lifecycle events" }
                api.acquire()
                repeat(7) {
                    val token = checkNotNull(api.activate().playerToken)
                    api.finishTurn(token)
                    checkNativeRows(runtime, "Rome", "Greece")
                    checkNativeRows(runtime, "Greece", "Rome")
                }
                for (civ in listOf("Rome", "Greece")) {
                    val records = agreements(runtime, civ)
                    check(records.count { it.text("status") == "ACTIVE" } == 1)
                    check(records.count { it.text("status") == "ENDED" } == 2)
                    val endings = events(runtime, civ).filter { it.text("change") == "ENDED" }
                    check(endings.size == 2 && endings.all { it.text("reason") == "EXPIRED" })
                    check(endings.map { it.text("agreement") }.distinct().size == 2)
                }
                check(original.all { read(runtime, "Rome", it).text("status") == "ENDED" })
            }
        }
    }

    private fun resourceLoss(root: Path, peace: Boolean) {
        HeadlessRuntime.createTradeExercise(root.resolve("initial")).use { runtime ->
            val rome = runtime.game.getCivilization("Rome")
            val greece = runtime.game.getCivilization("Greece")
            if (peace) rome.getDiplomacyManager(greece)!!.declareWar()
            val treaty = if (peace) listOf(TradeOffer(Constants.peaceTreaty, TradeOfferType.Treaty, 1, 6)) else emptyList()
            repeat(if (peace) 1 else 2) {
                nativeTrade(runtime.game, "Rome", "Greece", treaty + TradeOffer("Iron", TradeOfferType.Strategic_Resource, 1, 6), treaty + gpt(1, 6))
            }
            if (!peace) {
                rome.getDiplomacyManager(greece)!!.signDeclarationOfFriendship()
                for ((a, b) in listOf(rome to greece, greece to rome)) a.getDiplomacyManager(b)!!.setFlag(DiplomacyFlags.DeclarationOfFriendship, 1)
            }
            runtime.executor.enableCheckpoints(root.resolve("game.json"))
            val before = agreements(runtime, "Rome").filter { it.text("kind") == "TRADE" }.map(::id)
            val friendship = agreements(runtime, "Rome").singleOrNull { it.text("kind") == "FRIENDSHIP" }?.let(::id)
            check(before.size == if (peace) 1 else 2)
            val iron = rome.cities.single().getTiles().single { it.resource == "Iron" }
            // A prepared native resource change keeps this test independent of worker/combat
            // movement. The actual cut-short operation occurs in ordinary scheduled turn logic.
            if (peace) iron.improvementIsPillaged = true else iron.resourceAmount = 1
            rome.cache.updateCivResources()
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                repeat(2) { api.finishTurn(checkNotNull(api.activate().playerToken)) }
            }
            checkNativeRows(runtime, "Rome", "Greece")
            checkNativeRows(runtime, "Greece", "Rome")
            val ending = events(runtime, "Rome").single { it.text("agreement") == before.first() && it.text("change") == "ENDED" }
            check(ending.text("reason") == "RESOURCE_UNAVAILABLE")
            check(ending.toString().contains("Iron")) { "The known former terms should remain readable" }
            check(read(runtime, "Rome", before.first()).text("status") == "ENDED")
            if (peace) {
                val remaining = agreements(runtime, "Rome").single { it.text("status") == "ACTIVE" }
                check(id(remaining) !in before) { "Native replacement treaty reused the removed bundle's ID" }
                check(rome.getDiplomacyManager(greece)!!.turnsToPeaceTreaty() > 0)
                check(!rome.isAtWarWith(greece))
                val terms = read(runtime, "Rome", id(remaining))
                check((terms.getValue("give").jsonArray + terms.getValue("receive").jsonArray).all { it.jsonObject.text("name") == Constants.peaceTreaty })
            } else {
                // Native cut-short removes the first supplier row and all equal partner rows.
                // Preserve that actual asymmetry without reassigning the survivor's identity.
                check(read(runtime, "Rome", before.last()).text("status") == "ACTIVE")
                check(rome.getDiplomacyManager(greece)!!.trades.size == 1)
                check(greece.getDiplomacyManager(rome)!!.trades.isEmpty())
                check(events(runtime, "Rome").single { it.text("agreement") == friendship && it.text("change") == "ENDED" }.text("reason") == "EXPIRED") { "Unrelated resource-cut notice obscured the friendship's natural expiry" }
            }
        }
    }

    private fun resourceLossRetainsRelationshipFlags(root: Path) {
        HeadlessRuntime.createTradeExercise(root.resolve("initial")).use { runtime ->
            val research = TradeOffer(Constants.researchAgreement, TradeOfferType.Treaty, 5, 3)
            val defense = TradeOffer(Constants.defensivePact, TradeOfferType.Treaty, 1, 3)
            nativeTrade(runtime.game, "Rome", "Greece", listOf(research, defense, TradeOffer("Iron", TradeOfferType.Strategic_Resource, 1, 3)), listOf(research.copy(), defense.copy(), gpt(1, 3)))
            runtime.executor.enableCheckpoints(root.resolve("game.json"))
            val original = listOf("Rome", "Greece").associateWith { civ -> agreements(runtime, civ).associate { it.text("kind") to id(it) } }
            val rome = runtime.game.getCivilization("Rome")
            rome.cities.single().getTiles().single { it.resource == "Iron" }.improvementIsPillaged = true
            rome.cache.updateCivResources()
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                repeat(2) { api.finishTurn(checkNotNull(api.activate().playerToken)) }
                for (civ in listOf("Rome", "Greece")) {
                    val ids = original.getValue(civ)
                    check(read(runtime, civ, ids.getValue("TRADE")).text("status") == "ENDED")
                    check(events(runtime, civ).single { it.text("agreement") == ids.getValue("TRADE") }.text("reason") == "RESOURCE_UNAVAILABLE")
                    for (kind in listOf("RESEARCH_AGREEMENT", "DEFENSIVE_PACT")) {
                        check(read(runtime, civ, ids.getValue(kind)).text("status") == "ACTIVE") { "$kind must retain its actual native flag after a resource-cut removes the bundled row" }
                    }
                    check(runtime.game.getCivilization(civ).getDiplomacyManager(if (civ == "Rome") "Greece" else "Rome")!!.trades.isEmpty())
                }
                repeat(4) { api.finishTurn(checkNotNull(api.activate().playerToken)) }
                for (civ in listOf("Rome", "Greece")) {
                    for (kind in listOf("RESEARCH_AGREEMENT", "DEFENSIVE_PACT")) {
                        val id = original.getValue(civ).getValue(kind)
                        check(read(runtime, civ, id).text("status") == "ENDED")
                        check(events(runtime, civ).single { it.text("agreement") == id && it.text("change") == "ENDED" }.text("reason") == "EXPIRED")
                    }
                }
            }
        }
    }

    private fun warAndPrivateWorld(root: Path) {
        HeadlessRuntime.create(root.resolve("initial")) { ThirdPartyTradeScenarioSetup.create() }.use { runtime ->
            nativeTrade(runtime.game, "Rome", "Greece", listOf(gpt(2, 8)), emptyList())
            val defense = TradeOffer(Constants.defensivePact, TradeOfferType.Treaty, 1, 8)
            nativeTrade(runtime.game, "Rome", "France", listOf(defense, gpt(3, 8)), listOf(defense.copy()))
            runtime.executor.enableCheckpoints(root.resolve("game.json"))
            val romanBefore = agreements(runtime, "Rome").map { read(runtime, "Rome", id(it)) }
            val egyptBefore = call(runtime, "Egypt", "agreement_list")
            val egyptStream = runtime.executor.communicationState.streams["Egypt"].orEmpty().toList()
            val ids = romanBefore.filter { it.text("kind") == "TRADE" }.associate { it.text("partner") to id(it) }
            val guessed = runCatching { read(runtime, "Egypt", ids.getValue("Greece")) }.exceptionOrNull()
            val absent = runCatching { read(runtime, "Egypt", "a999999") }.exceptionOrNull()
            check(guessed is PlayerSessionException && absent is PlayerSessionException && guessed.reason == absent.reason)
            // Hidden changes must not consume another player's local reference sequence.
            val france = runtime.game.getCivilization("France")
            val egypt = runtime.game.getCivilization("Egypt")
            france.diplomacyFunctions.makeCivilizationsMeet(egypt)
            nativeTrade(runtime.game, "France", "Egypt", listOf(gpt(1, 8)), emptyList())
            runtime.executor.commitCheckpoint()
            check(agreements(runtime, "Rome").map { read(runtime, "Rome", id(it)) } == romanBefore)
            val franceBefore = agreements(runtime, "France").map { read(runtime, "France", id(it)) }
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val turn = session.turn()
            val request = TurnCommandRequest("agreement-war", turn.epoch, turn.revision, TurnCommandKind.DECLARE_WAR, "Greece", confirmWar = true).reviewedFor(turn)
            val receipt = session.execute(request)
            check(receipt.kind == TurnCommandKind.DECLARE_WAR)
            check(read(runtime, "Rome", ids.getValue("Greece")).text("status") == "ENDED")
            check(read(runtime, "Rome", ids.getValue("France")).text("status") == "ENDED")
            val endings = events(runtime, "Rome").filter { it.text("change") == "ENDED" }
            check(endings.single { it.text("agreement") == ids.getValue("Greece") }.text("reason") == "WAR")
            check(endings.single { it.text("agreement") == ids.getValue("France") }.text("reason") == "DEFENSIVE_PACT_CANCELLED")
            check(runtime.game.getCivilization("Rome").getDiplomacyManager("France")!!.trades.isEmpty())
            check(!runtime.game.getCivilization("Rome").isAtWarWith(france))
            check(agreements(runtime, "France").count { it.text("status") == "ACTIVE" } == 1)
            check(franceBefore.count { it.text("kind") == "TRADE" } == 2)
            val after = events(runtime, "Rome")
            check(session.execute(request) == receipt && events(runtime, "Rome") == after)
            check(egyptBefore.getValue("agreements").jsonArray.isEmpty())
            check(runtime.executor.communicationState.streams["Egypt"].orEmpty().filter { it.kind == "agreement" }.none { it.body.text("partner") in setOf("Rome", "Greece") })
            check(runtime.executor.communicationState.streams["Egypt"].orEmpty().take(egyptStream.size) == egyptStream)
            // A local friendship is a flag, not a native Trade row, and still needs its own ID.
            france.getDiplomacyManager(egypt)!!.signDeclarationOfFriendship()
            runtime.executor.commitCheckpoint()
            check(agreements(runtime, "France").any { it.text("kind") == "FRIENDSHIP" && it.text("partner") == "Egypt" && it.text("status") == "ACTIVE" })
        }
    }

    private fun failedExpiration(root: Path, afterReplace: Boolean) {
        val checkpoint = root.resolve("game.json")
        var fail = false
        lateinit var request: TurnCommandRequest
        lateinit var agreement: String
        HeadlessRuntime.createTradeExercise(root.resolve("initial")).use { runtime ->
            nativeTrade(runtime.game, "Rome", "Greece", listOf(gpt(1, 1)), emptyList())
            runtime.executor.enableCheckpoints(CheckpointStore(checkpoint, beforeReplace = { if (fail && !afterReplace) error("Before lifecycle checkpoint replacement") }, afterReplace = { if (fail && afterReplace) error("After lifecycle checkpoint replacement") }))
            agreement = id(agreements(runtime, "Rome").single())
            val rome = PlayerHost(runtime).bindPlayer("Rome")
            val turn = prepareEndTurn(runtime, "Rome")
            request = TurnCommandRequest("expiration-end", turn.epoch, turn.revision, TurnCommandKind.END_TURN).reviewedFor(turn)
            fail = true
            val failure = runCatching { rome.execute(request) }.exceptionOrNull()
            check(failure is PlayerSessionException && failure.reason == PlayerSessionFailure.GAME_HALTED) { "Expected player-facing checkpoint fail-stop, got ${failure?.stackTraceToString() ?: "successful END_TURN"}" }
            check(runtime.mutationFailure != null)
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            val record = read(runtime, "Rome", agreement)
            check(record.text("status") == if (afterReplace) "ENDED" else "ACTIVE")
            check(events(runtime, "Rome").count { it.text("change") == "ENDED" } == if (afterReplace) 1 else 0)
            val rome = PlayerHost(runtime).bindPlayer("Rome")
            val turn = rome.turn()
            val retried = if (afterReplace) request else request.copy(revision = turn.revision).reviewedFor(turn)
            val receipt = rome.execute(retried)
            check(receipt.kind == TurnCommandKind.END_TURN)
            check(rome.execute(retried) == receipt)
            check(read(runtime, "Rome", agreement).text("status") == "ENDED")
            check(events(runtime, "Rome").count { it.text("change") == "ENDED" } == 1)
        }
    }

    /** Clear ordinary fixture choices while the checkpoint writer is still healthy. */
    private fun prepareEndTurn(runtime: HeadlessRuntime, civ: String): PlayerTurnSnapshot {
        val player = PlayerHost(runtime).bindPlayer(civ)
        repeat(40) {
            val turn = player.turn()
            fun command(kind: TurnCommandKind, target: String? = null, selection: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection).reviewedFor(turn)
            val alert = turn.alerts.firstOrNull { it.canAcknowledge }
            val city = turn.cities.firstOrNull { !it.puppet && it.constructionQueue.isEmpty() }
            val next = when {
                alert != null -> command(TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle)
                turn.freeTechnologies > 0 -> command(TurnCommandKind.CHOOSE_FREE_TECHNOLOGY, selection = turn.researchOptions.first())
                turn.freeGreatPeople > 0 -> command(TurnCommandKind.CHOOSE_FREE_GREAT_PERSON, selection = turn.greatPersonOptions.first())
                turn.researchQueue.isEmpty() -> command(TurnCommandKind.SET_RESEARCH_QUEUE).copy(queue = listOf(turn.researchOptions.first()))
                city != null -> command(TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, city.constructionOptions.first())
                turn.pending.any { it.kind == "Policy" } -> command(TurnCommandKind.ADOPT_POLICY, selection = turn.policyOptions.first())
                turn.canEndTurn -> return turn
                else -> error("Unsupported lifecycle fixture decision: ${turn.pending}")
            }
            player.execute(next)
        }
        error("Lifecycle fixture choices did not settle")
    }

    private fun turnRecovery(root: Path) {
        val checkpoint = root.resolve("game.json")
        lateinit var before: GameCheckpoint
        lateinit var original: List<JsonObject>
        lateinit var issuedAfter: String
        HeadlessRuntime.createTradeExercise(root.resolve("initial")).use { runtime ->
            nativeTrade(runtime.game, "Rome", "Greece", listOf(gpt(1, 2)), emptyList())
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                api.activate()
                api.operator(OperatorRequest("barrier", api.lease))
                original = agreements(runtime, "Rome").map { read(runtime, "Rome", id(it)) }
                before = checkNotNull(api.operator(OperatorRequest("snapshot", api.lease)).checkpoint)
                api.operator(OperatorRequest("unbarrier", api.lease))
                nativeTrade(runtime.game, "Rome", "Greece", listOf(gpt(2, 5)), emptyList())
                runtime.executor.commitCheckpoint()
                issuedAfter = agreements(runtime, "Rome").map(::id).single { it != id(original.single()) }
                api.operator(OperatorRequest("barrier", api.lease))
                runtime.executor.restoreTurn(before, "agreement-restored")
                check(agreements(runtime, "Rome").map { read(runtime, "Rome", id(it)) } == original)
                check(events(runtime, "Rome").none { it.text("change") == "ENDED" })
                check(runCatching { read(runtime, "Rome", issuedAfter) }.exceptionOrNull() is PlayerSessionException)
                val obsolete = obj("op" to "agreement_read", "epoch" to before.epoch, "agreement" to id(original.single()))
                val failure = runCatching { PlayerHost(runtime).bindPlayer("Rome").communication(obsolete) }.exceptionOrNull()
                check(failure is PlayerSessionException && failure.reason == PlayerSessionFailure.STALE_EPOCH)
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored-restart")).use { runtime ->
            check(runtime.epoch == "agreement-restored")
            check(agreements(runtime, "Rome").map { read(runtime, "Rome", id(it)) } == original)
            check(runCatching { read(runtime, "Rome", issuedAfter) }.exceptionOrNull() is PlayerSessionException)
        }
    }

    private fun checkNativeRows(runtime: HeadlessRuntime, viewer: String, partner: String) {
        val actual = agreements(runtime, viewer).filter { it.text("kind") == "TRADE" && it.text("partner") == partner && it.text("status") == "ACTIVE" }
        val native = runtime.game.getCivilization(viewer).getDiplomacyManager(partner)!!.trades
        check(actual.size == native.size) { "$viewer has ${actual.size} active rows for ${native.size} native trades" }
        actual.zip(native).forEach { (summary, trade) ->
            val full = read(runtime, viewer, id(summary))
            check(summary["give"] == full["give"] && summary["receive"] == full["receive"])
            check("representation" !in summary && "cancellation" !in summary)
            fun nativeFields(key: String) = JsonArray(full.getValue(key).jsonArray.map { item -> JsonObject(item.jsonObject.filterKeys { it in setOf("type", "name", "amount", "duration") }) })
            check(nativeFields("give") == offers(trade.ourOffers)) { "Outgoing clauses differ from native: $full" }
            check(nativeFields("receive") == offers(trade.theirOffers)) { "Incoming clauses differ from native: $full" }
            check(full.getValue("remainingTurns").jsonPrimitive.int == (trade.ourOffers + trade.theirOffers).maxOfOrNull { it.duration }!!.coerceAtLeast(0))
            check(!full.getValue("cancellation").jsonObject.getValue("available").jsonPrimitive.boolean)
        }
    }

    private fun offers(items: List<TradeOffer>) = JsonArray(items.map { obj("type" to it.type.name, "name" to it.name, "amount" to it.amount, "duration" to it.duration) })
    private fun gpt(amount: Int, duration: Int) = TradeOffer(Constants.goldPerTurn, TradeOfferType.Gold_Per_Turn, amount, duration)
    private fun nativeTrade(game: GameInfo, a: String, b: String, give: List<TradeOffer>, receive: List<TradeOffer>) {
        TradeLogic(game.getCivilization(a), game.getCivilization(b)).apply {
            currentTrade.set(
                Trade().apply {
                    ourOffers.addAll(give.map { it.copy() })
                    theirOffers.addAll(receive.map { it.copy() })
                },
            )
            acceptTrade()
        }
    }

    private fun call(runtime: HeadlessRuntime, civ: String, op: String, vararg fields: Pair<String, Any?>) = PlayerHost(runtime).bindPlayer(civ).communication(obj("op" to op, "epoch" to runtime.epoch, *fields))
    private fun agreements(runtime: HeadlessRuntime, civ: String) = call(runtime, civ, "agreement_list").getValue("agreements").jsonArray.map { it.jsonObject }
    private fun read(runtime: HeadlessRuntime, civ: String, agreement: String) = call(runtime, civ, "agreement_read", "agreement" to agreement).getValue("agreement").jsonObject
    private fun id(record: JsonObject) = record.text("agreement")
    private fun events(runtime: HeadlessRuntime, civ: String) = runtime.executor.communicationState.streams[civ].orEmpty().filter { it.kind == "agreement" }.map { it.body }
    private fun JsonObject.text(key: String) = getValue(key).jsonPrimitive.content
}
