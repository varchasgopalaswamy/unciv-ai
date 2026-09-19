package uncivai

import com.unciv.Constants
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerDiplomacyOperations
import com.unciv.logic.civilization.PlayerFriendshipOperations
import com.unciv.logic.civilization.PlayerPeaceOperations
import com.unciv.logic.trade.Trade
import com.unciv.logic.trade.TradeEvaluation
import com.unciv.logic.trade.TradeLogic
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.json.jsonObject
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.gameHasResult
import uncivai.player.toPlayerContent
import java.security.SecureRandom

@Serializable
internal data class TradeSelection(val type: TradeOfferType, val name: String, val amount: Int, val duration: Int) {
    fun offer() = TradeOffer(name, type, amount, duration)
}

/** Direction is canonical, never implicitly reversed for the reader. */
@Serializable
internal data class TradeClause(val from: String, val to: String, val item: TradeSelection, val city: TradeCity? = null)

/** The city information shown in the trade dialog, frozen when terms are published. */
@Serializable
internal data class TradeCity(val name: String, val population: Int)

@Serializable
internal data class FriendshipTerms(val name: String = "Declaration of Friendship", val duration: Int = PlayerFriendshipOperations.DURATION)

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
    val deadlineMillis: Long,
    val counterofferLimit: Int,
    val turn: Int,
    val consents: List<TradeConsent>,
    val status: String = "PENDING",
    val failure: String? = null,
    val outcome: EngineOutcome? = null,
    val friendship: FriendshipTerms? = null,
) {
    fun participants() = listOf(author, partner)
}

@Serializable
internal data class EngineTradeState(
    val proposals: MutableMap<String, MutableList<EngineTradeProposal>> = mutableMapOf(),
    // Shared short references stay bound to the same engine city across transfer and restart.
    // This private lookup is never part of a player response.
    val cityHandles: MutableMap<String, String> = mutableMapOf(),
    // Retained only for checkpoints written before social promises stopped gating trades.
    // These legacy counters have no effect on consent or native trade legality.
    val warConsentVersions: MutableMap<String, Long> = mutableMapOf(),
    var activeProposal: String? = null,
    var openedAtMillis: Long = 0,
    var completedWaitMillis: Long = 0,
    var counterofferLimit: Int = 3,
    var timeoutSeconds: Int = 300,
)

/** Bilateral consent is independent of hotseat ownership. The enclosing communication operation
 * holds the game executor through validation, engine transfer, receipt and atomic checkpoint.
 * Economic terms and mutual peace use the same canonical proposals and engine transaction.
 */
internal class EngineTrades(private val runtime: GameExecutor) {
    private val state get() = runtime.communicationState.trades
    private val json = Json { encodeDefaults = true }
    private val random = SecureRandom()
    private fun active() = state.activeProposal?.let { state.proposals.getValue(it).last() }

    /** Called under the executor, including by its deadline task. No model or mailbox waits here. */
    fun expireIfNeeded() {
        val proposal = active() ?: return
        if (runtime.clockMillis() < proposal.deadlineMillis) return
        replace(proposal.copy(status = "EXPIRED", failure = "The negotiation deadline elapsed."), "")
        runtime.commitCheckpoint()
    }

    fun negotiation(civilization: String): JsonObject = active()?.let {
        // The pause is global; unrelated players see no private participants or proposal handles.
        if (civilization in it.participants()) {
            obj(
                "active" to true,
                "proposal" to it.proposal,
                "deadlineMillis" to it.deadlineMillis,
                "counteroffersRemaining" to (it.counterofferLimit - it.version + 1),
            )
        } else {
            obj("active" to true)
        }
    } ?: obj("active" to false)

