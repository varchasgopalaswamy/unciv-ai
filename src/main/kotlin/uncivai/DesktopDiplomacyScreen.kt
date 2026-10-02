package uncivai

import com.badlogic.gdx.scenes.scene2d.Actor
import com.badlogic.gdx.scenes.scene2d.ui.Cell
import com.badlogic.gdx.scenes.scene2d.ui.Label
import com.badlogic.gdx.scenes.scene2d.ui.ScrollPane
import com.badlogic.gdx.scenes.scene2d.ui.SelectBox
import com.badlogic.gdx.scenes.scene2d.ui.Table
import com.badlogic.gdx.scenes.scene2d.ui.TextArea
import com.badlogic.gdx.scenes.scene2d.ui.TextField
import com.badlogic.gdx.scenes.scene2d.utils.ChangeListener
import com.unciv.ui.components.extensions.isEnabled
import com.unciv.ui.components.extensions.toTextButton
import com.unciv.ui.components.input.onClick
import com.unciv.ui.popups.ConfirmPopup
import com.unciv.ui.screens.basescreen.BaseScreen
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.intOrNull
import java.util.UUID

/** Local, unsent input belongs to its conversation and is discarded on a turn rewind. */
internal class DesktopDiplomacyDrafts {
    var selectedCivilization: String? = null
    var tab = "Chat"
    val conversations = mutableMapOf<String?, DesktopConversationDraft>()
}

internal data class DesktopConversationDraft(
    val message: String,
    val cursor: Int,
    val promise: String,
    val conditions: String,
    val expiry: String,
    val promiseKind: String,
    val revisingPromise: JsonObject?,
)

