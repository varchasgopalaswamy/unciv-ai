package uncivai

import com.unciv.Constants
import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.GameplayScenario

/** A complete bilateral exercise. Progress comes from committed proposals, never agent notes. */
internal object TradeCycleScenarioSetup {
    const val MAP_NAME = "Complete trade cycle exercise"

    enum class Step(val terminal: String = "ENACTED", val authors: List<String> = listOf("Rome")) {
        PAID_PEACE(authors = listOf("Rome", "Greece")),
        DECLINE("DECLINED"),
        WITHDRAW("WITHDRAWN"),
        DEADLINE("EXPIRED"),
        GIFT,
        RESOURCE_BARGAIN(authors = listOf("Rome", "Greece", "Rome", "Greece")),
        FRIENDSHIP,
        EMBASSY_IN_ROME,
        EMBASSY_IN_ATHENS,
        GREEK_ACCESS,
        FUNDED_TREATIES_AND_CITIES,
    }

    fun create(seed: Long): GameInfo = TradeCycleMap.create(seed).also { game ->
        game.tileMap.mapParameters.name = MAP_NAME
        val rome = game.getCivilization("Rome")
        val greece = game.getCivilization("Greece")
        rome.addGold(2000 - rome.gold)
        greece.addGold(-greece.gold)
        rome.getDiplomacyManager(greece)!!.declareWar()
        // Begin after the normal minimum-war period, as in the standalone peace fixture.
        // No cooldown is bypassed after play begins, and no treaty is pre-signed.
        for ((player, other) in listOf(rome to greece, greece to rome)) {
            player.getDiplomacyManager(other)!!.removeFlag(DiplomacyFlags.DeclaredWar)
            player.popupAlerts.clear()
            player.notifications.clear()
            player.updateStatsForNextTurn()
        }
    }

    /** Rome receives every proposal in this two-player exercise. Event order survives restart. */
    fun proposals(state: CommunicationState): List<List<EngineTradeProposal>> = state.streams["Rome"].orEmpty()
        .filter { it.kind == "trade" }
        .map { it.body.getValue("proposal").jsonPrimitive.content }
        .distinct().map { state.trades.proposals.getValue(it) }

    fun scenario(runtime: GameExecutor, game: GameInfo, civilization: String): GameplayScenario {
        val history = proposals(runtime.communicationState)
        fun view(phase: String, vararg instructions: String) = GameplayScenario("TRADE_CYCLE_EXERCISE", phase, listOf(*instructions))
        val routine = "Keep Gold production and research running; resolve mandatory choices and read alerts before acknowledging them. Keep both received cities with RESOLVE_CAPTURED_CITY when prompted. Do not rename, raze or trade away cities, spend gold on purchases, declare war, or start extra deals. End scheduled turns normally."
        fun failed(reason: String) = view("FAILED", reason, "Do not reopen or repair a failed exercise by inventing replacement deals. If a proposal is still pending, its author should withdraw it. Record the failure and continue ordinary turns.", routine)
        if (runtime.communicationState.trades.counterofferLimit != 3) return failed("This exercise requires --trade-counteroffers 3.")
        if (history.size > Step.entries.size) return failed("An extra negotiation was opened after the exercise.")
        for ((index, versions) in history.withIndex()) {
            val step = Step.entries[index]
            if (!validTerms(game, step, versions)) return failed("${step.name}: the stored terms or counteroffer sequence differ from the exercise.")
            val latest = versions.last()
            if (latest.status == "PENDING") return instructions(step, civilization, latest)
            if (latest.status != step.terminal || versions.size != step.authors.size ||
                (step.terminal == "ENACTED" && latest.outcome == null)
            ) {
                return failed("${step.name}: expected ${step.terminal}, observed ${latest.status} at version ${latest.version}.")
            }
        }
        if (history.size == Step.entries.size) {
            return view(
                "TRADES_ENACTED",
                "All eleven negotiations reached their intended outcomes. Inspect trade_options, your economy, owned cities and any pending city disposition; record sourced economy and relationship facts. Continue for two more complete rounds so the operator can verify ownership, GPT, resources and treaty durations in the save. Do not renew agreements.",
                routine,
            )
        }
        if (history.lastOrNull()?.last()?.turn == game.turns) {
            return view("CONTINUE_TURN", "This round's negotiation is complete. Inspect its outcome and update concise economy/relationship notes with evidence. The next step starts on Rome's next turn. Communication jobs should not open it.", routine)
        }
        return instructions(Step.entries[history.size], civilization, null)
    }

