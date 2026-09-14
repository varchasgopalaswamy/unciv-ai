package uncivai

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** Explicit direction expectations for a shared receipt, independent of its reader or author. */
internal fun checkTradeConfirmation(
    outcome: JsonObject,
    romeMayEnter: Boolean,
    greeceMayEnter: Boolean,
    romeHasEmbassy: Boolean,
    greeceHasEmbassy: Boolean,
) {
    val confirmation = outcome.getValue("confirmation").jsonObject
    check(confirmation.keys == setOf("source", "relationships"))
    check(confirmation.getValue("source").jsonPrimitive.content == "engine")
    val relationships = confirmation.getValue("relationships").jsonObject
    check(relationships.keys == setOf("Rome", "Greece"))
    fun relationship(civ: String, other: String, mayEnter: Boolean, hasEmbassy: Boolean) {
        val value = relationships.getValue(civ).jsonObject
        check(value.keys == setOf("otherCivilizationId", "atWar", "peaceTreatyTurns", "friendshipTurns", "researchAgreementTurns", "defensivePactTurns", "mayEnterOtherTerritory", "hasEmbassyInOtherCapital"))
        check(value.getValue("otherCivilizationId").jsonPrimitive.content == other)
        check(!value.getValue("atWar").jsonPrimitive.boolean)
        check(value.getValue("mayEnterOtherTerritory").jsonPrimitive.boolean == mayEnter)
        check(value.getValue("hasEmbassyInOtherCapital").jsonPrimitive.boolean == hasEmbassy)
    }
    relationship("Rome", "Greece", romeMayEnter, romeHasEmbassy)
    relationship("Greece", "Rome", greeceMayEnter, greeceHasEmbassy)
}
