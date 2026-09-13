package uncivai

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.trade.Trade
import com.unciv.logic.trade.TradeEvaluation
import com.unciv.logic.trade.TradeLogic
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.gameHasResult
import java.security.SecureRandom

@Serializable
internal data class TradeSelection(val type: TradeOfferType, val name: String, val amount: Int, val duration: Int) {
    fun offer() = TradeOffer(name, type, amount, duration)
}

/** Direction is canonical, never implicitly reversed for the reader. */
@Serializable
internal data class TradeClause(val from: String, val to: String, val item: TradeSelection)

@Serializable
internal data class TradeConsent(val author: String, val decision: String, val turn: Int)

/** Terms never change within a version. State transitions retain terms and consent history. */
@Serializable
internal data class EngineTradeProposal(
    val proposal: String,
    val version: Int,
    val author: String,
    val partner: String,
    val clauses: List<TradeClause>,
    val expiresTurn: Int,
    val turn: Int,
    val consents: List<TradeConsent>,
    val status: String = "PENDING",
    val failure: String? = null,
    val outcome: JsonObject? = null,
) {
    fun participants() = listOf(author, partner)
}

@Serializable
internal data class EngineTradeState(val proposals: MutableMap<String, MutableList<EngineTradeProposal>> = mutableMapOf())

/** Bilateral consent is independent of hotseat ownership. The enclosing communication operation
 * holds the game executor through validation, engine transfer, receipt and atomic checkpoint.
 * Only the four ordinary economic categories can reach TradeLogic.acceptTrade here.
 */
internal class EngineTrades(private val runtime: GameExecutor) {
    private val state get() = runtime.communicationState.trades
    private val json = Json { encodeDefaults = true }
    private val random = SecureRandom()
    private val supported = setOf(TradeOfferType.Gold, TradeOfferType.Gold_Per_Turn, TradeOfferType.Luxury_Resource, TradeOfferType.Strategic_Resource)

    private fun invalid(): Nothing = throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
    private fun text(request: JsonObject, key: String) = (request[key] as? JsonPrimitive)?.takeIf { it.isString }?.content?.takeIf { it.isNotBlank() && it.length <= 128 } ?: invalid()
    private fun player(id: String) = runtime.accessGame { game -> game.civilizations.singleOrNull { it.civID == id && it.isHuman() && it.isMajorCiv() } } ?: invalid()
    private fun known(a: Civilization, b: Civilization) = a !== b && a.knows(b) && b.knows(a)
    private fun relationReason(a: Civilization, b: Civilization): String? = when {
        !known(a, b) -> "Trade requires in-game contact between two human major civilizations."
        a.isDefeated() || b.isDefeated() -> "A civilization has been defeated."
        gameHasResult(a.gameInfo) -> "The game has ended."
        a.isAtWarWith(b) || b.isAtWarWith(a) -> "Ordinary trade requires peace. Use peace negotiations to end the war."
        else -> null
    }

    private fun available(logic: TradeLogic, owner: Civilization): List<TradeSelection> {
        val offers = if (logic.ourCivilization === owner) logic.ourAvailableOffers else logic.theirAvailableOffers
        val resources = owner.getCivResourcesByName()
        return offers.filter { it.type in supported }.map {
            val amount = if (it.type in setOf(TradeOfferType.Luxury_Resource, TradeOfferType.Strategic_Resource)) minOf(it.amount, resources[it.name] ?: 0) else it.amount
            TradeSelection(it.type, it.name, amount.coerceAtLeast(0), it.duration)
        }
    }

    private fun options(terms: List<TradeSelection>) = terms.map { item ->
        JsonObject(json.encodeToJsonElement(item).jsonObject + obj("available" to (item.amount > 0), "unavailableReason" to if (item.amount > 0) null else "No quantity is currently available."))
    }

    fun read(civilization: String, request: JsonObject): JsonObject = when (text(request, "op")) {
        "trade_options" -> {
            if (request.keys != setOf("op", "epoch", "partner")) invalid()
            val a = player(civilization)
            val b = player(text(request, "partner"))
            if (!known(a, b)) invalid() // Do not probe unknown civilizations' finances or resources.
            val reason = relationReason(a, b)
            val logic = if (reason == null) TradeLogic(a, b) else null
            obj(
                "partner" to b.civID,
                "available" to (reason == null),
                "unavailableReason" to reason,
                "give" to logic?.let { options(available(it, a)) }.orEmpty(),
                "receive" to logic?.let { options(available(it, b)) }.orEmpty(),
                "activeAgreements" to a.getDiplomacyManager(b)!!.trades.map { trade ->
                    obj("clauses" to json.encodeToJsonElement(clauses(a.civID, b.civID, trade.ourOffers.map(::selection), trade.theirOffers.map(::selection))))
                },
            )
        }

        "trade_read" -> {
            if (request.keys != setOf("op", "epoch", "proposal")) invalid()
            val versions = visible(civilization, text(request, "proposal"))
            obj("trade" to view(versions.last(), civilization), "history" to versions.dropLast(1).map { view(it, civilization) })
        }

        else -> invalid()
    }

