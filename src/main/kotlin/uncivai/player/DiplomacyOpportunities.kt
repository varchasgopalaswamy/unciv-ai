package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.trade.TradeLogic
import com.unciv.logic.trade.TradeOfferType
import kotlinx.serialization.Serializable
import uncivai.GameExecutor
import java.util.Collections

@Serializable
data class LuxuryQuantity(val name: String, val amount: Int)

@Serializable
data class TradePartnerOpportunity(
    val civilizationId: String,
    val mode: String,
    val missingLuxuriesOffered: List<LuxuryQuantity>,
    val spareLuxuriesToOffer: List<LuxuryQuantity>,
    val requiresPeaceTreaty: Boolean,
)

@Serializable
data class ExpiringTradeTerm(val civilizationId: String, val agreement: String?, val direction: String, val type: String, val name: String?, val amount: Int, val turnsRemaining: Int)

@Serializable
data class DiplomacyOpportunities(val luxuryTrades: List<TradePartnerOpportunity>, val expiringTerms: List<ExpiringTradeTerm>)

/** Uses precisely the permitted trade-menu inventories, without accepting or proposing terms. */
internal fun observeDiplomacyOpportunities(player: Civilization, runtime: GameExecutor, relations: List<KnownCivilization>): DiplomacyOpportunities {
    val trades = runtime.communication.trades
    val partners = player.getKnownCivs().filter { it.isMajorCiv() && trades.known(player, it) }.sortedBy { it.civID }.mapNotNull { other ->
        if (trades.relationReason(player, other) != null || (other.isAI() && !trades.nativeBridge.permitted(player, other))) return@mapNotNull null
        val logic = TradeLogic(player, other)
        val give = trades.available(logic, player).filter { it.type == TradeOfferType.Luxury_Resource }.mapNotNull {
            val spare = minOf(it.amount, player.getResourceAmount(it.name) - 1)
            if (spare > 0) LuxuryQuantity(it.name, spare) else null
        }.sortedBy { it.name }
        val receive = trades.available(logic, other).filter {
            it.type == TradeOfferType.Luxury_Resource && it.amount > 0 && player.getResourceAmount(it.name) <= 0
        }.map { LuxuryQuantity(it.name, it.amount) }.sortedBy { it.name }
        if (give.isEmpty() && receive.isEmpty()) return@mapNotNull null
        TradePartnerOpportunity(other.civID, if (other.isAI()) "NATIVE_AI" else "SHARED", receive, give, player.isAtWarWith(other))
    }.toList()
    val expiring = relations.flatMap { relation ->
        relation.agreements.flatMap { agreement ->
            listOf("give" to agreement.give, "receive" to agreement.receive).flatMap { (direction, terms) ->
                terms.filter { it.supported && it.duration in 1..5 }.map {
                    ExpiringTradeTerm(relation.civilizationId, agreement.agreement, direction, it.type, it.name, it.amount, it.duration)
                }
            }
        }
    }
    return DiplomacyOpportunities(Collections.unmodifiableList(partners), Collections.unmodifiableList(expiring))
}