    fun waitMillis(): Long = state.completedWaitMillis + (
        active()?.let {
            (minOf(runtime.clockMillis(), it.deadlineMillis) - state.openedAtMillis).coerceAtLeast(0)
        } ?: 0
        )
    private fun invalid(): Nothing = throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
    private fun player(id: String) = runtime.accessGame { game -> game.civilizations.singleOrNull { it.civID == id && it.isHuman() && it.isMajorCiv() } } ?: invalid()
    private fun known(a: Civilization, b: Civilization) = a !== b && a.knows(b) && b.knows(a)
    private fun relationReason(a: Civilization, b: Civilization): String? = when {
        !known(a, b) -> "Trade requires in-game contact between two human major civilizations."
        a.isDefeated() || b.isDefeated() -> "A civilization has been defeated."
        gameHasResult(a.gameInfo) -> "The game has ended."
        a.isAtWarWith(b) || b.isAtWarWith(a) -> PlayerPeaceOperations(a).negotiationStateReason(b)
        else -> null
    }

    private fun available(logic: TradeLogic, owner: Civilization): List<TradeSelection> {
        val offers = if (logic.ourCivilization === owner) logic.ourAvailableOffers else logic.theirAvailableOffers
        val resources = owner.getCivResourcesByName()
        return offers.filter {
            supportedTradeTerm(it.type, it.name) &&
                (
                    it.type != TradeOfferType.City || (
                        owner.cities.any { city -> city.id == it.name && !city.hasJustBeenConquered } &&
                            owner.popupAlerts.none { alert -> alert.value == it.name && alert.type in setOf(AlertType.CityTraded, AlertType.CityConquered) }
                        )
                    )
        }.map {
            val amount = if (it.type in setOf(TradeOfferType.Luxury_Resource, TradeOfferType.Strategic_Resource)) minOf(it.amount, resources[it.name] ?: 0) else it.amount
            selection(it).copy(amount = amount.coerceAtLeast(0))
        }
    }

    private fun options(terms: List<TradeSelection>, owner: Civilization, reader: Civilization) = terms.map { item ->
        val war = if (item.type == TradeOfferType.WarDeclaration) {
            // Reading the seller's own confirmation could expose civilizations or pacts
            // that only the seller knows. Each signer inspects only their own warnings.
            obj(
                "declaringCivilization" to owner.civID,
                "targetCivilization" to item.name,
                "displayText" to item.offer().getOfferText(),
                "yourDeclarationConfirmation" to if (owner === reader) warConfirmation(reader, item.name) else null,
                "socialAdvisories" to json.encodeToJsonElement(runtime.communication.peaceAdvisories(reader.civID, item.name)),
            )
        } else {
            null
        }
        JsonObject(json.encodeToJsonElement(item).jsonObject + obj("available" to (item.amount > 0), "unavailableReason" to if (item.amount > 0) null else "No quantity is currently available.", "city" to cityDetails(item)?.let { json.encodeToJsonElement(it) }, "war" to war))
    }

    private fun warConfirmation(player: Civilization, target: String) = PlayerDiplomacyOperations(player).declarationConfirmation(player.gameInfo.getCivilization(target)).orEmpty().map { it.tr() }
    private fun warClauses(proposal: EngineTradeProposal) = proposal.clauses.filter { it.item.type == TradeOfferType.WarDeclaration }

