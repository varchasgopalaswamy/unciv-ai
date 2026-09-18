package uncivai

import com.unciv.Constants
import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.diplomacy.DiplomacyTurnManager.nextTurn
import com.unciv.logic.map.HexCoord
import com.unciv.logic.trade.TradeLogic
import com.unciv.logic.trade.TradeOfferType
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.GameplayScenario

/** Prepared history uses native trade creation and ageing; subsequent play has ordinary rules. */
internal object AgreementLifecycleScenarioSetup {
    const val MAP_NAME = "Agreement Lifecycle"
    val ironPosition = HexCoord(-2, -2)
    val raiderPosition = HexCoord(-1, -2)

    fun create(seed: Long): GameInfo = WarScenarioMap.create(seed, MAP_NAME).also { game ->
        val players = WarScenarioMap.civilizations.map(game::getCivilization)
        for (player in players) {
            player.units.getCivUnits().toList().forEach { it.destroy() }
            fun research(name: String) {
                if (player.tech.isResearched(name)) return
                game.ruleset.technologies.getValue(name).prerequisites.forEach(::research)
                player.tech.addTechnology(name, false)
            }
            research("Writing")
            research("Iron Working")
            player.tech.techsToResearch = arrayListOf("Metal Casting")
            check(player.tech.canBeResearched("Metal Casting"))
        }
        val rome = game.getCivilization("Rome")
        val france = game.getCivilization("France")
        val mine = game.tileMap[ironPosition]
        check(mine.getOwner() == rome && !mine.isCityCenter())
        mine.setTileResource("Iron")
        mine.resourceAmount = 1
        mine.setImprovementBasic("Mine")
        check(game.tileMap[raiderPosition].getOwner() == null)
        val raider = checkNotNull(game.tileMap.placeUnitNearTile(raiderPosition, "Warrior", france))
        check(raider.currentTile.position == raiderPosition)
        players.forEach {
            it.cache.updateCivResources()
            it.cache.updateViewableTiles()
            it.updateStatsForNextTurn()
        }
        check(rome.getCivResourcesByName()["Iron"] == 1)
        check((game.getCivilization("Greece").getCivResourcesByName()["Iron"] ?: 0) == 0)
        check(mine in france.viewableTiles)

        // Both owner copies start one tick from natural expiry. Never assign a shortened duration.
        val trade = TradeLogic(rome, france)
        trade.currentTrade.ourOffers.add(trade.ourAvailableOffers.single { it.type == TradeOfferType.Gold_Per_Turn }.copy(amount = 1))
        trade.currentTrade.theirOffers.add(trade.theirAvailableOffers.single { it.name == Constants.openBorders }.copy())
        trade.acceptTrade()
        val pairs = listOf(rome to france, france to rome)
        repeat(game.speed.dealDuration - 1) {
            pairs.forEach { (player, other) -> player.getDiplomacyManager(other)!!.nextTurn() }
        }
        for ((player, other) in pairs) {
            val native = player.getDiplomacyManager(other)!!.trades.single()
            check((native.ourOffers + native.theirOffers).all { it.duration == 1 })
        }
        players.forEach {
            it.popupAlerts.clear()
            it.notifications.clear()
            it.updateStatsForNextTurn()
        }
        check(game.turns == 0 && game.currentPlayer == "Rome")
    }

