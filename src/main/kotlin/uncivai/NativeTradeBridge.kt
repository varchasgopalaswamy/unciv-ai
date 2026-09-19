package uncivai

import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.NotificationCategory
import com.unciv.logic.civilization.NotificationIcon
import com.unciv.logic.civilization.PopupAlert
import com.unciv.logic.trade.TradeLogic
import com.unciv.logic.trade.TradeOfferType
import com.unciv.logic.trade.TradeRequest
import kotlinx.serialization.json.JsonObject
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.gameHasResult
import java.util.UUID

/** Native requests are pending across turns, never owners of the shared gameplay pause.
 * The native AI runs its existing decision code. Its synchronous result observer records
 * the actual result in the same checkpoint as END_TURN, without rerunning AI on recovery.
 */
internal class NativeTradeBridge(private val runtime: GameExecutor, private val trades: EngineTrades) {
    private val state get() = trades.state
    private var synchronizing = false
    fun permitted(player: Civilization, other: Civilization) = player.isHuman() && player.isMajorCiv() &&
        player.civID !in runtime.communicationState.humanPlayers && other.isAI() && other.isMajorCiv() &&
        player.knows(other) && other.knows(player)

    private fun id() = "n" + UUID.randomUUID().toString().replace("-", "")
    private fun fail(reason: PlayerSessionFailure = PlayerSessionFailure.OPTION_UNAVAILABLE): Nothing = throw PlayerSessionException(reason)
    private fun active(player: Civilization) {
        runtime.requireRunning()
        if (player.gameInfo.currentPlayer != player.civID) fail(PlayerSessionFailure.NOT_YOUR_TURN)
        if (player.isDefeated() || gameHasResult(player.gameInfo)) fail()
    }

    fun propose(actor: String, request: CommunicationRequest, friendship: Boolean): JsonObject {
        val player = trades.player(actor)
        val other = trades.player(checkNotNull(request.partner))
        if (!permitted(player, other)) fail()
        active(player)
        val previous = request.proposal?.let { trades.visible(actor, it).last() }
        if (previous != null) {
            trades.requireVersion(previous, request)
            if (previous.mode != "NATIVE_AI" || previous.status != "PENDING" || previous.friendship != null ||
                previous.participants().toSet() != setOf(actor, other.civID) || previous.failure != null
            ) {
                fail()
            }
        } else if (request.version != null) {
            fail(PlayerSessionFailure.INVALID_ARGUMENT)
        }
        if (previous == null && state.proposals.values.any { it.last().let { p -> p.mode == "NATIVE_AI" && (p.friendship != null) == friendship && p.status == "PENDING" && p.participants().toSet() == setOf(actor, other.civID) } }) {
            return trades.unavailable("Resolve or withdraw the pending native request first.")
        }
        if (!friendship && request.give.isEmpty() && request.receive.isEmpty()) fail(PlayerSessionFailure.INVALID_ARGUMENT)
        for (items in listOf(request.give, request.receive)) {
            if (items.any { !supportedTradeTerm(it.type, it.name) } || items.map { it.type to it.name }.distinct().size != items.size) fail(PlayerSessionFailure.INVALID_ARGUMENT)
        }
        val requestId = id()
        val proposal = EngineTradeProposal(
            previous?.proposal ?: id(), (previous?.version ?: 0) + 1, actor, other.civID,
            if (friendship) emptyList() else trades.clauses(actor, other.civID, request.give, request.receive),
            0, Int.MAX_VALUE, player.gameInfo.turns, listOf(TradeConsent(actor, "accept", player.gameInfo.turns)),
            friendship = if (friendship) FriendshipTerms() else null, mode = "NATIVE_AI", nativeRequestId = requestId,
        )
        trades.legality(proposal)?.let { return trades.unavailable(it) }
        if (friendship && other.popupAlerts.any { it.type == AlertType.DeclarationOfFriendship && it.value == actor }) {
            return trades.unavailable("A native friendship request is already pending.")
        }
        // All rejection checks precede removing a prior request or publishing replacement terms.
        if (previous != null) {
            removeRequest(previous)
            trades.replace(previous.copy(status = "SUPERSEDED"), actor)
        }
        if (friendship) {
            other.popupAlerts.add(PopupAlert(AlertType.DeclarationOfFriendship, actor).also { it.requestId = requestId })
        } else {
            other.tradeRequests.add(TradeRequest(actor, trades.engineTrade(proposal).reverse()).also { it.requestId = requestId })
            player.cache.updateCivResources()
        }
        state.proposals.getOrPut(proposal.proposal) { mutableListOf() }.add(proposal)
        trades.publish(proposal, actor)
        runtime.turnSessions[actor]?.invalidate()
        synchronize(player.gameInfo)
        return obj("trade" to view(proposal, actor))
    }