    fun read(civilization: String, request: CommunicationRequest): JsonObject {
        val referencesBefore = state.cityHandles.size
        val result = when (request.op) {
            "trade_options" -> {
                val a = player(civilization)
                val b = player(checkNotNull(request.partner))
                if (!known(a, b)) invalid() // Do not probe unknown civilizations' finances or resources.
                val reason = relationReason(a, b)
                val logic = if (reason == null) TradeLogic(a, b) else null
                val give = logic?.let { available(it, a) }.orEmpty()
                val receive = logic?.let { available(it, b) }.orEmpty()
                val researchCost = a.diplomacyFunctions.getResearchAgreementCost(b)
                obj(
                    "partner" to b.civID,
                    "available" to (reason == null),
                    "unavailableReason" to reason,
                    "canInitiate" to (a.gameInfo.currentPlayer == civilization && active() == null && !runtime.isPaused()),
                    "initiationRule" to "Only the current player can open a negotiation. Ask them in chat when off turn. Publishing an offer pauses all gameplay until a decision or the deadline; counters share one limit.",
                    "negotiation" to negotiation(civilization),
                    "requiresPeaceTreaty" to a.isAtWarWith(b),
                    "give" to options(give, a, a),
                    "receive" to options(receive, b, a),
                    "unavailableGive" to bilateralUnavailable(a, b, give, reason),
                    "unavailableReceive" to bilateralUnavailable(b, a, receive, reason),
                    "friendship" to friendshipOption(a, b, reason),
                    "relationship" to bilateralStatus(a, b),
                    "researchFunding" to obj(
                        "costEach" to researchCost,
                        "currentlyFunded" to (a.gold >= researchCost && b.gold >= researchCost),
                        "rule" to "Both players pay the exact treaty amount after immediate gold transfers. Include funding in the deal if needed; gold per turn cannot fund the signing cost.",
                    ),
                    "activeAgreements" to a.getDiplomacyManager(b)!!.trades.mapIndexed { index, trade ->
                        obj("agreement" to runtime.communication.agreements.reference(a.civID, b.civID, index), "clauses" to json.encodeToJsonElement(clauses(a.civID, b.civID, trade.ourOffers.map(::selection), trade.theirOffers.map(::selection))))
                    },
                )
            }

            "trade_read" -> {
                val versions = visible(civilization, checkNotNull(request.proposal))
                obj("trade" to view(versions.last(), civilization), "history" to versions.dropLast(1).map { view(it, civilization) })
            }

            else -> invalid()
        }
        // Queries issue city references; persist them before a client can use them after restart.
        if (state.cityHandles.size != referencesBefore) runtime.commitCheckpoint()
        return result
    }

    fun summaries(civilization: String): List<JsonObject> = state.proposals.values.map { it.last() }.filter { civilization in it.participants() }.map {
        obj("proposal" to it.proposal, "author" to it.author, "partner" to it.partner, "kind" to if (it.friendship != null) "FRIENDSHIP" else "TRADE", "status" to status(it), "deadlineMillis" to it.deadlineMillis)
    }

    private fun visible(civilization: String, id: String): MutableList<EngineTradeProposal> = state.proposals[id]?.takeIf { civilization in it.last().participants() } ?: invalid()
    private fun status(proposal: EngineTradeProposal) = proposal.status
    private fun view(proposal: EngineTradeProposal, civilization: String): JsonObject {
        val state = status(proposal)
        val reason = when {
            state != "PENDING" -> proposal.failure ?: "This proposal is ${state.lowercase()}."
            civilization == proposal.author -> "Publishing these terms already recorded your consent."
            else -> legality(proposal)
        }
        return JsonObject(
            json.encodeToJsonElement(proposal).jsonObject + obj(
                "status" to state,
                "acceptAvailable" to (reason == null),
                "unavailableReason" to reason,
                "counteroffersRemaining" to (proposal.counterofferLimit - proposal.version + 1),
                "counterofferAvailable" to (state == "PENDING" && proposal.friendship == null && proposal.version <= proposal.counterofferLimit),
                "yourWarDeclarations" to warClauses(proposal).filter { it.from == civilization }.map {
                    obj("targetCivilization" to it.item.name, "confirmation" to warConfirmation(player(civilization), it.item.name))
                },
                // Social promises inform this reader's choice without changing native
                // availability or sharing either signer's private promises with the other.
                "socialAdvisories" to json.encodeToJsonElement(
                    warClauses(proposal).flatMap {
                        runtime.communication.peaceAdvisories(civilization, it.item.name)
                    }.distinctBy { it.proposal to it.version },
                ),
            ),
        )
    }

    fun mutate(civilization: String, request: CommunicationRequest): JsonObject = when (request.op) {
        "trade_offer" -> propose(civilization, request)
        "friendship_offer" -> propose(civilization, request, friendship = true)
        "trade_decide" -> decide(civilization, request)
        else -> invalid()
    }