    fun summaries(civilization: String): List<JsonObject> = state.proposals.values.map { it.last() }.filter { civilization in it.participants() }.map {
        obj("proposal" to it.proposal, "author" to it.author, "partner" to it.partner, "status" to status(it), "expiresTurn" to it.expiresTurn)
    }

    private fun visible(civilization: String, id: String): MutableList<EngineTradeProposal> = state.proposals[id]?.takeIf { civilization in it.last().participants() } ?: invalid()
    private fun status(proposal: EngineTradeProposal) = if (proposal.status == "PENDING" && runtime.accessGame { it.turns } >= proposal.expiresTurn) "EXPIRED" else proposal.status
    private fun view(proposal: EngineTradeProposal, civilization: String): JsonObject {
        val state = status(proposal)
        val reason = when {
            state != "PENDING" -> proposal.failure ?: "This proposal is ${state.lowercase()}."
            civilization == proposal.author -> "Publishing these terms already recorded your consent."
            else -> legality(proposal)
        }
        return JsonObject(json.encodeToJsonElement(proposal).jsonObject + obj("status" to state, "acceptAvailable" to (reason == null), "unavailableReason" to reason))
    }

    fun mutate(civilization: String, request: JsonObject): JsonObject = when (text(request, "op")) {
        "trade_offer" -> propose(civilization, request)
        "trade_decide" -> decide(civilization, request)
        else -> invalid()
    }

    private fun propose(civilization: String, request: JsonObject): JsonObject {
        if (request.keys.any { it !in setOf("op", "epoch", "requestId", "partner", "give", "receive", "expiresTurn", "proposal", "version") }) invalid()
        val a = player(civilization)
        val b = player(text(request, "partner"))
        if (!known(a, b)) invalid()
        val previous = if ("proposal" in request) visible(civilization, text(request, "proposal")).last() else null
        if (previous != null) {
            requireVersion(previous, request)
            if (previous.participants().toSet() != setOf(civilization, b.civID) || status(previous) != "PENDING") return unavailable("Only a pending proposal between these players can be replaced.")
        } else if ("version" in request) {
            invalid()
        }
        val now = a.gameInfo.turns
        val expires = if ("expiresTurn" in request) request["expiresTurn"]?.jsonPrimitive?.intOrNull ?: invalid() else now + 3
        if (expires <= now) invalid()
        fun terms(key: String): List<TradeSelection> {
            val terms = try {
                json.decodeFromJsonElement<List<TradeSelection>>(request[key] ?: invalid())
            } catch (_: IllegalArgumentException) {
                invalid()
            }
            if (terms.size > 64 || terms.any { it.type !in supported || it.amount <= 0 || it.name.isBlank() || it.name.length > 256 } || terms.map { it.type to it.name }.distinct().size != terms.size) invalid()
            return terms
        }
        val give = terms("give")
        val receive = terms("receive")
        if (give.isEmpty() && receive.isEmpty()) invalid()
        val id = previous?.proposal ?: generateSequence { "d" + java.lang.Long.toUnsignedString(random.nextLong(), 36).take(8) }.first { it !in state.proposals }
        val proposal = EngineTradeProposal(id, (previous?.version ?: 0) + 1, civilization, b.civID, clauses(civilization, b.civID, give, receive), expires, now, listOf(TradeConsent(civilization, "accept", now)))
        legality(proposal)?.let { return unavailable(it) }
        if (previous != null) replace(previous.copy(status = "SUPERSEDED"), civilization)
        state.proposals.getOrPut(id) { mutableListOf() }.add(proposal)
        publish(proposal, civilization)
        return obj("trade" to view(proposal, civilization))
    }