    fun decide(actor: String, request: CommunicationRequest, proposal: EngineTradeProposal): JsonObject {
        if (request.decision == "accept" && proposal.status == "ENACTED") return obj("trade" to view(proposal, actor), "outcome" to proposal.outcome?.recordJson())
        val player = trades.player(actor)
        val other = trades.player(proposal.participants().single { it != actor })
        if (!permitted(player, other)) fail()
        active(player)
        if (proposal.status != "PENDING") return trades.unavailable("This native request is ${proposal.status.lowercase()}.", proposal, actor)
        if (request.decision == "withdraw") {
            if (proposal.author != actor) fail()
            removeRequest(proposal)
            trades.replace(proposal.copy(status = "WITHDRAWN"), actor)
        } else {
            if (proposal.author == actor || request.decision !in setOf("accept", "decline")) fail()
            val native = player.tradeRequests.singleOrNull { it.requestId == proposal.nativeRequestId } ?: fail()
            if (request.decision == "accept") {
                trades.legality(proposal)?.let { return trades.unavailable(it, proposal, actor) }
                if (!native.trade.equalTrade(trades.engineTrade(proposal).reverse())) return trades.unavailable("The native terms changed; inspect the new request.", proposal, actor)
                player.tradeRequests.remove(native)
                val logic = TradeLogic(player, other)
                logic.currentTrade.set(native.trade)
                logic.acceptTrade()
                other.addNotification("[${player.civName}] has accepted your trade request", NotificationCategory.Trade, player.civName, NotificationIcon.Trade)
                resolved(proposal, "ENACTED", null)
            } else {
                player.tradeRequests.remove(native)
                native.decline(player)
                other.addNotification("[${player.civName}] has denied your trade request", NotificationCategory.Trade, player.civName, NotificationIcon.Trade)
                trades.replace(proposal.copy(status = "DECLINED", consents = proposal.consents + TradeConsent(actor, "decline", player.gameInfo.turns)), actor)
            }
        }
        player.cache.updateCivResources()
        proposal.participants().forEach { runtime.turnSessions[it]?.invalidate() }
        val final = state.proposals.getValue(proposal.proposal).last()
        return obj("trade" to view(final, actor), "outcome" to final.outcome?.recordJson())
    }

    private fun removeRequest(proposal: EngineTradeProposal) {
        for (civilization in proposal.participants().map(trades::player)) {
            civilization.tradeRequests.removeAll { it.requestId == proposal.nativeRequestId }
            civilization.popupAlerts.removeAll { it.requestId == proposal.nativeRequestId }
            civilization.cache.updateCivResources()
        }
    }

    fun view(proposal: EngineTradeProposal, reader: String): JsonObject {
        val player = trades.player(reader)
        val incoming = proposal.author != reader
        val onTurn = player.gameInfo.currentPlayer == reader && !runtime.isPaused() && state.activeProposal == null
        val reason = when {
            proposal.status != "PENDING" -> proposal.failure ?: "This request is ${proposal.status.lowercase()}."
            !incoming -> "Awaiting the built-in AI's native turn."
            !onTurn -> "Respond on your turn."
            else -> trades.legality(proposal)
        }
        return JsonObject(
            proposal.recordJson() + obj(
                "acceptAvailable" to (incoming && reason == null), "unavailableReason" to reason,
                "declineAvailable" to (incoming && onTurn && proposal.status == "PENDING"),
                "withdrawAvailable" to (!incoming && onTurn && proposal.status == "PENDING"),
                "counterofferAvailable" to (onTurn && proposal.status == "PENDING" && proposal.friendship == null && proposal.failure == null),
                "counteroffersRemaining" to null, "deadlineMillis" to null,
                "awaitingNativeTurn" to (!incoming && proposal.status == "PENDING"), "pausesGameplay" to false,
                "yourWarDeclarations" to trades.warClauses(proposal).filter { it.from == reader }.map {
                    obj("targetCivilization" to it.item.name, "confirmation" to com.unciv.logic.civilization.PlayerDiplomacyOperations(player).declarationConfirmation(trades.player(it.item.name)).orEmpty())
                },
                "socialAdvisories" to kotlinx.serialization.json.Json.encodeToJsonElement(
                    kotlinx.serialization.builtins.ListSerializer(SocialPromiseAdvisory.serializer()),
                    trades.warClauses(proposal).flatMap { runtime.communication.peaceAdvisories(reader, it.item.name) }.distinctBy { it.proposal to it.version },
                ),
            ),
        )
    }

    fun handles(player: Civilization, request: TradeRequest): Boolean = state.proposals.values.any {
        it.last().let { p -> p.mode == "NATIVE_AI" && p.status == "PENDING" && p.partner == player.civID && p.nativeRequestId == request.requestId }
    }

