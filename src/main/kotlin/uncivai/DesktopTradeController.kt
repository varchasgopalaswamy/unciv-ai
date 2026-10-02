package uncivai

import com.unciv.Constants
import com.unciv.logic.trade.Trade
import com.unciv.logic.trade.TradeOfferType
import com.unciv.logic.trade.TradeOperations
import com.unciv.ui.popups.ConfirmPopup
import com.unciv.ui.screens.diplomacyscreen.DiplomacyScreen
import com.unciv.ui.screens.diplomacyscreen.TradeTable
import com.unciv.ui.screens.diplomacyscreen.TradeTableAction
import com.unciv.ui.screens.diplomacyscreen.TradeTableController
import com.unciv.view.CivView
import com.unciv.view.ForeignCivView
import kotlinx.serialization.json.JsonObject
import java.util.UUID

internal data class DesktopTradeDraft(val epoch: String, val proposal: String?, val version: Int?, val trade: Trade)

/** Supplies shared proposal/consent actions to Unciv's ordinary trade table. */
internal class DesktopTradeController(
    private val session: DesktopSession,
    viewer: CivView,
    partnerView: ForeignCivView,
    val screen: DiplomacyScreen,
    private var savedDraft: DesktopTradeDraft? = null,
) : TradeTableController {
    val partner = partnerView.getCiv().civID
    private val epoch = session.epoch
    override val tradeView = viewer.getTradeView(partnerView)
    lateinit var table: TradeTable
    var changed = true
    private val items = DesktopTradeItems(session.executor)
    private var options = obj()
    private var communicationAvailable = false
    private var proposal: JsonObject? = null
    private var editing = true
    private var initialized = false
    private var notice = ""

    init {
        refresh()
    }

    private fun identity(value: JsonObject?) = value?.let { it.text("proposal") to it.number("version") }
    override val canEdit get() = communicationAvailable && editing &&
        (proposal?.flag("counterofferAvailable") ?: (options.flag("available") && options.flag("canInitiate")))
    override val message get() = listOfNotNull(
        session.message(),
        notice.takeIf { it.isNotBlank() },
        proposal?.let { "${it.text("author")} → ${it.text("partner")} · ${it.text("status")} · version ${it.number("version")}" },
        (proposal?.get("friendship") as? JsonObject)?.let { "Declaration of Friendship · ${it.number("duration")} turns" },
        (proposal ?: options).text("unavailableReason"),
    ).joinToString("\n")

    private fun request(op: String, vararg fields: Pair<String, Any?>, mutation: Boolean = false): JsonObject {
        val request = obj("op" to op, "epoch" to epoch, *fields)
        val result = session.social(if (mutation) JsonObject(request + obj("requestId" to "human-" + UUID.randomUUID())) else request)
        require(result["error"] == null) { result.text("reason") ?: "These terms are no longer available. Review the updated offer." }
        return result
    }

    private fun act(clearNotice: Boolean = true, block: () -> Unit) {
        try {
            block()
            if (clearNotice) notice = ""
        } catch (failure: uncivai.player.PlayerSessionException) {
            notice = "The proposal or position changed, or communication is paused. Review the current terms before deciding."
        } catch (failure: IllegalArgumentException) {
            notice = failure.message ?: "These terms are unavailable."
        }
        changed = true
    }

    override fun refresh(): Boolean {
        if (!changed) return false
        communicationAvailable = false
        act(clearNotice = false) {
            val state = request("state")
            val canCommunicate = (state["context"] as JsonObject).flag("communicationAvailable")
            options = request("trade_options", "partner" to partner)
            val pending = state.array("trades").filterIsInstance<JsonObject>().lastOrNull {
                it.text("status") == "PENDING" && partner in listOf(it.text("author"), it.text("partner"))
            }
            val reference = pending?.text("proposal") ?: proposal?.text("proposal")
            val next = reference?.let { request("trade_read", "proposal" to it)["trade"] as JsonObject }
            if (!initialized || identity(next) != identity(proposal) || next?.text("status") != proposal?.text("status")) {
                proposal = next
                editing = next == null
                resetTerms()
            }
            proposal = next
            for ((side, offers) in listOf("give" to tradeView.ourAvailableOffers(), "receive" to tradeView.theirAvailableOffers())) {
                offers.clear()
                options.array(side).filterIsInstance<JsonObject>().mapTo(offers, items::native)
            }
            savedDraft?.let { saved ->
                if (saved.epoch == epoch && saved.proposal == proposal?.text("proposal") && saved.version == proposal?.number("version")) {
                    tradeView.setStagedTrade(TradeOperations.copy(saved.trade))
                    editing = true
                }
            }
            savedDraft = null
            initialized = true
            communicationAvailable = canCommunicate
        }
        changed = false
        return true
    }

    private fun resetTerms() {
        val current = proposal
        tradeView.setStagedTrade(
            if (current != null) {
                items.nativeProposal(current, session.viewingCivilization)
            } else {
                Trade().apply {
                    if (options.flag("requiresPeaceTreaty")) {
                        options.array("give").filterIsInstance<JsonObject>().firstOrNull { it.text("name") == Constants.peaceTreaty }?.let { ourOffers.add(items.native(it)) }
                        options.array("receive").filterIsInstance<JsonObject>().firstOrNull { it.text("name") == Constants.peaceTreaty }?.let { theirOffers.add(items.native(it)) }
                    }
                }
            },
        )
    }

    fun draft(): DesktopTradeDraft? = if (!editing) null else DesktopTradeDraft(epoch, proposal?.text("proposal"), proposal?.number("version"), staged())
    private fun staged() = TradeOperations.draft(tradeView.ourStagedOffers(), tradeView.theirStagedOffers())

    override fun draftChanged() {
        notice = ""
    }

    private fun terms(trade: Trade) = "You give:\n" + trade.ourOffers.joinToString("\n") { it.getOfferText() }.ifEmpty { "Nothing" } +
        "\nYou receive:\n" + trade.theirOffers.joinToString("\n") { it.getOfferText() }.ifEmpty { "Nothing" }

    private fun publish() = act {
        require(canEdit)
        val prepared = request(
            "trade_prepare",
            "partner" to partner,
            "give" to tradeView.ourStagedOffers().map(items::player),
            "receive" to tradeView.theirStagedOffers().map(items::player),
        )
        require(prepared.flag("valid")) { prepared.text("unavailableReason") ?: "These terms are unavailable." }
        val draft = items.nativeProposal(prepared, session.viewingCivilization)
        tradeView.setStagedTrade(draft)
        val fields = mutableListOf<Pair<String, Any?>>(
            "partner" to partner,
            "give" to draft.ourOffers.map(items::player),
            "receive" to draft.theirOffers.map(items::player),
        )
        val current = proposal
        if (current != null) fields.addAll(listOf("proposal" to current.text("proposal"), "version" to current.number("version")))
        val warnings = options.array("give").filterIsInstance<JsonObject>().filter { option ->
            draft.ourOffers.any { it.type == TradeOfferType.WarDeclaration && it.name == option.text("name") }
        }.flatMap { option ->
            val war = option["war"] as? JsonObject
            war?.array("yourDeclarationConfirmation").orEmpty().map { (it as kotlinx.serialization.json.JsonPrimitive).content } +
                war?.array("socialAdvisories").orEmpty().filterIsInstance<JsonObject>().mapNotNull { it.text("terms") }
        }
        ConfirmPopup(screen, (listOf("Publish these exact terms?", terms(draft)) + warnings).joinToString("\n"), "Publish") {
            act {
                request("trade_offer", *fields.toTypedArray(), mutation = true)
                editing = false
            }
        }.open()
    }

    private fun decide(current: JsonObject, decision: String) = act {
        request("trade_decide", "proposal" to current.text("proposal"), "version" to current.number("version"), "decision" to decision, mutation = true)
    }

    override fun actions(): List<TradeTableAction> = buildList {
        fun action(label: String, enabled: Boolean = true, block: () -> Unit) {
            add(TradeTableAction(label, enabled && communicationAvailable, block))
        }
        val current = proposal
        if (editing) {
            action(if (current == null) "Offer trade" else "Publish counteroffer", canEdit && (tradeView.ourStagedOffers().isNotEmpty() || tradeView.theirStagedOffers().isNotEmpty())) { publish() }
            if (current != null) {
                action("Cancel editing") {
                    editing = false
                    resetTerms()
                    changed = true
                }
            }
        } else if (current != null && current.text("status") == "PENDING") {
            if (current.flag("acceptAvailable")) {
                action("Accept") {
                    val warnings = current.array("yourWarDeclarations").filterIsInstance<JsonObject>().flatMap { it.array("confirmation") }.map { (it as kotlinx.serialization.json.JsonPrimitive).content } +
                        current.array("socialAdvisories").filterIsInstance<JsonObject>().mapNotNull { it.text("terms") }
                    val description = if (current["friendship"] is JsonObject) "Declaration of Friendship · ${(current["friendship"] as JsonObject).number("duration")} turns" else terms(items.nativeProposal(current, session.viewingCivilization))
                    ConfirmPopup(screen, (listOf("Accept these exact terms?", description) + warnings).joinToString("\n"), "Accept") { decide(current, "accept") }.open()
                }
            }
            if (current.flag("declineAvailable")) action("Decline") { decide(current, "decline") }
            if (current.flag("withdrawAvailable")) action("Withdraw") { decide(current, "withdraw") }
            if (current.flag("counterofferAvailable")) {
                action("Counteroffer") {
                    editing = true
                    changed = true
                }
            }
        } else {
            action("New proposal", options.flag("canInitiate") && options.flag("available")) {
                proposal = null
                editing = true
                resetTerms()
                changed = true
            }
        }
        add(TradeTableAction("Chat") { session.openDiplomacy(partner) })
    }
}
