package uncivai

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerFriendshipOperations
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.civilization.diplomacy.DiplomaticModifiers
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

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

/** Presentation of native reasons; no item eligibility is implemented here. */
internal fun bilateralUnavailable(a: Civilization, b: Civilization, reason: String?): List<JsonObject> = com.unciv.logic.trade.unavailableTradeItems(a, b).map { option ->
    obj(
        "type" to option.type.name,
        "name" to option.name,
        "amount" to option.amount,
        "duration" to option.duration,
        "available" to false,
        "unavailableReason" to (reason ?: option.reason),
    )
}