    /** Refresh request identities and transient observers after load and every native turn. */
    fun synchronize(game: GameInfo): Boolean {
        if (synchronizing) return false
        synchronizing = true
        var changed = false
        try {
            for (player in game.civilizations.filter { it.isHuman() && it.isMajorCiv() && it.civID !in runtime.communicationState.humanPlayers }) {
                for (request in player.tradeRequests.toList()) {
                    val sender = game.civilizations.firstOrNull { it.civID == request.requestingCiv } ?: continue
                    if (!permitted(player, sender)) continue
                    if (state.proposals.values.any { versions -> versions.any { it.nativeRequestId != null && it.nativeRequestId == request.requestId } }) continue
                    importRequest(player, sender, request)
                    changed = true
                }
            }
            for (proposal in state.proposals.values.map { it.last() }.filter { it.mode == "NATIVE_AI" && it.status == "PENDING" }) {
                val recipient = trades.player(proposal.partner)
                if (proposal.friendship != null) {
                    val alert = recipient.popupAlerts.firstOrNull { it.requestId == proposal.nativeRequestId }
                    if (alert != null) {
                        alert.onResponse = { accepted -> resolved(proposal, if (accepted) "ENACTED" else "DECLINED", null) }
                    } else {
                        trades.replace(proposal.copy(status = "INVALID", failure = "The native friendship request is no longer pending."), proposal.partner)
                        changed = true
                    }
                } else {
                    val request = recipient.tradeRequests.firstOrNull { it.requestId == proposal.nativeRequestId }
                    if (request != null) {
                        request.onResponse = { status, counter -> resolved(proposal, status, counter) }
                    } else {
                        trades.replace(proposal.copy(status = "INVALID", failure = "The native trade request is no longer pending."), proposal.partner)
                        changed = true
                    }
                }
            }
        } finally {
            synchronizing = false
        }
        return changed
    }

    private fun importRequest(player: Civilization, sender: Civilization, request: TradeRequest, previous: EngineTradeProposal? = null) {
        if (request.requestId.isEmpty()) request.requestId = id()
        val safe = (request.trade.ourOffers + request.trade.theirOffers).all { item ->
            supportedTradeTerm(item.type, item.name) && when (item.type) {
                TradeOfferType.WarDeclaration, TradeOfferType.PeaceProposal -> player.getKnownCivs().any { it.isMajorCiv() && it.civID == item.name }
                TradeOfferType.City -> (player.cities + sender.cities).any { it.id == item.name }
                else -> true
            }
        }
        val proposal = EngineTradeProposal(
            previous?.proposal ?: id(), (previous?.version ?: 0) + 1, sender.civID, player.civID,
            if (safe) trades.clauses(sender.civID, player.civID, request.trade.theirOffers.map(trades::selection), request.trade.ourOffers.map(trades::selection)) else emptyList(),
            0, Int.MAX_VALUE, player.gameInfo.turns, listOf(TradeConsent(sender.civID, "accept", player.gameInfo.turns)),
            failure = if (safe) null else "This native offer contains terms outside your permitted information or supported trade categories; it can be declined.",
            mode = "NATIVE_AI", nativeRequestId = request.requestId,
        )
        state.proposals.getOrPut(proposal.proposal) { mutableListOf() }.add(proposal)
        trades.publish(proposal, sender.civID)
    }

    private fun resolved(proposal: EngineTradeProposal, status: String, counter: TradeRequest?) {
        if (state.proposals.getValue(proposal.proposal).last().let { it.version != proposal.version || it.status != "PENDING" }) return
        val a = trades.player(proposal.author)
        val b = trades.player(proposal.partner)
        val outcome = if (status == "ENACTED") {
            EngineOutcome(
                action = if (proposal.friendship != null) "DECLARE_FRIENDSHIP" else "ENACT_TRADE", author = b.civID,
                participants = proposal.participants(), turn = a.gameInfo.turns, confirmation = bilateralConfirmation(a, b),
                proposal = proposal.proposal, version = proposal.version, clauses = proposal.clauses, friendship = proposal.friendship,
                cities = proposal.clauses.filter { it.item.type == TradeOfferType.City }.map { TradedCityOwner(it.item.name, it.to) },
                wars = trades.warClauses(proposal).map { ConfirmedTradeWar(it.from, it.item.name, trades.player(it.from).isAtWarWith(trades.player(it.item.name))) },
            )
        } else {
            null
        }
        trades.replace(
            proposal.copy(
                status = status,
                outcome = outcome,
                consents = proposal.consents + TradeConsent(b.civID, if (status == "ENACTED") "accept" else status.lowercase(), a.gameInfo.turns),
            ),
            b.civID,
        )
        if (outcome != null) {
            runtime.communicationState.outcomes.add(outcome)
            runtime.communication.emit(proposal.participants(), "engine", outcome.recordJson(), proposal.participants().toSet())
            for (war in outcome.wars) runtime.communication.diplomacyEnacted(war.declaringCivilization, war.targetCivilization, "DECLARE_WAR", a.gameInfo.turns)
        }
        if (counter != null) importRequest(a, b, counter, proposal)
        proposal.participants().forEach { runtime.turnSessions[it]?.invalidate() }
    }
}