    private fun instructions(step: Step, civilization: String, pending: EngineTradeProposal?): GameplayScenario {
        val role = if (civilization == "Rome") "Only your ACTIVE gameplay job opens this step, once. Communication jobs only handle the existing proposal." else "Rome opens this step during its gameplay turn. Handle its proposal in your communication job, even off turn; do not initiate another deal."
        val common = listOf(
            "Trade-cycle step ${step.ordinal + 1}/${Step.entries.size}: ${step.name}. $role ${pending?.let { "Current proposal: ${it.proposal}." } ?: "No proposal exists for this step yet."}",
            "Read trade_options for legal items and exact durations/costs, and trade_read before deciding or countering. Give/receive are from YOUR perspective. The service handles versions and waits; never poll. After settlement refresh player_turn, record concise sourced economy/relationship facts, and finish the turn. Only one new negotiation per Rome turn.",
        )
        val task = when (step) {
            Step.PAID_PEACE -> "The prepared war is eligible for peace. Rome initially offers mutual Peace Treaty plus 40 immediate Gold to Greece. Greece must counter once: give the matching Peace Treaty, receive the matching Peace Treaty and 30 Gold. Rome reads and accepts that counter. Keep both treaty terms; no attacks."
            Step.DECLINE -> "Rome offers Greece a gift of 7 immediate Gold, receiving nothing. Greece reads it and DECLINES. This deliberate decline is success for this step; nobody transfers gold or reopens it."
            Step.WITHDRAW -> "Rome offers Greece a gift of 11 immediate Gold, receiving nothing. Greece reads it, does not accept or counter, and sends Rome one private message asking it to withdraw. Rome's communication job reads its own pending offer and WITHDRAWS it. Withdrawal is the intended outcome."
            Step.DEADLINE -> "Rome offers Greece a gift of 13 immediate Gold, receiving nothing. Greece reads it but makes NO decision, counteroffer or reply. Both communication jobs finish without waiting. Let the host deadline expire this proposal; the suspended gameplay tool resumes automatically. EXPIRED is success here; do not reopen."
            Step.GIFT -> "Rome offers Greece a gift of 17 immediate Gold, receiving nothing. Greece reads and accepts. Verify the engine confirmation; this is an actual one-sided gift."
            Step.RESOURCE_BARGAIN -> "Use one proposal and exactly three counters. Every version transfers Rome's 1 Silk and 2 Iron to Greece, and Greece's 1 Gold Ore to Rome. Initial: Greece also pays 4 GPT. First counter, by Greece: instead pay 3 GPT + 10 Gold. Second counter, by Rome: ask 2 GPT + 20 Gold. Third counter, by Greece: pay 2 GPT + 25 Gold. Rome checks counteroffersRemaining=0, reads and accepts the final terms. Never accept an earlier price or open another proposal."
            Step.FRIENDSHIP -> "Rome uses friendship_offer for Declaration of Friendship. Greece reads the fixed engine terms and accepts with trade_decide. Do not substitute a social commitment. Inspect unavailable treaty reasons: mutual embassies still need to be established."
            Step.EMBASSY_IN_ROME -> "Rome offers only its own Accept Embassy item, receiving nothing. Greece accepts. Verify that Greece now has an embassy in Rome's capital and Rome does not yet have one in Athens; this is directional."
            Step.EMBASSY_IN_ATHENS -> "Rome requests only Greece's Accept Embassy item, giving nothing. Greece accepts. Verify mutual embassies and newly available treaty options."
            Step.GREEK_ACCESS -> "Rome offers only its own Open Borders, receiving nothing. Greece accepts. Verify Greece may enter Roman territory, while Rome may not yet enter Greek territory."
            Step.FUNDED_TREATIES_AND_CITIES -> "Rome offers its non-capital Antium AND immediate Gold equal to researchFunding.costEach, plus Research Agreement and Defensive Pact. It receives Greece's non-capital Sparta AND Greece's Open Borders, plus IDENTICAL Research Agreement and Defensive Pact terms. Each civilization pays the treaty's exact signing cost; the immediate gold funds Greece's payment. Use the cities' short City trade IDs from trade_options. Greece reads and accepts. These cities are bought from their founders and kept automatically; inspect your received city during gameplay. Choose KeepCity only if a disposition actually appears. Verify reciprocal access, friendship, research/defense treaties and ownership."
        }
        return GameplayScenario("TRADE_CYCLE_EXERCISE", step.name, common + task)
    }

