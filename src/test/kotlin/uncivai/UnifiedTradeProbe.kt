package uncivai

import com.unciv.Constants
import com.unciv.json.json
import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.trade.TradeOfferType
import com.unciv.logic.trade.TradeOperations
import com.unciv.view.GameView
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import uncivai.player.PlayerHost
import java.nio.file.Path
import java.util.UUID

/** Same full transactions for every human/agent pairing, using actual native draft conversion. */
object UnifiedTradeProbe {
    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args.single())
        val effects = mutableMapOf<String, String>()
        for ((index, humans) in listOf(emptySet(), setOf("Greece"), setOf("Rome"), setOf("Rome", "Greece")).withIndex()) {
            HeadlessRuntime.createBilateralExercise(root.resolve("pair-$index")).use { runtime ->
                runtime.executor.communicationState.humanPlayers.addAll(humans)
                runtime.executor.enableCheckpoints(root.resolve("checkpoint-$index-${runtime.game.tileMap.mapParameters.name.hashCode()}.json"))
                GameplayTestApi(runtime, root.resolve("tls-$index")).use { api ->
                    api.acquire()
                    api.activate()
                    val calls = Calls(runtime, humans.isNotEmpty(), effects)
                    fun item(type: String, name: String? = null, amount: Int? = null, side: String = "give"): JsonObject {
                        val chosen = calls.read("Rome", "trade_options", "partner" to "Greece").array(side).filterIsInstance<JsonObject>().single {
                            it.text("type") == type && (name == null || it.text("name") == name)
                        }
                        return JsonObject(
                            chosen.filterKeys { it in setOf("type", "name", "amount", "duration") } +
                                if (amount == null) emptyMap() else obj("amount" to amount),
                        )
                    }
                    calls.deal("cash-resources", listOf(item("Gold", amount = 6), item("Luxury_Resource", "Silk", 1), item("Strategic_Resource", "Iron", 2)), listOf(item("Gold_Per_Turn", amount = 2, side = "receive")))
                    calls.deal("embassies-cities", listOf(item("Embassy"), item("City")), listOf(item("Embassy", side = "receive"), item("City", side = "receive")))
                    val friendship = calls.write("Rome", "friendship_offer", "partner" to "Greece").getValue("trade").jsonObject
                    calls.write("Greece", "trade_decide", "proposal" to friendship["proposal"], "version" to friendship["version"], "decision" to "accept")
                    calls.deal("open-borders", listOf(item("Agreement")), listOf(item("Agreement", side = "receive")))
                    val greek = runtime.game.getCivilization("Greece")
                    greek.addGold(-greek.gold)
                    calls.deal("funded-research-defense", listOf(item("Treaty", Constants.researchAgreement), item("Treaty", Constants.defensivePact)), emptyList())
                    val rome = runtime.game.getCivilization("Rome")
                    rome.getDiplomacyManager(greek)!!.declareWar()
                    for (civ in listOf(rome, greek)) civ.getDiplomacyManager(if (civ === rome) greek else rome)!!.removeFlag(DiplomacyFlags.DeclaredWar)
                    calls.deal("peace", listOf(item("Treaty", Constants.peaceTreaty), item("Gold", amount = 4)), emptyList())
                }
            }
            HeadlessRuntime.create(root.resolve("third-$index")) { ThirdPartyTradeScenarioSetup.create() }.use { runtime ->
                runtime.executor.communicationState.humanPlayers.addAll(humans)
                runtime.executor.enableCheckpoints(root.resolve("checkpoint-$index-${runtime.game.tileMap.mapParameters.name.hashCode()}.json"))
                val france = runtime.game.getCivilization("France")
                france.playerType = PlayerType.AI // Only native AI targets can consent automatically to a third-party peace proposal.
                GameplayTestApi(runtime, root.resolve("third-tls-$index"), listOf("Rome", "Greece").associateWith { it.first().toString().repeat(43) }).use { api ->
                    api.acquire()
                    api.activate()
                    val calls = Calls(runtime, humans.isNotEmpty(), effects)
                    calls.deal("third-party-war", emptyList(), listOf(obj("type" to "WarDeclaration", "name" to "France", "amount" to 1, "duration" to -1)))
                    val greece = runtime.game.getCivilization("Greece")
                    greece.getDiplomacyManager(france)!!.removeFlag(DiplomacyFlags.DeclaredWar)
                    france.getDiplomacyManager(greece)!!.removeFlag(DiplomacyFlags.DeclaredWar)
                    val option = calls.read("Rome", "trade_options", "partner" to "Greece").array("receive").filterIsInstance<JsonObject>().single { it.text("type") == "PeaceProposal" }
                    check(option.number("amount") == 1) { option }
                    calls.deal("third-party-peace", emptyList(), listOf(JsonObject(option.filterKeys { it in setOf("type", "name", "amount", "duration") })))
                    check(!greece.isAtWarWith(france))
                }
            }
        }
        println("UNIFIED-TRADE-COMPLETE: four controller combinations; all Vanilla items prepared, countered and accepted")
    }

    private class Calls(val runtime: HeadlessRuntime, val human: Boolean, val effects: MutableMap<String, String>) {
        val players = PlayerHost(runtime)
        fun read(civ: String, op: String, vararg fields: Pair<String, Any?>) = players.bindPlayer(civ).communication(obj("op" to op, "epoch" to runtime.epoch, *fields))
        fun write(civ: String, op: String, vararg fields: Pair<String, Any?>): JsonObject {
            val result = read(civ, op, "requestId" to UUID.randomUUID().toString(), *fields)
            check(result["error"] == null) { result }
            return result
        }
        fun deal(name: String, give: List<JsonObject>, receive: List<JsonObject>) {
            val before = json().toJson(runtime.game)
            val prepared = read("Rome", "trade_prepare", "partner" to "Greece", "give" to give, "receive" to receive)
            check(prepared.flag("valid")) { "$name: $prepared" }
            val items = DesktopTradeItems(runtime.executor)
            val nativeDraft = items.nativeProposal(prepared, "Rome")
            val view = GameView(runtime.game, runtime.game.getCivilization("Rome"))
            val nativeTable = view.civView.getTradeView(view.getForeignCivView(runtime.game.getCivilization("Greece")))
            nativeTable.setStagedTrade(nativeDraft)
            nativeTable.fundStagedResearchAgreement()
            check(nativeTable.ourStagedOffers().map(items::player) == prepared.array("give"))
            check(nativeTable.theirStagedOffers().map(items::player) == prepared.array("receive"))
            check(json().toJson(runtime.game) == before) { "Draft mutated the game" }
            check(!prepared.toString().contains(runtime.game.getCivilization("Rome").cities.first().id))
            val first = write("Rome", "trade_offer", "partner" to "Greece", "give" to prepared["give"], "receive" to prepared["receive"]).getValue("trade").jsonObject
            check(first["clauses"] == prepared["clauses"])
            check((first["deadlineMillis"] == JsonNull) == human)
            val counter = write(
                "Greece",
                "trade_offer",
                "partner" to "Rome",
                "proposal" to first["proposal"],
                "version" to 1,
                "give" to prepared["receive"],
                "receive" to prepared["give"],
            ).getValue("trade").jsonObject
            check(counter.array("consents").size == 1)
            check(runCatching { write("Greece", "trade_decide", "proposal" to first["proposal"], "version" to 1, "decision" to "accept") }.isFailure)
            val expected = runtime.game.clone().apply { setTransients() }
            // setTransients recalculates income. Compare the same displayed instant, since native
            // gift valuation uses cached GPT before the trade updates both civilizations' stats.
            for (civ in runtime.game.civilizations) expected.getCivilization(civ.civID).stats.statsForNextTurn.gold = civ.stats.statsForNextTurn.gold
            check(TradeOperations(expected.getCivilization("Greece"), expected.getCivilization("Rome")).tryEnact(nativeDraft.reverse()))
            val request = obj("op" to "trade_decide", "epoch" to runtime.epoch, "requestId" to "accept-$name", "proposal" to counter["proposal"], "version" to 2, "decision" to "accept")
            val accepted = players.bindPlayer("Rome").communication(request)
            check(accepted["outcome"] != null) { accepted }
            check(players.bindPlayer("Rome").communication(request) == accepted)
            for (civ in runtime.game.civilizations.filter { it.isMajorCiv() }) {
                val native = expected.getCivilization(civ.civID)
                check(civ.gold == native.gold)
                check(civ.getCivResourcesByName() == native.getCivResourcesByName())
                check(civ.cities.map { it.id }.toSet() == native.cities.map { it.id }.toSet())
                check(Json.parseToJsonElement(json().toJson(civ.diplomacy)) == Json.parseToJsonElement(json().toJson(native.diplomacy))) { "$name ${civ.civID}: ${json().toJson(civ.diplomacy)} != ${json().toJson(native.diplomacy)}" }
                val signature = "${civ.gold}:${civ.getCivResourcesByName().toSortedMap()}:${civ.cities.map { it.name }.sorted()}:" +
                    civ.diplomacy.values.sortedBy { it.otherCivName }.joinToString { "${it.otherCivName}:${it.diplomaticStatus}:${it.goldPerTurn()}" }
                val key = "$name:${civ.civID}"
                check(effects.getOrPut(key) { signature } == signature) { "Controller-dependent effects: $key" }
            }
        }
    }
}
