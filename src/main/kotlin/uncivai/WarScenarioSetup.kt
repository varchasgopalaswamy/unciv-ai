package uncivai

import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.trade.TradeOfferType
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.GameplayScenario

/** Only the two negotiators inspect their shared proposals. The target's phase uses its own wars. */
internal object WarScenarioSetup {
    fun mapName(variant: String): String = when (variant) {
        "war-paid" -> "Three-Cornered War: paid"
        "war-joint" -> "Three-Cornered War: joint"
        else -> error("Unknown war exercise")
    }

    fun seedHistory(runtime: GameExecutor, variant: String) {
        if (variant != "war-paid") return
        val communication = runtime.communication
        val offered = communication.handle(
            "Greece",
            obj(
                "op" to "offer",
                "epoch" to runtime.epoch,
                "requestId" to "prepared-peace-offer",
                "participants" to listOf("Greece", "France"),
                "kind" to "peace",
                "terms" to "Prepared background: Greece and France promise not to attack each other.",
                "conditions" to "Until explicitly reconsidered. This is prepared history, not a live negotiation.",
            ),
        ).getValue("offer").jsonObject
        communication.handle(
            "France",
            obj(
                "op" to "accept",
                "epoch" to runtime.epoch,
                "requestId" to "prepared-peace-accept",
                "proposal" to offered["proposal"],
                "version" to 1,
            ),
        )
    }

    fun scenario(runtime: GameExecutor, game: GameInfo, player: Civilization): GameplayScenario? {
        val variant = when (game.tileMap.mapParameters.name) {
            mapName("war-paid") -> "war-paid"
            mapName("war-joint") -> "war-joint"
            else -> return null
        }
        val common = listOf(
            "Limited frontier conflict exercise. Choose your own military moves, defenses and legal attacks; do not capture capitals. Keep cities productive and finish normal turns.",
            "Read your own full native alerts and communication records. Update private plans with sources; distinguish observations, messages and inferences. Scenario instructions are objectives, not evidence that an event happened.",
        )
        // Do this before accessing the negotiators' private records, including their phase labels.
        if (player.civID == "France") return GameplayScenario("FRONTIER_EXERCISE", if (player.isAtWar()) "DEFEND" else "PREPARE", common + "Protect your frontier and maintain diplomatic awareness. Read and handle your own notices and commitments. Negotiate and communicate as you see fit; infer intentions only from information available to you.")
        if (player.civID !in setOf("Rome", "Greece")) return null
        val state = runtime.communicationState
        val ids = state.streams[player.civID].orEmpty().filter { it.kind == "trade" }.map { it.body.getValue("proposal").jsonPrimitive.content }.distinct()
        val proposals = ids.mapNotNull { state.trades.proposals[it]?.lastOrNull() }.filter { it.participants().toSet() == setOf("Rome", "Greece") }
        val done = proposals.count { it.status == "ENACTED" }
        val expected = if (variant == "war-paid") 2 else 1
        val failed = proposals.any { it.status !in setOf("PENDING", "ENACTED") } || proposals.size > expected ||
            proposals.withIndex().any { (index, proposal) -> !validTerms(proposal, variant, index) }
        val phase = when {
            failed -> "OBJECTIVE_UNMET"
            done >= expected -> "CONTINUE"
            variant == "war-joint" -> "JOINT_WAR"
            done == 0 -> "PAID_WAR"
            else -> "JOIN_WAR"
        }
        val role = when (phase) {
            "OBJECTIVE_UNMET" -> "A negotiation ended without enactment or extra proposals were opened. Do not silently replace it or claim success. Record the unmet objective and continue ordinary play for operator review."
            "CONTINUE" -> "The negotiated deals are enacted. Inspect their canonical outcomes and your native relationships. Continue limited military play; do not repeat deals after a restart."
            "JOINT_WAR" -> "Rome initiates a joint declaration against France with Greece. Negotiate any immediate Gold payment yourselves. One proposal must include a WarDeclaration against France from BOTH parties. Read trade_options, all warnings and the final trade_read before consenting."
            "PAID_WAR" -> if (player.civID == "Rome") "On your first turn offer Greece 30 immediate Gold for Greece alone to declare war on France. Your private spending cap is 60 Gold. Negotiate with Greece and accept an affordable final counter after reading it. Remain neutral yourself for this deal." else "Your private minimum payment to declare war on France is 40 Gold. Read Rome's offer and negotiate a counter in the 40–60 Gold range. First read your existing accepted peace promise with France and explicitly reconsider it with a reason BEFORE publishing a counter: a counter already records your consent. You alone declare war in this deal."
            else -> if (player.civID == "Greece") "During your next Greek gameplay turn, offer Rome 10–20 immediate Gold to join your existing war against France. Negotiate the price yourselves. Request a one-sided WarDeclaration by Rome; you are already at war. Do not open this follow-up during Rome's suspended gameplay turn." else "Greece seeks your participation in its existing war against France. You are willing to join for 10–20 immediate Gold. Wait for Greece's offer on its gameplay turn, read it and negotiate the final terms. The WarDeclaration is by you alone."
        }
        return GameplayScenario(
            "WAR_NEGOTIATION_EXERCISE",
            phase,
            common + listOf(
                role,
                proposals.lastOrNull { it.status == "PENDING" }?.let { "Proposal ${it.proposal}, version ${it.version}, is already pending. Read and continue that negotiation; do not open another offer from a parallel job." } ?: "No negotiation is currently pending. Check chat_read for offers from your other jobs before opening one.",
                "Use one proposal per milestone and counter that proposal, never replacement offers. Give/receive are from your perspective. Read trade_options and your warnings, inspect the latest trade_read before deciding. Let the service wait; never poll. If another job already opened the negotiation, read its proposal and continue it. A refusal or expiry is an unmet objective. Ordinary turns resume after settlement.",
            ),
        )
    }

    private fun validTerms(proposal: EngineTradeProposal, variant: String, index: Int): Boolean {
        val wars = proposal.clauses.filter { it.item.type == TradeOfferType.WarDeclaration }
        val gold = proposal.clauses.filter { it.item.type == TradeOfferType.Gold }
        val declarers = if (variant == "war-joint") {
            setOf("Rome", "Greece")
        } else if (index == 0) {
            setOf("Greece")
        } else {
            setOf("Rome")
        }
        if (proposal.friendship != null || wars.size != declarers.size || wars.map { it.from }.toSet() != declarers) return false
        if (wars.any { it.item.name != "France" || it.item.amount != 1 || it.item.duration != -1 }) return false
        if (proposal.clauses.size != wars.size + gold.size || gold.size > 1) return false
        if (gold.any { it.item.name != "Gold" || it.item.duration != -1 || it.item.amount <= 0 }) return false
        if (variant == "war-joint") return true
        val payment = gold.singleOrNull() ?: return false
        val payer = if (index == 0) "Rome" else "Greece"
        val price = if (index == 1) {
            10..20
        } else if (proposal.version == 1 && proposal.status == "PENDING") {
            30..30
        } else {
            40..60
        }
        return payment.from == payer && payment.item.amount in price
    }
}
