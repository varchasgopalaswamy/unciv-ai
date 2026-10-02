package uncivai

import com.unciv.Constants
import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.Notification
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.trade.Trade
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import java.util.IdentityHashMap

/** These bindings are operator-only. A native row has identity but no serialized ID. */
@Serializable
internal data class AgreementState(val players: MutableMap<String, PlayerAgreements> = mutableMapOf())

@Serializable
internal data class PlayerAgreements(var nextId: Long = 1, val records: MutableMap<String, AgreementRecord> = linkedMapOf())

@Serializable
internal data class AgreementTerms(val give: List<TradeSelection> = emptyList(), val receive: List<TradeSelection> = emptyList())

@Serializable
internal data class AgreementRecord(
    val agreement: String,
    val partner: String,
    val kind: String,
    val observedTurn: Int,
    val initial: JsonObject,
    var nativeSlot: Int?,
    var nativeTerms: AgreementTerms,
    var current: JsonObject,
    var effects: JsonObject,
    var status: String = "ACTIVE",
    var version: Int = 1,
    var endedTurn: Int? = null,
    val history: MutableList<JsonObject> = mutableListOf(),
)

/** Observe only the owner's native rows, flags and notifications. Never explain an ending
 * using a foreign resource balance, research counter, unseen war or hidden partner.
 * Reconciliation and its recipient events join the game mutation's atomic checkpoint.
 */
internal class AgreementLifecycle(private val runtime: GameExecutor) {
    private val state get() = runtime.communicationState.agreements
    private var boundState: AgreementState? = null
    private var boundGame: GameInfo? = null
    private val rows = mutableMapOf<String, IdentityHashMap<Trade, String>>()
    private val notifications = mutableMapOf<String, Set<Notification>>()

    fun synchronize(game: GameInfo): Boolean {
        if (boundState !== state || boundGame !== game) bind(game)
        var changed = false
        for (player in game.civilizations.filter { it.isHuman() && it.isMajorCiv() }) {
            val saved = state.players.getOrPut(player.civID) { PlayerAgreements() }
            val bindings = rows.getOrPut(player.civID) { IdentityHashMap() }
            val newNotices = player.notifications.filter { it !in notifications[player.civID].orEmpty() }.map { it.text }
            val seen = mutableSetOf<String>()
            for (other in player.getKnownCivs().filter { it.isMajorCiv() }.sortedBy { it.civID }) {
                val diplomacy = player.getDiplomacyManager(other) ?: continue
                for ((index, trade) in diplomacy.trades.withIndex()) {
                    val terms = terms(trade)
                    val id = bindings[trade]
                    val record = if (id == null) {
                        create(saved, player, other, "TRADE", index, terms).also {
                            bindings[trade] = it.agreement
                            changed = true
                        }
                    } else {
                        saved.records.getValue(id)
                    }
                    seen.add(record.agreement)
                    if (record.nativeSlot != index) {
                        record.nativeSlot = index
                        changed = true
                    }
                    changed = update(record, player, other, terms) || changed
                }
                for ((kind, terms) in relationships(player, other)) {
                    val record = saved.records.values.singleOrNull { it.partner == other.civID && it.kind == kind && it.status == "ACTIVE" }
                        ?: create(saved, player, other, kind, null, terms).also { changed = true }
                    seen.add(record.agreement)
                    changed = update(record, player, other, terms) || changed
                }
            }
            for (record in saved.records.values.filter { it.status == "ACTIVE" && it.agreement !in seen }) {
                val other = game.getCivilization(record.partner)
                val removedRow = bindings.entries.firstOrNull { it.value == record.agreement }?.key
                val reason = endingReason(record, player, other, removedRow, newNotices)
                record.status = "ENDED"
                record.endedTurn = game.turns
                record.nativeSlot = null
                record.version++
                val before = record.current
                record.nativeTerms = AgreementTerms()
                record.current = publicTerms(player, record.nativeTerms, record.kind)
                record.effects = effects(player, other)
                emit(record, player, "ENDED", reason, before)
                if (removedRow != null) bindings.remove(removedRow)
                changed = true
            }
            notifications[player.civID] = identitySet(player.notifications)
        }
        return changed
    }