    fun scenario(runtime: GameExecutor, game: GameInfo, player: Civilization): GameplayScenario? {
        if (game.tileMap.mapParameters.name != MAP_NAME || player.civID !in WarScenarioMap.civilizations) return null
        val common = listOf(
            "Guided agreement lifecycle exercise. Scenario instructions are objectives, never evidence that a deal or ending happened. Use only your own observations and communication records.",
            "Read agreement_list and agreement_read for each of your agreements. Before ending your opening turn, create one concise sourced private plan for each agreement, including its ID and the income, access or resource it supports. When an agreement ending flags a plan, read that agreement again and revise the SAME plan from fresh evidence, including the changed effect. Communication jobs should do this even off turn. Do not leave review flags unresolved.",
            "Keep the existing production and research queues. Resolve mandatory choices and read alerts before acknowledging them. Do not make purchases, capture cities, sign ceasefire promises, make extra deals or renew ended agreements. Finish scheduled turns normally; continue after an ending. No free early-cancellation action exists.",
        )
        // France's objective is independent of the negotiators' private proposals and agreement state.
        if (player.civID == "France") {
            val phase = if (game.turns == 0) "PREPARE" else "LIMITED_RAID"
            val task = if (game.turns == 0) {
                "On game turn 0 hold your Warrior at (-1,-2). Inspect and plan around your existing Rome agreement, then finish the turn. Do not declare war yet."
            } else {
                "From game turn 1 conduct one limited raid on Rome: inspect your visible Roman Iron Mine at (-2,-2), declare war on Rome with the ordinary confirmed command if still at peace, move your Warrior from (-1,-2) onto that mine, then PILLAGE it with confirmation. Read the unit's legal move and pillage options. If the mine is already pillaged, hold position and finish normal turns. Do not attack or capture cities, declare war on Greece, repair the mine, or negotiate a peace deal during this exercise. Do not infer anyone else's private trade from this objective."
            }
            return GameplayScenario("AGREEMENT_LIFECYCLE_EXERCISE", phase, common + task)
        }
        val state = runtime.communicationState
        val ids = state.streams[player.civID].orEmpty().filter { it.kind == "trade" }
            .map { it.body.getValue("proposal").jsonPrimitive.content }.distinct()
        val proposals = ids.mapNotNull { state.trades.proposals[it]?.lastOrNull() }
            .filter { it.participants().toSet() == setOf("Rome", "Greece") }
        val latest = proposals.singleOrNull()
        val failed = proposals.size > 1 || latest?.let { it.status !in setOf("PENDING", "ENACTED") || !validTerms(game, it) } == true
        val phase = when {
            failed -> "OBJECTIVE_UNMET"
            latest?.status == "ENACTED" -> "OBSERVE_AND_REVIEW"
            else -> "NEGOTIATE"
        }
        val role = when (phase) {
            "OBJECTIVE_UNMET" -> "The negotiation did not meet the objective. Record the failure without opening a replacement deal and continue normal turns."

            "OBSERVE_AND_REVIEW" -> "The resource negotiation is enacted. Inspect its native agreement and maintain your sourced plan. On expiry or interruption, refresh the agreement, review the affected plan and explain what your own income or Iron supply now permits. Hold units and keep Rome and Greece at peace; do not repair the prepared mine or repeat a deal after restart."

            else -> if (player.civID == "Rome") {
                "Your ACTIVE opening gameplay job opens exactly one proposal with Greece: give 1 Iron and request 2 Gold per turn, using the normal durations from trade_options. Greece may accept or counter with a price from 2 to 4 GPT. Negotiate that price yourselves; a counter is optional. Read the latest trade_read before consenting. Communication jobs only continue the existing proposal. After enactment, read the agreement and save its sourced plan BEFORE ending your opening turn."
            } else {
                "Rome opens one proposal giving you 1 Iron for 2 GPT. Choose an acceptable price from 2 to 4 GPT; you may accept or counter that same proposal. Read trade_options and the latest trade_read before consenting, using normal native durations. Handle the proposal in communication even off turn. Do not open a second deal. After enactment, read the agreement and create a sourced plan for your imported Iron BEFORE ending your opening turn."
            }
        }
        val pending = latest?.takeIf { it.status == "PENDING" }?.let { "Existing proposal ${it.proposal}, version ${it.version}: continue it rather than opening another." }
            ?: "Check your own chat_read before initiating; never duplicate an offer opened by another job. Let the service wait for negotiation; never poll."
        return GameplayScenario("AGREEMENT_LIFECYCLE_EXERCISE", phase, common + listOf(role, pending))
    }

    private fun validTerms(game: GameInfo, proposal: EngineTradeProposal): Boolean {
        if (proposal.friendship != null || proposal.clauses.size != 2) return false
        return proposal.clauses.any { it.from == "Rome" && it.to == "Greece" && it.item == TradeSelection(TradeOfferType.Strategic_Resource, "Iron", 1, game.speed.dealDuration) } &&
            proposal.clauses.any { it.from == "Greece" && it.to == "Rome" && it.item.type == TradeOfferType.Gold_Per_Turn && it.item.name == Constants.goldPerTurn && it.item.amount in 2..4 && it.item.duration == game.speed.dealDuration }
    }
}