    private fun propose(civilization: String, request: CommunicationRequest, friendship: Boolean = false): JsonObject {
        val a = player(civilization)
        val b = player(checkNotNull(request.partner))
        if (!known(a, b)) invalid()
        val previous = if (request.proposal != null) visible(civilization, checkNotNull(request.proposal)).last() else null
        if (previous != null) {
            requireVersion(previous, request)
            if (previous.friendship != null || previous.participants().toSet() != setOf(civilization, b.civID) || status(previous) != "PENDING") return unavailable("Only a pending trade proposal between these players can be replaced.")
            if (state.activeProposal != previous.proposal) return unavailable("This negotiation is closed.")
            if (previous.version > previous.counterofferLimit) return unavailable("The shared counteroffer limit has been reached. Accept, decline or withdraw the latest terms.", previous, civilization)
        } else if (request.version != null) {
            invalid()
        } else {
            if (active() != null) return unavailable("A negotiation is already holding gameplay. Finish it before opening another.")
            if (runtime.isPaused()) return unavailable("Gameplay is paused by the operator.")
            if (a.gameInfo.currentPlayer != civilization) return unavailable("Only the current player can open a negotiation. Invite them through chat.")
        }
        val now = a.gameInfo.turns
        fun terms(items: List<TradeSelection>): List<TradeSelection> {
            if (items.any { !supportedTradeTerm(it.type, it.name) } || items.map { it.type to it.name }.distinct().size != items.size) invalid()
            return items
        }
        val give = if (friendship) emptyList() else terms(request.give)
        val receive = if (friendship) emptyList() else terms(request.receive)
        if (!friendship && give.isEmpty() && receive.isEmpty()) invalid()
        val id = previous?.proposal ?: generateSequence { "d" + java.lang.Long.toUnsignedString(random.nextLong(), 36).take(8) }.first { it !in state.proposals }
        val proposal = EngineTradeProposal(id, (previous?.version ?: 0) + 1, civilization, b.civID, clauses(civilization, b.civID, give, receive), previous?.deadlineMillis ?: (runtime.clockMillis() + state.timeoutSeconds * 1000L), previous?.counterofferLimit ?: state.counterofferLimit, now, listOf(TradeConsent(civilization, "accept", now)), friendship = if (friendship) FriendshipTerms() else null)
        legality(proposal)?.let { return unavailable(it) }
        if (previous != null) replace(previous.copy(status = "SUPERSEDED"), civilization)
        state.proposals.getOrPut(id) { mutableListOf() }.add(proposal)
        if (previous == null) {
            runtime.communicationState.coordinatedRecovery = true
            state.activeProposal = id
            state.openedAtMillis = runtime.clockMillis()
            runtime.scheduleTradeDeadline(proposal.deadlineMillis)
        }
        publish(proposal, civilization)
        return obj("trade" to view(proposal, civilization))
    }

