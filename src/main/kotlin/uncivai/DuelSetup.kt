package uncivai

import com.unciv.logic.GameInfo
import com.unciv.logic.GameStarter
import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.map.MapParameters
import com.unciv.logic.map.MapSize
import com.unciv.logic.map.MapType
import com.unciv.models.metadata.BaseRuleset
import com.unciv.models.metadata.GameParameters
import com.unciv.models.metadata.GameSetupInfo
import com.unciv.models.metadata.Player

/** Operator-owned setup. Human engine slots prevent built-in AI substitution. */
internal class DuelSetup(private val seed: Long) {
    fun create(): GameInfo {
        val parameters = GameParameters().apply {
            baseRuleset = BaseRuleset.Civ_V_Vanilla.fullName
            speed = "Quick"
            startingEra = "Ancient era"
            numberOfCityStates = 0
            noBarbarians = true
            shufflePlayerOrder = false
            victoryTypes = arrayListOf("Domination", "Time")
            maxTurns = 330
            players = arrayListOf(Player("Rome", PlayerType.Human), Player("Greece", PlayerType.Human))
        }
        val map = MapParameters().apply {
            baseRuleset = parameters.baseRuleset
            type = MapType.pangaea
            mapSize = MapSize.Tiny
            noRuins = true
            this.seed = this@DuelSetup.seed
        }
        return GameStarter.startNewGame(GameSetupInfo(parameters, map)).also { game ->
            check(game.gameParameters.baseRuleset == parameters.baseRuleset && game.gameParameters.mods.isEmpty())
            check(game.civilizations.filter { it.isMajorCiv() }.map { it.civName }.toSet() == setOf("Rome", "Greece"))
            check(game.civilizations.filter { it.isMajorCiv() }.all { it.isHuman() })
            check(game.currentPlayer == "Rome")
        }
    }
}