    /** Verify all versions, not just a terminal label. City labels here are fixture names. */
    fun validTerms(game: GameInfo, step: Step, versions: List<EngineTradeProposal>): Boolean {
        if (versions.isEmpty() || versions.size > step.authors.size) return false
        for ((index, proposal) in versions.withIndex()) {
            if (proposal.version != index + 1 || proposal.author != step.authors[index] || proposal.participants().toSet() != setOf("Rome", "Greece")) return false
            if (index < versions.lastIndex && proposal.status != "SUPERSEDED") return false
            if (proposal.counterofferLimit != 3 || proposal.deadlineMillis != versions.first().deadlineMillis) return false
            if (step == Step.FRIENDSHIP) {
                if (proposal.friendship != FriendshipTerms() || proposal.clauses.isNotEmpty()) return false
                continue
            }
            if (proposal.friendship != null) return false
            val researchCost = proposal.clauses.firstOrNull { it.item.name == Constants.researchAgreement }?.item?.amount ?: 1
            fun item(type: TradeOfferType, name: String, amount: Int = 1) = TradeOffer(name, type, amount, game.speed).let { TradeSelection(it.type, it.name, it.amount, it.duration) }
            fun gold(amount: Int) = item(TradeOfferType.Gold, "Gold", amount)
            val peace = item(TradeOfferType.Treaty, Constants.peaceTreaty)
            val embassy = item(TradeOfferType.Embassy, Constants.acceptEmbassy)
            val borders = item(TradeOfferType.Agreement, Constants.openBorders)
            val (give, receive) = when (step) {
                Step.PAID_PEACE -> listOf(peace, gold(if (index == 0) 40 else 30)) to listOf(peace)

                Step.DECLINE -> listOf(gold(7)) to emptyList()

                Step.WITHDRAW -> listOf(gold(11)) to emptyList()

                Step.DEADLINE -> listOf(gold(13)) to emptyList()

                Step.GIFT -> listOf(gold(17)) to emptyList()

                Step.RESOURCE_BARGAIN -> listOf(item(TradeOfferType.Luxury_Resource, "Silk"), item(TradeOfferType.Strategic_Resource, "Iron", 2)) to
                    (listOf(item(TradeOfferType.Luxury_Resource, "Gold Ore"), item(TradeOfferType.Gold_Per_Turn, Constants.goldPerTurn, listOf(4, 3, 2, 2)[index])) + if (index == 0) emptyList() else listOf(gold(listOf(0, 10, 20, 25)[index])))

                Step.EMBASSY_IN_ROME -> listOf(embassy) to emptyList()

                Step.EMBASSY_IN_ATHENS -> emptyList<TradeSelection>() to listOf(embassy)

                Step.GREEK_ACCESS -> listOf(borders) to emptyList()

                Step.FUNDED_TREATIES_AND_CITIES -> {
                    val treaties = listOf(item(TradeOfferType.Treaty, Constants.researchAgreement, researchCost), item(TradeOfferType.Treaty, Constants.defensivePact))
                    (treaties + gold(researchCost) + item(TradeOfferType.City, "Antium")) to (treaties + borders + item(TradeOfferType.City, "Sparta"))
                }

                Step.FRIENDSHIP -> error("Handled above")
            }
            fun actual(from: String) = proposal.clauses.filter { it.from == from && it.to == if (from == "Rome") "Greece" else "Rome" }.map {
                if (it.item.type == TradeOfferType.City) it.item.copy(name = it.city?.name ?: "") else it.item
            }
            if (proposal.clauses.size != give.size + receive.size || actual("Rome").toSet() != give.toSet() || actual("Greece").toSet() != receive.toSet()) return false
        }
        return true
    }
}