    private fun decide(civilization: String, request: CommunicationRequest): JsonObject {
        val proposal = visible(civilization, checkNotNull(request.proposal)).last()
        requireVersion(proposal, request)
        val decision = checkNotNull(request.decision)
        if (decision !in setOf("accept", "decline", "withdraw")) invalid()
        if ((decision == "withdraw") != (civilization == proposal.author)) invalid()
        // A second independently identified acceptance of enacted terms cannot repeat transfers.
        if (decision == "accept" && proposal.status == "ENACTED") return obj("trade" to view(proposal, civilization), "outcome" to proposal.outcome?.recordJson())
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
        val trade = engineTrade(proposal)
        logic.currentTrade.set(trade)
        try {
            if (trade.isPeaceTreaty()) {
                // Obsolete native UI requests cannot remain as turn blockers after peace.
                a.tradeRequests.removeAll { it.requestingCiv == b.civID && it.trade.isPeaceTreaty() }
                b.tradeRequests.removeAll { it.requestingCiv == a.civID && it.trade.isPeaceTreaty() }
            }
            if (proposal.friendship != null) {
                a.getDiplomacyManager(b)!!.signDeclarationOfFriendship()
                a.popupAlerts.removeAll { it.type == AlertType.DeclarationOfFriendship && it.value == b.civID }
                b.popupAlerts.removeAll { it.type == AlertType.DeclarationOfFriendship && it.value == a.civID }
            } else {
                logic.acceptTrade()
            }
            a.gameInfo.checkForVictory()
            // A gameplay job must re-observe effects before spending changed resources or ending turn.
            proposal.participants().forEach { runtime.turnSessions[it]?.invalidate() }
            val feedback = if (trade.isPeaceTreaty()) PlayerPeaceOperations(b).acceptancePopupContent(a)?.toPlayerContent() else null
            val outcome = EngineOutcome(
                action = if (proposal.friendship != null) "DECLARE_FRIENDSHIP" else "ENACT_TRADE",
                proposal = proposal.proposal, version = proposal.version, author = civilization, participants = proposal.participants(), turn = now,
                clauses = proposal.clauses, friendship = proposal.friendship, confirmation = bilateralConfirmation(a, b),
                cities = proposal.clauses.filter { it.item.type == TradeOfferType.City }.map { TradedCityOwner(it.item.name, it.to) }, feedback = feedback,
                wars = warClauses(proposal).map { ConfirmedTradeWar(it.from, it.item.name, player(it.from).isAtWarWith(a.gameInfo.getCivilization(it.item.name))) },
            )
            val updated = proposal.copy(status = "ENACTED", consents = proposal.consents + TradeConsent(civilization, "accept", now), outcome = outcome)
            replace(updated, civilization)
            if (trade.isPeaceTreaty() || proposal.friendship != null) {
                invalidateDiplomacy(a.civID, b.civID)
            }
            runtime.communicationState.outcomes.add(outcome)
            runtime.communication.emit(proposal.participants(), "engine", outcome.recordJson(), proposal.participants().toSet() - civilization)
            for (war in outcome.wars) {
                runtime.turnSessions[war.targetCivilization]?.invalidate()
                runtime.communication.diplomacyEnacted(war.declaringCivilization, war.targetCivilization, "DECLARE_WAR", now)
            }
            return obj("trade" to view(updated, civilization), "outcome" to outcome.recordJson())
        } catch (failure: Throwable) {
            runtime.haltAfterMutation(failure)
        }
    }

    private fun legality(proposal: EngineTradeProposal): String? {
        val a = player(proposal.author)
        val b = player(proposal.partner)
        relationReason(a, b)?.let { return it }
        if (proposal.friendship != null) return PlayerFriendshipOperations(a).stateReason(b)
        // Native treaties execute from one side only. Consent must contain exactly the
        // same treaty, cost and duration in both directions.
        val ourTreaties = proposal.clauses.filter { it.from == a.civID && it.item.type == TradeOfferType.Treaty }.map { it.item }.toSet()
        val theirTreaties = proposal.clauses.filter { it.from == b.civID && it.item.type == TradeOfferType.Treaty }.map { it.item }.toSet()
        if (ourTreaties != theirTreaties) return "Every treaty must have identical quantity, cost and duration on both sides."
        for (owner in listOf(a, b)) {
            val treaties = proposal.clauses.filter { it.from == owner.civID && it.item.type == TradeOfferType.Treaty }
            if (a.isAtWarWith(b)) {
                if (treaties.size != 1 || treaties.single().item != TradeSelection(TradeOfferType.Treaty, Constants.peaceTreaty, 1, a.gameInfo.speed.peaceDealDuration)) return "Peace requires one Peace Treaty on each side with the engine's quantity and duration."
            } else if (treaties.any { it.item.name == Constants.peaceTreaty }) {
                return "These civilizations are already at peace."
            }
        }
        val logic = TradeLogic(a, b)
        for (owner in listOf(a, b)) {
            val available = available(logic, owner)
            for (clause in proposal.clauses.filter { it.from == owner.civID }) {
                val item = clause.item
                if (available.none {
                        it.type == item.type && it.name == item.name && it.duration == item.duration &&
                            if (item.type == TradeOfferType.Treaty) it.amount == item.amount else it.amount >= item.amount
                    }
                ) {
                    return "${owner.civID}'s offered ${item.name} quantity, cost or duration is no longer available."
                }
            }
        }
        for (recipient in listOf(a, b)) {
            // The engine transfers each side separately. A safe final net balance is insufficient
            // if an incoming transfer could overflow before this player's outgoing transfer.
            val incoming = proposal.clauses.filter { it.item.type == TradeOfferType.Gold && it.to == recipient.civID }.sumOf { it.item.amount.toLong() }
            if (recipient.gold.toLong() + incoming > Int.MAX_VALUE) return "The gold transfer exceeds the engine balance limit."
            val outgoing = proposal.clauses.filter { it.item.type == TradeOfferType.Gold && it.from == recipient.civID }.sumOf { it.item.amount.toLong() }
            val researchCost = ourTreaties.singleOrNull { it.name == Constants.researchAgreement }?.amount ?: 0
            if (recipient.gold.toLong() + incoming - outgoing < researchCost) return "${recipient.civID} cannot fund the research agreement after the proposed gold transfers."
        }
        return if (TradeEvaluation().isTradeValid(engineTrade(proposal), a, b)) null else "The engine no longer permits these trade terms."
    }

