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

/** An exact selection from a displayed trade table. Duration is revalidated by the engine. */
@Serializable
data class PeaceTermSelection(val type: String, val name: String, val amount: Int, val duration: Int)

@Serializable
data class PeaceTerms(val give: List<PeaceTermSelection>, val receive: List<PeaceTermSelection>)

@Serializable
data class PeaceTerm(
    val type: String,
    val name: String?,
    val amount: Int,
    val duration: Int,
    val supported: Boolean,
    val displayText: String,
    val ownedByYou: Int? = null,
)

/** Option amounts are current maxima; give/receive always refer to the authenticated player. */
@Serializable
data class PeaceOptions(val give: List<PeaceTerm>, val receive: List<PeaceTerm>)

/** Current trade overview rows, including paid one-time consideration while a treaty remains. */
@Serializable
data class PeaceAgreement(val give: List<PeaceTerm>, val receive: List<PeaceTerm>)

@Serializable
data class PeaceOffer(
    val handle: String,
    val civilizationId: String,
    val civilizationName: String,
    val leaderName: String,
    val message: String,
    val give: List<PeaceTerm>,
    val receive: List<PeaceTerm>,
    val accept: DiplomaticAction,
    val reject: DiplomaticAction,
    val counteroffer: DiplomaticAction,
    val acceptLabel: String = "Sounds good!",
    val rejectLabel: String = "Not this time.",
    val counterofferLabel: String = "How about something else...",
)

@Serializable
data class OutgoingPeaceOffer(
    val civilizationId: String,
    val give: List<PeaceTerm>,
    val receive: List<PeaceTerm>,
    val retract: DiplomaticAction,
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
    val peaceOptions: PeaceOptions?,
    val agreements: List<PeaceAgreement>,
)

@Serializable
data class DiplomacyObservation(
    val knownCivilizations: List<KnownCivilization> = emptyList(),
    val incomingOffers: List<PeaceOffer> = emptyList(),
    val outgoingOffers: List<OutgoingPeaceOffer> = emptyList(),
)

/** Copy the introduction of an actual player-owned alert, never a caller-supplied civilization. */
internal fun observeFirstContact(operations: PlayerOperations, alert: PopupAlert): FirstContactDetails? = operations.firstContactIntroduction(alert)?.let {
    FirstContactDetails(it.civilizationId, it.civilizationName, it.leaderName, it.message.tr(), it.acknowledgement.tr())
}

internal fun observeDiplomacy(player: Civilization, state: TurnSessionState, active: Boolean): DiplomacyObservation {
    val diplomacy = PlayerDiplomacyOperations(player)
    val peace = PlayerPeaceOperations(player)
    fun action(reasons: List<String>) = DiplomaticAction(
        active && reasons.isEmpty(),
        immutable((reasons + if (active || reasons.isNotEmpty()) emptyList() else listOf("Not the active player turn.")).map { it.tr() }),
    )
    fun action(reason: String?) = action(listOfNotNull(reason))
    val resources = player.getCivResourcesByName()
    fun terms(terms: List<PeaceTradeTerm>): List<PeaceTerm> = immutable(
        terms.map { term ->
            val supported = PlayerPeaceOperations.supportsTerm(term)
            val resource = term.type in setOf(TradeOfferType.Luxury_Resource, TradeOfferType.Strategic_Resource)
            // In particular, never resolve a raw city ID via the global desktop game singleton.
            // Unsupported offers are described explicitly and may only be rejected here.
            val display = if (supported) {
                TradeOffer(term.name, term.type, term.amount, term.duration).getOfferText()
            } else {
                "Unsupported trade term: [${term.type.name}]".tr()
            }
            PeaceTerm(
                term.type.name,
                term.name.takeIf { supported },
                term.amount,
                term.duration,
                supported,
                display,
                if (resource) resources[term.name] else null,
            )
        },
    )
    val known = player.diplomacyFunctions.getKnownCivsSorted(includeCityStates = false).filter { it.isMajorCiv() }.toList()
    state.tradeHandles.keys.removeIf { old -> player.tradeRequests.none { it === old } }
    val incoming = player.tradeRequests.mapNotNull { request ->
        val trade = peace.inspectIncoming(request) ?: return@mapNotNull null
        val other = player.gameInfo.civilizations.single { it.civID == request.requestingCiv }
        PeaceOffer(
            state.tradeHandles.getOrPut(request) { state.identifiers.next(PlayerIdentifier.OFFER) },
            other.civID, other.civName, other.getLeaderDisplayName(), other.nation.tradeRequest.tr(),
            terms(trade.ourOffers), terms(trade.theirOffers),
            action(peace.acceptUnavailableReason(request)), action(peace.rejectUnavailableReason(request)),
            action(peace.counterofferUnavailableReason(request)),
            "Sounds good!".tr(), "Not this time.".tr(), "How about something else...".tr(),
        )
    }
    return DiplomacyObservation(
        knownCivilizations = immutable(
            known.mapNotNull { other ->
                val options = diplomacy.options(other) ?: return@mapNotNull null
                val outgoing = peace.pendingOutgoing(other)
                val peaceOptions = peace.availableTerms(other)
                KnownCivilization(
                    other.civID, other.civName, other.getLeaderDisplayName(), player.isAtWarWith(other),
                    options.status.name, options.peaceTreatyTurns, action(options.declareWar.unavailableReasons),
                    immutable(options.declarationConfirmation.map { it.tr() }),
                    action(peace.negotiationUnavailableReason(other) ?: if (outgoing != null) "Retract your existing offer first." else null),
                    peaceOptions?.let { PeaceOptions(terms(it.ourOptions), terms(it.theirOptions)) },
                    immutable(peace.activeAgreements(other).map { PeaceAgreement(terms(it.ourOffers), terms(it.theirOffers)) }),
                )
            },
        ),
        incomingOffers = immutable(incoming),
        outgoingOffers = immutable(
            known.mapNotNull { other ->
                val trade = peace.pendingOutgoing(other) ?: return@mapNotNull null
                OutgoingPeaceOffer(other.civID, terms(trade.ourOffers), terms(trade.theirOffers), action(peace.retractUnavailableReason(other)))
            },
        ),
    )
}

internal fun PeaceTermSelection.engineTerm(): PeaceTradeTerm = PeaceTradeTerm(name, TradeOfferType.valueOf(type), amount, duration)

private fun <T> immutable(values: List<T>): List<T> = Collections.unmodifiableList(values.toList())