/** Ordinary widgets over the human's existing player communication session. */
internal class DesktopDiplomacyScreen(
    private val session: DesktopSession,
    initialCivilization: String?,
) : BaseScreen() {
    private val openedEpoch = session.epoch
    internal val isCurrent get() = openedEpoch == session.epoch
    var selectedCivilization: String? = initialCivilization ?: session.diplomacyDrafts.selectedCivilization
        private set
    var changed = true
    private var unreadDirty = true
    private var state = JsonObject(emptyMap())
    private val root = Table()
    private val content = Table()
    private val status = Label("", skin)
    private val contacts = SelectBox<String>(skin).apply { name = "contacts" }
    private var updatingContacts = false
    private val message = TextArea("", skin).apply { name = "chat-message" }
    private val composer = Table()
    private val composerCell: Cell<Table>
    private var tab = session.diplomacyDrafts.tab
    private var cursor = 0
    private val events = ArrayList<JsonObject>()
    private var notice = ""
    private val conditions = TextField("", skin)
    private val promise = TextArea("", skin)
    private var revisingPromise: JsonObject? = null
    private val expiry = TextField("", skin).apply {
        messageText = "Expires on turn (optional)"
        textFieldFilter = TextField.TextFieldFilter.DigitsOnlyFilter()
    }
    private val promiseKind = SelectBox<String>(skin).apply {
        setItems("other", "peace")
    }

    internal fun refreshed(): DesktopDiplomacyScreen {
        saveDraft()
        return DesktopDiplomacyScreen(session, selectedCivilization)
    }

    private fun saveDraft() {
        if (!isCurrent) return
        session.diplomacyDrafts.apply {
            selectedCivilization = this@DesktopDiplomacyScreen.selectedCivilization
            tab = this@DesktopDiplomacyScreen.tab
            conversations[selectedCivilization] = DesktopConversationDraft(
                message.text, message.cursorPosition, promise.text, conditions.text,
                expiry.text, promiseKind.selected, revisingPromise,
            )
        }
    }

    private fun restoreDraft() {
        val draft = session.diplomacyDrafts.conversations[selectedCivilization]
        message.text = draft?.message ?: ""
        message.cursorPosition = draft?.cursor ?: 0
        promise.text = draft?.promise ?: ""
        conditions.text = draft?.conditions ?: ""
        expiry.text = draft?.expiry ?: ""
        promiseKind.selected = draft?.promiseKind ?: "other"
        revisingPromise = draft?.revisingPromise
    }

    override fun hide() {
        saveDraft()
        super.hide()
    }

    init {
        restoreDraft()
        root.setFillParent(true)
        root.defaults().pad(8f)
        root.add(Label("Diplomacy · ${session.viewingCivilization}", skin)).left().growX()
        root.add("Back to map".toTextButton().apply { onClick { game.popScreen() } }).right().row()
        status.setWrap(true)
        root.add(status).colspan(2).growX().row()
        root.add(contacts).colspan(2).growX().row()
        val tabs = Table()
        for (name in listOf("Chat", "Trades", "Promises", "Agreements")) {
            tabs.add(
                name.toTextButton().apply {
                    onClick {
                        tab = name
                        rebuild()
                    }
                },
            ).pad(5f)
        }
        root.add(tabs).colspan(2).growX().row()
        root.add(
            ScrollPane(content, skin).apply {
                setFadeScrollBars(false)
                setScrollingDisabled(true, false)
            },
        ).colspan(2).grow().row()
        composer.add(message).growX().height(90f)
        composer.add("Send message".toTextButton().apply { onClick { sendMessage() } }).padLeft(10f)
        composerCell = root.add(composer).colspan(2).growX()
        stage.addActor(root)
        contacts.addListener(object : ChangeListener() {
            override fun changed(event: ChangeEvent?, actor: Actor?) {
                if (updatingContacts) return
                saveDraft()
                selectedCivilization = contacts.selected.takeUnless { it == "Public" }
                restoreDraft()
                rebuild()
            }
        })
        refresh()
    }

    private fun request(op: String, vararg fields: Pair<String, Any?>, mutation: Boolean = false): JsonObject {
        val base = obj("op" to op, "epoch" to session.epoch, *fields)
        val result = session.social(if (mutation) JsonObject(base + obj("requestId" to "human-" + UUID.randomUUID())) else base)
        if (result["error"] != null) throw IllegalArgumentException(result.text("reason") ?: "That choice is no longer available. Refresh and try again.")
        return result
    }

    private fun act(clearNotice: Boolean = true, block: () -> Unit) {
        try {
            block()
            if (clearNotice) notice = ""
            changed = true
        } catch (failure: uncivai.player.PlayerSessionException) {
            notice = "That choice is no longer available, or communication is paused. Refresh and try again."
            changed = true
        } catch (failure: IllegalArgumentException) {
            notice = failure.message ?: "Check the entered terms."
        }
    }

    private fun refresh() {
        changed = false
        act(clearNotice = false) {
            state = request("state")
            val names = listOf("Public") + state.array("channels").mapNotNull { (it as? JsonPrimitive)?.content }
                .filter { it != session.viewingCivilization }
            val previous = selectedCivilization ?: "Public"
            if (contacts.items.toList() != names) {
                updatingContacts = true
                try {
                    contacts.setItems(*names.toTypedArray())
                    contacts.selected = previous.takeIf { it in names } ?: "Public"
                } finally {
                    updatingContacts = false
                }
                val selected = contacts.selected.takeUnless { it == "Public" }
                if (selected != selectedCivilization) {
                    saveDraft()
                    selectedCivilization = selected
                    restoreDraft()
                }
            }
            val page = request("read", "cursor" to cursor, "limit" to 100)
            events.addAll(page.array("events").map { it as JsonObject })
            cursor = page.number("nextCursor") ?: cursor
            rebuild()
        }
        changed = false
    }

    private fun line(value: String, table: Table = content) {
        table.add(Label(value, skin).apply { setWrap(true) }).width(stage.width - 90f).left().pad(8f).row()
    }

    private fun button(text: String, enabled: Boolean = true, action: () -> Unit) {
        content.add(
            text.toTextButton().apply {
                isEnabled = enabled
                onClick { act(block = action) }
            },
        ).left().pad(5f).row()
    }

    private val communicationAvailable get() = (state["context"] as? JsonObject)?.flag("communicationAvailable") == true
    private val canQueueMessage get() = (state["context"] as? JsonObject)?.flag("canQueueMessage") == true
    private fun decision(text: String, enabled: Boolean = true, action: () -> Unit) = button(text, enabled && communicationAvailable, action)

    private fun rebuild() {
        composer.isVisible = tab == "Chat"
        composerCell.height(if (composer.isVisible) 90f else 0f)
        content.clear()
        content.top().left()
        when (tab) {
            "Chat" -> chat()
            "Trades" -> trades()
            "Promises" -> promises()
            "Agreements" -> agreements()
        }
        unreadDirty = true
        if (session.rewindNotice != null) button("Acknowledge rewind") { session.rewindNotice = null }
    }

    private fun markVisibleRead() {
        session.markRead(
            events.filter { event ->
                val body = event["body"] as JsonObject
                val matchesTab = when (tab) {
                    "Chat" -> event.text("kind") == "message"
                    "Trades" -> event.text("kind") == "trade"
                    "Promises" -> event.text("kind") in setOf("offer", "consent")
                    "Agreements" -> event.text("kind") == "agreement"
                    else -> false
                }
                matchesTab && if (tab == "Chat" && selectedCivilization == null) {
                    body.flag("public")
                } else {
                    selectedCivilization == null || (
                        !body.flag("public") &&
                            (body.text("partner") == selectedCivilization || body.array("participants").any { (it as? JsonPrimitive)?.content == selectedCivilization })
                        )
                }
            },
        )
        unreadDirty = false
    }

    private fun sendMessage() = act {
        require(communicationAvailable || canQueueMessage) { "Communication is paused. You can send this message when play resumes." }
        require(message.text.isNotBlank()) { "Write a message first." }
        val fields = mutableListOf<Pair<String, Any?>>("text" to message.text)
        selectedCivilization?.let { fields.add("to" to it) }
        if (selectedCivilization == null) fields.add("mentions" to contacts.items.toList().filter { it != "Public" })
        request("send", *fields.toTypedArray(), mutation = true)
        message.text = ""
    }

    private fun chat() {
        val visible = events.filter { event ->
            if (event.text("kind") != "message") return@filter false
            val body = event["body"] as JsonObject
            if (selectedCivilization == null) {
                body.flag("public")
            } else {
                !body.flag("public") && body.array("participants").any { (it as? JsonPrimitive)?.content == selectedCivilization }
            }
        }
        val queued = state.array("queuedMessages").filterIsInstance<JsonObject>().filter { body ->
            if (selectedCivilization == null) body.flag("public")
            else !body.flag("public") && body.array("participants").any { (it as? JsonPrimitive)?.content == selectedCivilization }
        }
        if (visible.isEmpty() && queued.isEmpty()) line("No messages in this conversation.")
        visible.forEach { event ->
            val body = event["body"] as JsonObject
            line("Turn ${body.number("turn")} · ${body.text("author")}\n${body.text("text")}")
        }
        queued.forEach { body ->
            line("Queued · Will be delivered when strategic reviews finish\n${body.text("text")}")
        }
        button("Load more history") { refresh() }
    }

    private fun clauses(trade: JsonObject): String {
        val friendship = trade["friendship"] as? JsonObject
        if (friendship != null) return "Declaration of Friendship · ${friendship.number("duration")} turns"
        return trade.array("clauses").joinToString("\n") { value ->
            val clause = value as JsonObject
            val item = clause["item"] as JsonObject
            val duration = item.number("duration") ?: 0
            "${clause.text("from")} → ${clause.text("to")}: ${item.number("amount")} ${itemName(JsonObject(item + (clause["city"]?.let { mapOf("city" to it) } ?: emptyMap())))}" +
                if (duration > 0) " for $duration turns" else ""
        }
    }

    private fun trades() {
        val partner = selectedCivilization
        val met = (state["context"] as? JsonObject)?.array("knownCivilizations")?.any { (it as? JsonObject)?.text("civilizationId") == partner } == true
        if (partner != null && met) {
            button("Open trade") { session.openTrade(partner) }
            val options = request("trade_options", "partner" to partner)
            val friendship = options["friendship"] as? JsonObject
            decision("Offer friendship", options.flag("canInitiate") && friendship?.flag("available") == true) {
                request("friendship_offer", "partner" to partner, mutation = true)
            }
        } else {
            line(if (partner == null) "Select a civilization to make an offer." else "Meet this civilization in game before trading.")
        }
        for (summary in state.array("trades").filterIsInstance<JsonObject>().asReversed()) {
            if (partner != null && summary.text("author") != partner && summary.text("partner") != partner) continue
            val trade = request("trade_read", "proposal" to summary.text("proposal"))["trade"] as JsonObject
            line("${trade.text("author")} ↔ ${trade.text("partner")} · ${trade.text("status")} · version ${trade.number("version")}\n${clauses(trade)}")
            if (trade.text("status") == "PENDING") {
                line("Waiting for a decision; no response deadline.")
                val other = listOfNotNull(trade.text("author"), trade.text("partner")).first { it != session.viewingCivilization }
                button("Review trade with $other") { session.openTrade(other) }
            }
        }
    }

    private fun itemName(item: JsonObject): String = when (item.text("type")) {
        "City" -> (item["city"] as? JsonObject)?.let { "${it.text("name")} (population ${it.number("population")})" } ?: "City"
        "WarDeclaration" -> "Declare war on ${item.text("name")}"
        else -> item.text("name") ?: ""
    }

    private fun describeItems(items: Collection<JsonObject>) = items.joinToString("\n") {
        "${it.number("amount")} ${itemName(it)}" + (it.number("duration")?.takeIf { duration -> duration > 0 }?.let { duration -> " for $duration turns" } ?: "")
    }.ifEmpty { "Nothing" }

    private fun promises() {
        line("Social promises record stated intent. Players interpret them; they do not restrict game actions.")
        val partner = selectedCivilization
        if (partner != null) {
            content.add(promiseKind).left().row()
            content.add(promise).growX().height(90f).row()
            conditions.messageText = "Conditions (optional)"
            content.add(conditions).growX().row()
            content.add(expiry).growX().row()
            decision(if (revisingPromise == null) "Offer promise" else "Publish revised promise") {
                require(promise.text.isNotBlank()) { "Describe the proposed promise." }
                val previous = revisingPromise
                val fields = mutableListOf<Pair<String, Any?>>(
                    "participants" to (previous?.get("participants") ?: listOf(session.viewingCivilization, partner)),
                    "kind" to promiseKind.selected,
                    "terms" to promise.text,
                    "conditions" to conditions.text,
                )
                previous?.text("proposal")?.let { fields.add("proposal" to it) }
                if (expiry.text.isNotBlank()) fields.add("expiresTurn" to (expiry.text.toIntOrNull() ?: throw IllegalArgumentException("Enter a valid expiry turn.")))
                request(if (previous == null) "offer" else "revise", *fields.toTypedArray(), mutation = true)
                revisingPromise = null
                promise.text = ""
                conditions.text = ""
                expiry.text = ""
            }
        }
        val versions = state.array("offers").map { it as JsonObject }.groupBy { it.text("proposal") }
        val visible = versions.values.flatMap { offers -> offers.filter { it.flag("effective") || it === offers.last() } }
        for (offer in visible.asReversed()) {
            if (partner != null && offer.array("participants").none { (it as? JsonPrimitive)?.content == partner }) continue
            line("${offer.text("author")} · ${offer.text("kind")} · version ${offer.number("version")}\n${offer.text("terms")}\n${offer.text("conditions")}" + if (offer.flag("effective")) "\nAccepted by all participants." else "")
            val latest = versions.getValue(offer.text("proposal")).last() == offer
            decision("Accept this promise", latest) {
                ConfirmPopup(this, "Accept this exact statement?\n${offer.text("terms")}\n${offer.text("conditions")}", "Accept") {
                    act { request("accept", "proposal" to offer.text("proposal"), "version" to offer.number("version"), mutation = true) }
                }.open()
            }
            decision("Withdraw my commitment") {
                request("withdraw", "proposal" to offer.text("proposal"), "version" to offer.number("version"), "reason" to "Withdrawn by the human player.", mutation = true)
            }
            decision("Reconsider my commitment") {
                request("reconsider", "proposal" to offer.text("proposal"), "version" to offer.number("version"), "reason" to "Reconsidering these terms; let's discuss them.", mutation = true)
            }
            if (offer.text("author") == session.viewingCivilization) {
                button("Revise this promise") {
                    revisingPromise = offer
                    promise.text = offer.text("terms") ?: ""
                    conditions.text = offer.text("conditions") ?: ""
                    expiry.text = offer.number("expiresTurn")?.toString() ?: ""
                    promiseKind.selected = offer.text("kind")
                    rebuild()
                }
            }
        }
    }

    private fun agreements() {
        for (summary in state.array("agreements").map { it as JsonObject }) {
            if (selectedCivilization != null && summary.text("partner") != selectedCivilization) continue
            val agreement = request("agreement_read", "agreement" to summary.text("agreement"))["agreement"] as JsonObject
            line("${agreement.text("partner")} · ${agreement.text("kind")} · ${agreement.text("status")}")
            for (side in listOf("give", "receive")) {
                line((if (side == "give") "You give: " else "You receive: ") + describeItems(agreement.array(side).filterIsInstance<JsonObject>()))
            }
        }
    }

    override fun render(delta: Float) {
        if (changed) refresh()
        if (unreadDirty) markVisibleRead()
        status.setText(listOf(session.message(), notice).filter { it.isNotBlank() }.joinToString("\n"))
        super.render(delta)
    }
}

internal fun JsonObject.text(key: String) = (get(key) as? JsonPrimitive)?.takeUnless { it === JsonNull }?.content
internal fun JsonObject.number(key: String) = (get(key) as? JsonPrimitive)?.intOrNull
internal fun JsonObject.flag(key: String) = (get(key) as? JsonPrimitive)?.booleanOrNull == true
internal fun JsonObject.array(key: String): List<JsonElement> = (get(key) as? JsonArray)?.toList().orEmpty()