    fun invalidateDiplomacy(a: String, b: String) {
        state.proposals.values.map { it.last() }.filter { it.status == "PENDING" && it.participants().toSet() == setOf(a, b) }.forEach {
            replace(it.copy(status = "INVALID", failure = "Diplomatic relations changed after this proposal was published."), a)
        }
    }

    private fun engineTrade(proposal: EngineTradeProposal) = Trade().apply {
        ourOffers.addAll(proposal.clauses.filter { it.from == proposal.author }.map { engineOffer(it.item) })
        theirOffers.addAll(proposal.clauses.filter { it.from == proposal.partner }.map { engineOffer(it.item) })
    }
    private fun selection(offer: TradeOffer) = TradeSelection(
        offer.type,
        if (offer.type == TradeOfferType.City) {
            state.cityHandles.getOrPut(offer.name) {
                // Random short references don't reveal other players' city counts or query order.
                generateSequence { "tc" + random.nextInt(1_679_616).toString(36) }.first { it !in state.cityHandles.values }
            }
        } else {
            offer.name
        },
        offer.amount,
        offer.duration,
    )
    private fun engineCityId(item: TradeSelection): String? = state.cityHandles.entries.singleOrNull { it.value == item.name }?.key
    private fun engineOffer(item: TradeSelection) = if (item.type == TradeOfferType.City) TradeOffer(engineCityId(item) ?: invalid(), item.type, item.amount, item.duration) else item.offer()
    private fun cityDetails(item: TradeSelection): TradeCity? = if (item.type == TradeOfferType.City) {
        runtime.accessGame { game ->
            game.getCities().firstOrNull { it.id == engineCityId(item) }?.let { TradeCity(it.name, it.population.population) }
        }
    } else {
        null
    }
    private fun clauses(a: String, b: String, give: List<TradeSelection>, receive: List<TradeSelection>) = give.map { TradeClause(a, b, it, cityDetails(it)) } + receive.map { TradeClause(b, a, it, cityDetails(it)) }
    private fun requireVersion(proposal: EngineTradeProposal, request: CommunicationRequest) {
        if (request.version != proposal.version) throw PlayerSessionException(PlayerSessionFailure.STALE_REVISION)
    }
    private fun replace(proposal: EngineTradeProposal, actor: String) {
        state.proposals.getValue(proposal.proposal)[proposal.version - 1] = proposal
        if (state.activeProposal == proposal.proposal && proposal.status !in setOf("PENDING", "SUPERSEDED")) {
            state.completedWaitMillis += (minOf(runtime.clockMillis(), proposal.deadlineMillis) - state.openedAtMillis).coerceAtLeast(0)
            state.activeProposal = null
            proposal.participants().forEach { runtime.turnSessions[it]?.invalidate() }
        }
        publish(proposal, actor)
    }
    private fun publish(proposal: EngineTradeProposal, actor: String) {
        val body = JsonObject(json.encodeToJsonElement(proposal).jsonObject + obj("participants" to proposal.participants()))
        runtime.communication.emit(proposal.participants(), "trade", body, proposal.participants().toSet() - actor)
    }
    private fun unavailable(reason: String, proposal: EngineTradeProposal? = null, civilization: String? = null) = obj("error" to "TRADE_UNAVAILABLE", "reason" to reason, "trade" to proposal?.let { view(it, checkNotNull(civilization)) })
}