    private fun decide(civilization: String, request: JsonObject): JsonObject {
        if (request.keys != setOf("op", "epoch", "requestId", "proposal", "version", "decision")) invalid()
        val proposal = visible(civilization, text(request, "proposal")).last()
        requireVersion(proposal, request)
        val decision = text(request, "decision")
        if (decision !in setOf("accept", "decline", "withdraw")) invalid()
        if ((decision == "withdraw") != (civilization == proposal.author)) invalid()
        // A second independently identified acceptance of enacted terms cannot repeat transfers.
        if (decision == "accept" && proposal.status == "ENACTED") return obj("trade" to view(proposal, civilization), "outcome" to proposal.outcome)
        if (status(proposal) != "PENDING") return unavailable("This proposal is ${status(proposal).lowercase()}.", proposal, civilization)
        val now = runtime.accessGame { it.turns }
        if (decision != "accept") {
            val updated = proposal.copy(status = if (decision == "decline") "DECLINED" else "WITHDRAWN", consents = proposal.consents + TradeConsent(civilization, decision, now))
            replace(updated, civilization)
            return obj("trade" to view(updated, civilization))
        }
        legality(proposal)?.let { reason ->
            val invalid = proposal.copy(status = "INVALID", failure = reason)
            replace(invalid, civilization)
            return unavailable(reason, invalid, civilization)
        }
        val a = player(proposal.author)
        val b = player(proposal.partner)
        val logic = TradeLogic(a, b)
        logic.currentTrade.set(engineTrade(proposal))
        try {
            logic.acceptTrade()
            // A gameplay job must re-observe effects before spending changed resources or ending turn.
            proposal.participants().forEach { runtime.turnSessions[it]?.invalidate() }
            val outcome = obj("action" to "ENACT_TRADE", "proposal" to proposal.proposal, "version" to proposal.version, "author" to civilization, "participants" to proposal.participants(), "turn" to now, "clauses" to json.encodeToJsonElement(proposal.clauses), "confirmation" to obj("source" to "engine"))
            val updated = proposal.copy(status = "ENACTED", consents = proposal.consents + TradeConsent(civilization, "accept", now), outcome = outcome)
            replace(updated, civilization)
            runtime.communicationState.outcomes.add(outcome)
            runtime.communication.emit(proposal.participants(), "engine", outcome, proposal.participants().toSet() - civilization)
            return obj("trade" to view(updated, civilization), "outcome" to outcome)
        } catch (failure: Throwable) {
            runtime.haltAfterMutation(failure)
        }
    }

    private fun legality(proposal: EngineTradeProposal): String? {
        val a = player(proposal.author)
        val b = player(proposal.partner)
        relationReason(a, b)?.let { return it }
        val logic = TradeLogic(a, b)
        for (owner in listOf(a, b)) {
            val available = available(logic, owner)
            for (clause in proposal.clauses.filter { it.from == owner.civID }) {
                val item = clause.item
                if (available.none { it.type == item.type && it.name == item.name && it.duration == item.duration && it.amount >= item.amount }) return "${owner.civID}'s offered ${item.name} quantity or duration is no longer available."
            }
        }
        for (recipient in listOf(a, b)) {
            // The engine transfers each side separately. A safe final net balance is insufficient
            // if an incoming transfer could overflow before this player's outgoing transfer.
            val incoming = proposal.clauses.filter { it.item.type == TradeOfferType.Gold && it.to == recipient.civID }.sumOf { it.item.amount.toLong() }
            if (recipient.gold.toLong() + incoming > Int.MAX_VALUE) return "The gold transfer exceeds the engine balance limit."
        }
        return if (TradeEvaluation().isTradeValid(engineTrade(proposal), a, b)) null else "The engine no longer permits these trade terms."
    }

    fun invalidateDiplomacy(a: String, b: String) {
        state.proposals.values.map { it.last() }.filter { it.status == "PENDING" && it.participants().toSet() == setOf(a, b) }.forEach {
            replace(it.copy(status = "INVALID", failure = "Diplomatic relations changed after this proposal was published."), a)
        }
    }

    private fun engineTrade(proposal: EngineTradeProposal) = Trade().apply {
        ourOffers.addAll(proposal.clauses.filter { it.from == proposal.author }.map { it.item.offer() })
        theirOffers.addAll(proposal.clauses.filter { it.from == proposal.partner }.map { it.item.offer() })
    }
    private fun selection(offer: TradeOffer) = TradeSelection(offer.type, offer.name, offer.amount, offer.duration)
    private fun clauses(a: String, b: String, give: List<TradeSelection>, receive: List<TradeSelection>) = give.map { TradeClause(a, b, it) } + receive.map { TradeClause(b, a, it) }
    private fun requireVersion(proposal: EngineTradeProposal, request: JsonObject) {
        if (request["version"]?.jsonPrimitive?.intOrNull != proposal.version) throw PlayerSessionException(PlayerSessionFailure.STALE_REVISION)
    }
    private fun replace(proposal: EngineTradeProposal, actor: String) {
        state.proposals.getValue(proposal.proposal)[proposal.version - 1] = proposal
        publish(proposal, actor)
    }
    private fun publish(proposal: EngineTradeProposal, actor: String) {
        val body = JsonObject(json.encodeToJsonElement(proposal).jsonObject + obj("participants" to proposal.participants()))
        runtime.communication.emit(proposal.participants(), "trade", body, proposal.participants().toSet() - actor)
    }
    private fun unavailable(reason: String, proposal: EngineTradeProposal? = null, civilization: String? = null) = obj("error" to "TRADE_UNAVAILABLE", "reason" to reason, "trade" to proposal?.let { view(it, checkNotNull(civilization)) })
}
