package uncivai

import com.unciv.Constants
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerFriendshipOperations
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.civilization.diplomacy.DiplomaticModifiers
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

internal fun supportedTradeTerm(type: TradeOfferType, name: String): Boolean = when (type) {
    TradeOfferType.Gold, TradeOfferType.Gold_Per_Turn, TradeOfferType.Luxury_Resource,
    TradeOfferType.Strategic_Resource, TradeOfferType.City,
    -> true

    TradeOfferType.Treaty -> name in setOf(Constants.peaceTreaty, Constants.researchAgreement, Constants.defensivePact)

    TradeOfferType.Agreement -> name == Constants.openBorders

    TradeOfferType.Embassy -> name == Constants.acceptEmbassy

    else -> false
}

/** Shared historical facts name both civilizations instead of addressing a particular reader. */
@Serializable
internal data class BilateralConfirmation(val relationships: Map<String, ConfirmedRelationship>, val source: String = "engine")

@Serializable
internal data class ConfirmedRelationship(
    val otherCivilizationId: String,
    val atWar: Boolean,
    val peaceTreatyTurns: Int,
    val friendshipTurns: Int,
    val researchAgreementTurns: Int,
    val defensivePactTurns: Int,
    val mayEnterOtherTerritory: Boolean,
    val hasEmbassyInOtherCapital: Boolean,
)

private fun confirmedRelationship(player: Civilization, other: Civilization): ConfirmedRelationship {
    val diplomacy = player.getDiplomacyManager(other)!!
    return ConfirmedRelationship(
        other.civID,
        player.isAtWarWith(other),
        diplomacy.turnsToPeaceTreaty(),
        diplomacy.getFlag(DiplomacyFlags.DeclarationOfFriendship),
        diplomacy.getFlag(DiplomacyFlags.ResearchAgreement),
        diplomacy.getFlag(DiplomacyFlags.DefensivePact),
        diplomacy.hasOpenBorders,
        diplomacy.hasModifier(DiplomaticModifiers.EstablishedEmbassy) || diplomacy.hasModifier(DiplomaticModifiers.SharedEmbassies),
    )
}

internal fun bilateralConfirmation(a: Civilization, b: Civilization) = BilateralConfirmation(
    mapOf(a.civID to confirmedRelationship(a, b), b.civID to confirmedRelationship(b, a)),
)

/** Current player-bound state; no third-party partners or private research progress. */
internal fun bilateralStatus(a: Civilization, b: Civilization): JsonObject {
    val ours = confirmedRelationship(a, b)
    val theirs = confirmedRelationship(b, a)
    return obj(
        "source" to "engine", "atWar" to ours.atWar, "peaceTreatyTurns" to ours.peaceTreatyTurns,
        "friendshipTurns" to ours.friendshipTurns,
        "researchAgreementTurns" to ours.researchAgreementTurns,
        "defensivePactTurns" to ours.defensivePactTurns,
        "youMayEnterTheirTerritory" to ours.mayEnterOtherTerritory,
        "theyMayEnterYourTerritory" to theirs.mayEnterOtherTerritory,
        "yourEmbassyInTheirCapital" to ours.hasEmbassyInOtherCapital,
        "theirEmbassyInYourCapital" to theirs.hasEmbassyInOtherCapital,
    )
}

internal fun friendshipOption(a: Civilization, b: Civilization, relationReason: String?): JsonObject {
    val reason = relationReason ?: PlayerFriendshipOperations(a).stateReason(b)
    return obj(
        "name" to "Declaration of Friendship",
        "duration" to PlayerFriendshipOperations.DURATION,
        "available" to (reason == null),
        "unavailableReason" to reason,
    )
}

/** Explain omitted trade-screen choices without inspecting the opponent's hidden technologies. */
internal fun bilateralUnavailable(a: Civilization, b: Civilization, offered: List<TradeSelection>, reason: String?): List<JsonObject> {
    val special = listOf(
        TradeOffer(Constants.acceptEmbassy, TradeOfferType.Embassy, speed = a.gameInfo.speed),
        TradeOffer(Constants.openBorders, TradeOfferType.Agreement, speed = a.gameInfo.speed),
        TradeOffer(Constants.researchAgreement, TradeOfferType.Treaty, a.diplomacyFunctions.getResearchAgreementCost(b), a.gameInfo.speed),
        TradeOffer(Constants.defensivePact, TradeOfferType.Treaty, speed = a.gameInfo.speed),
    )
    val diplomacy = a.getDiplomacyManager(b)!!
    return special.filter { option -> offered.none { it.type == option.type && it.name == option.name } }.map { option ->
        val why = reason ?: when {
            a.isAtWarWith(b) -> "Requires peace."
            option.type == TradeOfferType.Embassy && (diplomacy.hasModifier(DiplomaticModifiers.ReceivedEmbassy) || diplomacy.hasModifier(DiplomaticModifiers.SharedEmbassies)) -> "This player already has an embassy in your capital."
            option.type == TradeOfferType.Embassy && a.getCapital() == null -> "No capital is available for an embassy."
            option.type == TradeOfferType.Embassy && (diplomacy.getFlag(DiplomacyFlags.Denunciation) == 30 || b.getDiplomacyManager(a)!!.getFlag(DiplomacyFlags.Denunciation) == 30) -> "Embassies cannot be established on the turn of a denunciation."
            option.type == TradeOfferType.Embassy -> "Requires embassy technology, a capital, no existing embassy in this direction, and no denunciation this turn."
            option.name == Constants.openBorders && b.getDiplomacyManager(a)!!.hasOpenBorders -> "You already grant this player Open Borders."
            !a.diplomacyFunctions.meetsEmbassyRequirementFor(b) -> "This ruleset requires mutual embassies first."
            option.name == Constants.openBorders -> "Both civilizations need technology enabling Open Borders."
            !diplomacy.hasFlag(DiplomacyFlags.DeclarationOfFriendship) -> "Requires an active Declaration of Friendship."
            option.name == Constants.researchAgreement && diplomacy.hasFlag(DiplomacyFlags.ResearchAgreement) -> "A research agreement is already active."
            option.name == Constants.defensivePact && diplomacy.hasFlag(DiplomacyFlags.DefensivePact) -> "A defensive pact is already active."
            option.name == Constants.researchAgreement -> "Both civilizations need technology enabling research agreements and remaining research."
            else -> "Both civilizations need technology enabling defensive pacts."
        }
        obj(
            "type" to option.type.name,
            "name" to option.name,
            "amount" to option.amount,
            "duration" to option.duration,
            "available" to false,
            "unavailableReason" to why,
        )
    } + if (offered.none { it.type == TradeOfferType.City }) {
        listOf(
            obj(
                "type" to "City",
                "available" to false,
                "unavailableReason" to (
                    reason ?: when {
                        a.isOneCityChallenger() || b.isOneCityChallenger() -> "One City Challenge prevents city trading."
                        a.cities.any { it.hasJustBeenConquered } || a.popupAlerts.any { it.type in setOf(AlertType.CityTraded, AlertType.CityConquered) } -> "A pending city disposition must be resolved before trading that city."
                        else -> "Only non-capital cities outside resistance can be traded."
                    }
                    ),
            ),
        )
    } else {
        emptyList()
    }
}
