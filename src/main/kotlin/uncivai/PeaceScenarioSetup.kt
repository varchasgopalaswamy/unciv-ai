package uncivai

import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.map.HexCoord
import com.unciv.models.metadata.GameSetupInfo

/** A prepared war with solvent capitals, surplus resources and adjacent armies. */
internal object PeaceScenarioSetup {
    const val MAP_NAME = "Asynchronous peace exercise"

    fun create(setup: GameSetupInfo): GameInfo = TradeScenarioSetup.create(setup).also { game ->
        game.tileMap.mapParameters.name = MAP_NAME
        val rome = game.getCivilization("Rome")
        val greece = game.getCivilization("Greece")
        rome.getDiplomacyManager(greece)!!.declareWar()
        // This fixture begins after the minimum-war period. Normal declarations retain it.
        for ((player, other) in listOf(rome to greece, greece to rome)) {
            player.getDiplomacyManager(other)!!.removeFlag(DiplomacyFlags.DeclaredWar)
            val position = HexCoord(if (player === rome) 0 else 1, 0)
            val unit = checkNotNull(game.tileMap.placeUnitNearTile(position, "Warrior", player))
            check(unit.currentTile.position == position)
            player.popupAlerts.clear()
            player.notifications.clear()
            player.cache.updateViewableTiles()
            player.updateStatsForNextTurn()
        }
    }

    fun instructions(civilization: String): List<String> = listOf(
        "Negotiate one binding peace deal with the other player while continuing scheduled turns. The prepared war has already passed its negotiation cooldown. Keep the armies in place without attacking; preserve existing construction and research queues and resolve mandatory choices.",
        "Use private chat and the ordinary trade tools. trade_options provides exact names and durations. Include one offered Peace Treaty on each side. Read trade_read before counteroffering or accepting. A chat promise is not an engine treaty.",
        "Rome's gameplay job should initiate once if chat_read shows no existing proposal. Leave responses to communication jobs so negotiation proceeds off turn. Never poll, wait for replies, or initiate a second deal. Read the latest terms and relationship notes before responding.",
        if (civilization == "Rome") "Offer mutual Peace Treaty and 1 Silk from Rome in exchange for 4 Gold per turn from Greece. If Greece counters with mutual peace, your 1 Silk, and their 2 Gold per turn plus 20 immediate Gold, read and accept those exact terms." else "Counter Rome's initial proposal once using its ID: mutual Peace Treaty, give Rome 2 Gold per turn and 20 immediate Gold, receive 1 Silk. Keep both treaty terms. Let Rome accept this counteroffer; do not accept the initial 4-GPT price.",
        "After ENACTED, verify peace, treaty duration, resource access and active agreements with trade_options and chat_read. Record the confirmed terms in economy and relationship notes with observed sources, then continue peaceful gameplay. Refresh affected gameplay observations after the deal; an old attack plan is no longer legal.",
    )
}