    /** Reattach by checkpointed ordinal AND exact terms, including duration and duplicates.
     * Never use equalTrade: it deliberately ignores duration. Fail closed on a mismatched save.
     */
    private fun bind(game: GameInfo) {
        rows.clear()
        notifications.clear()
        for ((civilization, saved) in state.players) {
            val player = game.getCivilization(civilization)
            val bindings = IdentityHashMap<Trade, String>()
            for (record in saved.records.values.filter { it.kind == "TRADE" && it.status == "ACTIVE" }) {
                val trade = player.getDiplomacyManager(record.partner)?.trades?.getOrNull(checkNotNull(record.nativeSlot))
                check(trade != null && terms(trade) == record.nativeTerms && !bindings.containsKey(trade)) { "Agreement checkpoint does not match native trade rows" }
                bindings[trade] = record.agreement
            }
            rows[civilization] = bindings
        }
        for (player in game.civilizations) notifications[player.civID] = identitySet(player.notifications)
        boundState = state
        boundGame = game
    }

    private fun create(saved: PlayerAgreements, player: Civilization, other: Civilization, kind: String, index: Int?, terms: AgreementTerms): AgreementRecord {
        val id = "a${saved.nextId++}"
        val current = publicTerms(player, terms, kind)
        return AgreementRecord(id, other.civID, kind, player.gameInfo.turns, current, index, terms, current, effects(player, other)).also { saved.records[id] = it }
    }

    private fun update(record: AgreementRecord, player: Civilization, other: Civilization, terms: AgreementTerms): Boolean {
        val currentEffects = effects(player, other)
        if (record.nativeTerms == terms) {
            if (record.effects == currentEffects) return false
            record.effects = currentEffects
            record.version++
            return true
        }
        val before = record.current
        val previous = record.nativeTerms
        record.nativeTerms = terms
        record.current = publicTerms(player, terms, record.kind)
        record.effects = currentEffects
        record.version++
        fun ended(old: List<TradeSelection>, now: List<TradeSelection>) = old.indices.any { old[it].duration > 0 && (now.getOrNull(it)?.duration ?: 0) <= 0 }
        if (ended(previous.give, terms.give) || ended(previous.receive, terms.receive)) emit(record, player, "TERMS_ENDED", "EXPIRED", before)
        return true
    }

    private fun emit(record: AgreementRecord, player: Civilization, change: String, reason: String, before: JsonObject) {
        val event = obj(
            "agreement" to record.agreement, "partner" to record.partner, "kind" to record.kind,
            "version" to record.version, "status" to record.status, "change" to change, "reason" to reason,
            "turn" to player.gameInfo.turns, "before" to before, "after" to record.current, "effects" to record.effects,
        )
        record.history.add(event)
        val wakes = if (player.gameInfo.getCivilization(record.partner).isHuman()) setOf(player.civID) else emptySet()
        runtime.communication.emit(listOf(player.civID), "agreement", event, wakes)
        runtime.turnSessions[player.civID]?.invalidate()
    }

    private fun endingReason(record: AgreementRecord, player: Civilization, other: Civilization, row: Trade?, notices: List<String>): String {
        val last = row?.let(::terms) ?: record.nativeTerms
        val offers = last.give + last.receive
        val timed = (record.nativeTerms.give + record.nativeTerms.receive).any { it.duration >= 0 }
        return when {
            record.kind == "TRADE" && offers.all { it.duration <= 0 } -> if (timed) "EXPIRED" else "COMPLETED"

            player.isAtWarWith(other) -> "WAR"

            player.isDefeated() || other.isDefeated() -> "CIVILIZATION_DEFEATED"

            record.kind == "TRADE" && notices.any { it == "One of our trades with [${other.civName}] has been cut short" } -> "RESOURCE_UNAVAILABLE"

            (record.kind == "DEFENSIVE_PACT" || offers.any { it.name == Constants.defensivePact }) &&
                notices.any { it == "[${other.civName}] cancelled their Defensive Pact with us!" || it == "We have cancelled our Defensive Pact with [${other.civName}]!" } -> "DEFENSIVE_PACT_CANCELLED"

            record.kind != "TRADE" && offers.any { it.duration == 1 } -> "EXPIRED"

            else -> "NATIVE_REMOVAL"
        }
    }

    fun summaries(civilization: String): List<JsonObject> = state.players[civilization]?.records?.values.orEmpty().map(::summary)

    fun read(civilization: String, id: String): JsonObject {
        val record = state.players[civilization]?.records?.get(id) ?: throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
        return obj(
            "agreement" to JsonObject(
                summary(record) + record.current + obj(
                    "observedTurn" to record.observedTurn,
                    "endedTurn" to record.endedTurn,
                    "initialGive" to record.initial["give"],
                    "initialReceive" to record.initial["receive"],
                    "history" to record.history,
                    "effects" to record.effects,
                    "cancellation" to obj("available" to false, "unavailableReason" to "Fixed-duration agreements cannot be cancelled directly."),
                ),
            ),
        )
    }

