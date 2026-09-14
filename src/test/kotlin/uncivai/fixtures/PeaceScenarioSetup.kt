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
}
