package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PeaceTradeTerm
import com.unciv.logic.civilization.PlayerDiplomacyOperations
import com.unciv.logic.civilization.PlayerOperations
import com.unciv.logic.civilization.PlayerPeaceOperations
import com.unciv.logic.civilization.PopupAlert
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import uncivai.GameExecutor
import uncivai.bilateralStatus
import uncivai.supportedTradeTerm
import java.util.Collections

@Serializable
data class FirstContactDetails(
    val civilizationId: String,
    val civilizationName: String,
    val leaderName: String,
    val message: String,
    val acknowledgement: String,
)

@Serializable
data class DiplomaticAction(val available: Boolean, val unavailableReasons: List<String>)

@Serializable
data class DiplomacyTradeTerm(
    val type: String,
    val name: String?,
    val amount: Int,
    val duration: Int,
    val supported: Boolean,
    val displayText: String,
    val ownedByYou: Int? = null,
)

/** Current trade overview rows, including paid one-time consideration while a treaty remains. */
@Serializable
data class TradeAgreement(val give: List<DiplomacyTradeTerm>, val receive: List<DiplomacyTradeTerm>, val agreement: String? = null)

/** Native requests link to their canonical proposal when the AI bridge owns the decision. */
@Serializable
data class UnhandledTradeRequest(
    val civilizationId: String,
    val civilizationName: String,
    val leaderName: String,
    val message: String,
    val give: List<DiplomacyTradeTerm>,
    val receive: List<DiplomacyTradeTerm>,
    val unavailableReason: String? = "This saved UI request cannot be acted on through trade proposals.",
    val proposal: String? = null,
    val version: Int? = null,
)

@Serializable
data class KnownCivilization(
    val civilizationId: String,
    val civilizationName: String,
    val leaderName: String,
    val atWar: Boolean,
    val status: String,
    val peaceTreatyTurns: Int,
    val declareWar: DiplomaticAction,
    val declarationConfirmation: List<String>,
    val negotiatePeace: DiplomaticAction,
    val agreements: List<TradeAgreement>,
    val bilateral: JsonObject,
    val native: NativeDiplomacyDetails? = null,
)

@Serializable
data class DiplomacyObservation(
    val knownCivilizations: List<KnownCivilization> = emptyList(),
    val cityStates: List<CityStateDetails> = emptyList(),
    val incomingRequests: List<UnhandledTradeRequest> = emptyList(),
    val outgoingRequests: List<UnhandledTradeRequest> = emptyList(),
)

/** Copy the introduction of an actual player-owned alert, never a caller-supplied civilization. */
internal fun observeFirstContact(operations: PlayerOperations, alert: PopupAlert): FirstContactDetails? = operations.firstContactIntroduction(alert)?.let {
    FirstContactDetails(it.civilizationId, it.civilizationName, it.leaderName, it.message.tr(), it.acknowledgement.tr())
}

internal fun observeDiplomacy(player: Civilization, active: Boolean, communicationAvailable: Boolean, runtime: GameExecutor): DiplomacyObservation {
    runtime.communication.agreements.synchronize(player.gameInfo)
    val diplomacy = PlayerDiplomacyOperations(player)
    val peace = PlayerPeaceOperations(player)
    fun action(reasons: List<String>) = DiplomaticAction(
        active && reasons.isEmpty(),
        immutable((reasons + if (active || reasons.isNotEmpty()) emptyList() else listOf("Not the active player turn.")).map { it.tr() }),
    )
    val resources = player.getCivResourcesByName()
    fun terms(terms: List<PeaceTradeTerm>, other: Civilization): List<DiplomacyTradeTerm> = immutable(
        terms.map { term ->
            // Imported requests may contain targets that this reader has never met.
            // Supporting the category does not authorize revealing those identities.
            val city = (player.cities + other.cities).firstOrNull { it.id == term.name }
            val supported = supportedTradeTerm(term.type, term.name) && when (term.type) {
                TradeOfferType.WarDeclaration, TradeOfferType.PeaceProposal -> player.getKnownCivs().any { it.isMajorCiv() && it.civID == term.name }
                TradeOfferType.City -> city != null
                else -> true
            }
            val resource = term.type in setOf(TradeOfferType.Luxury_Resource, TradeOfferType.Strategic_Resource)
            // In particular, never resolve a raw city ID via the global desktop game singleton.
            // Unsupported offers are described without revealing private identifiers.
            val display = if (term.type == TradeOfferType.City && supported) {
                checkNotNull(city).let { "${it.name} (${it.population.population})" }
            } else if (supported) {
                TradeOffer(term.name, term.type, term.amount, term.duration).getOfferText()
            } else {
                "Unsupported trade term: [${term.type.name}]".tr()
            }
            DiplomacyTradeTerm(
                term.type.name,
                term.name.takeIf { supported && term.type != TradeOfferType.City },
                term.amount,
                term.duration,
                supported,
                display,
                if (resource) resources[term.name] else null,
            )
        },
    )
    val known = player.diplomacyFunctions.getKnownCivsSorted(includeCityStates = false).filter { it.isMajorCiv() }.toList()
    fun request(other: Civilization, trade: com.unciv.logic.civilization.PeaceTrade, requestId: String?): UnhandledTradeRequest {
        val proposal = runtime.communicationState.trades.proposals.values.map { it.last() }.firstOrNull {
            requestId != null && requestId.isNotEmpty() && it.nativeRequestId == requestId && it.status == "PENDING" && player.civID in it.participants()
        }
        return UnhandledTradeRequest(
            other.civID,
            other.civName,
            other.getLeaderDisplayName(),
            other.nation.tradeRequest.tr(),
            terms(trade.ourOffers, other),
            terms(trade.theirOffers, other),
            if (proposal == null) "This saved UI request cannot be acted on through trade proposals." else proposal.failure,
            proposal?.proposal,
            proposal?.version,
        )
    }
    val incoming = player.tradeRequests.mapNotNull { incoming ->
        val trade = peace.inspectIncoming(incoming) ?: return@mapNotNull null
        request(player.gameInfo.getCivilization(incoming.requestingCiv), trade, incoming.requestId)
    }
    return DiplomacyObservation(
        cityStates = observeCityStates(player, active),
        knownCivilizations = immutable(
            known.mapNotNull { other ->
                val options = diplomacy.options(other) ?: return@mapNotNull null
                val peaceReason = peace.negotiationStateReason(other) ?: if (communicationAvailable) null else "Communication is unavailable."
                KnownCivilization(
                    other.civID, other.civName, other.getLeaderDisplayName(), player.isAtWarWith(other),
                    options.status.name, options.peaceTreatyTurns, action(options.declareWar.unavailableReasons),
                    immutable(options.declarationConfirmation.map { it.tr() }),
                    DiplomaticAction(peaceReason == null, immutable(listOfNotNull(peaceReason?.tr()))),
                    immutable(peace.activeAgreements(other).mapIndexed { index, trade -> TradeAgreement(terms(trade.ourOffers, other), terms(trade.theirOffers, other), runtime.communication.agreements.reference(player.civID, other.civID, index)) }),
                    bilateralStatus(player, other),
                    nativeDiplomacy(player, other, active),
                )
            },
        ),
        incomingRequests = immutable(incoming),
        outgoingRequests = immutable(
            known.mapNotNull { other ->
                val trade = peace.pendingOutgoing(other) ?: return@mapNotNull null
                request(other, trade, other.tradeRequests.firstOrNull { it.requestingCiv == player.civID }?.requestId)
            },
        ),
    )
}

private fun <T> immutable(values: List<T>): List<T> = Collections.unmodifiableList(values.toList())
