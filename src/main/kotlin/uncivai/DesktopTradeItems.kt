package uncivai

import com.unciv.logic.trade.Trade
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import kotlinx.serialization.json.JsonObject

/** Native widgets use engine city IDs; the player protocol always uses issued trade references. */
internal class DesktopTradeItems(private val executor: GameExecutor) {
    fun native(item: JsonObject): TradeOffer = executor.accessGame {
        executor.communication.trades.engineOffer(
            TradeSelection(
                TradeOfferType.valueOf(checkNotNull(item.text("type"))),
                checkNotNull(item.text("name")),
                checkNotNull(item.number("amount")),
                checkNotNull(item.number("duration")),
            ),
        )
    }

    fun player(offer: TradeOffer): JsonObject = executor.accessGame {
        val name = if (offer.type == TradeOfferType.City) {
            requireNotNull(executor.communicationState.trades.cityHandles[offer.name]) { "This city is no longer available in the trade dialog." }
        } else {
            offer.name
        }
        obj("type" to offer.type.name, "name" to name, "amount" to offer.amount, "duration" to offer.duration)
    }

    fun nativeProposal(proposal: JsonObject, viewer: String) = Trade().apply {
        for (clause in proposal.array("clauses").filterIsInstance<JsonObject>()) {
            (if (clause.text("from") == viewer) ourOffers else theirOffers).add(native(clause["item"] as JsonObject))
        }
    }
}