    fun reference(civilization: String, partner: String, index: Int): String? = state.players[civilization]?.records?.values?.singleOrNull {
        it.partner == partner && it.kind == "TRADE" && it.status == "ACTIVE" && it.nativeSlot == index
    }?.agreement

    private fun summary(record: AgreementRecord): JsonObject = obj(
        "agreement" to record.agreement,
        "partner" to record.partner,
        "kind" to record.kind,
        "version" to record.version,
        "status" to record.status,
        "give" to record.current["give"],
        "receive" to record.current["receive"],
        "remainingTurns" to if (record.status == "ENDED") 0 else (record.nativeTerms.give + record.nativeTerms.receive).map { it.duration }.filter { it >= 0 }.maxOrNull(),
    )

    private fun terms(trade: Trade) = AgreementTerms(trade.ourOffers.map(::selection), trade.theirOffers.map(::selection))
    private fun selection(offer: TradeOffer) = TradeSelection(offer.type, offer.name, offer.amount, offer.duration)

    private fun publicTerms(player: Civilization, terms: AgreementTerms, kind: String): JsonObject {
        fun items(items: List<TradeSelection>) = items.map {
            val supported = (it.type !in setOf(TradeOfferType.WarDeclaration, TradeOfferType.PeaceProposal, TradeOfferType.Introduction) || player.getKnownCivs().any { target -> target.isMajorCiv() && target.civID == it.name })
            val name = if (supported && it.type != TradeOfferType.City) it.name else null
            val display = when {
                kind != "TRADE" -> it.name.tr() + if (it.duration > 0) " (${it.duration})" else ""
                it.type == TradeOfferType.City -> player.gameInfo.getCities().firstOrNull { city -> city.id == it.name }?.let { city -> "${city.name} (${city.population.population})" } ?: "Non-existent city"
                supported -> it.offer().getOfferText()
                else -> "Unsupported trade term: [${it.type.name}]".tr()
            }
            obj("type" to it.type.name, "name" to name, "amount" to it.amount, "duration" to it.duration, "displayText" to display, "supported" to supported)
        }
        return obj("give" to items(terms.give), "receive" to items(terms.receive))
    }

    private fun relationships(player: Civilization, other: Civilization): Map<String, AgreementTerms> {
        val diplomacy = player.getDiplomacyManager(other)!!
        val result = linkedMapOf<String, AgreementTerms>()
        for ((kind, flag, name) in listOf(
            Triple("FRIENDSHIP", DiplomacyFlags.DeclarationOfFriendship, "Declaration of Friendship"),
            Triple("RESEARCH_AGREEMENT", DiplomacyFlags.ResearchAgreement, Constants.researchAgreement),
            Triple("DEFENSIVE_PACT", DiplomacyFlags.DefensivePact, Constants.defensivePact),
        )) {
            val duration = diplomacy.getFlag(flag)
            if (duration > 0) {
                val term = TradeSelection(TradeOfferType.Treaty, name, 1, duration)
                result[kind] = AgreementTerms(listOf(term), listOf(term))
            }
        }
        val status = bilateralStatus(player, other)
        val term = TradeSelection(TradeOfferType.Embassy, Constants.acceptEmbassy, 1, -1)
        val give = if (status.getValue("theirEmbassyInYourCapital").jsonPrimitive.boolean) listOf(term) else emptyList()
        val receive = if (status.getValue("yourEmbassyInTheirCapital").jsonPrimitive.boolean) listOf(term) else emptyList()
        if (give.isNotEmpty() || receive.isNotEmpty()) result["EMBASSY"] = AgreementTerms(give, receive)
        return result
    }

    private fun effects(player: Civilization, other: Civilization): JsonObject {
        val diplomacy = player.getDiplomacyManager(other)!!
        return obj(
            "relationship" to bilateralStatus(player, other),
            "goldPerTurnWithPartner" to diplomacy.goldPerTurn(),
            "resourcesWithPartner" to diplomacy.resourcesFromTrade().map { obj("name" to it.resource.name, "net" to it.amount) },
            "scope" to "Current native bilateral state and your trade flows; ending a row does not undo completed payments or city transfers.",
        )
    }

    private fun identitySet(values: Iterable<Notification>): Set<Notification> = java.util.Collections.newSetFromMap(IdentityHashMap<Notification, Boolean>()).apply { addAll(values) }
}
