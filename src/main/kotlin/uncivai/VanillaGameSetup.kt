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
import com.unciv.models.ruleset.RulesetCache

/** Ordinary engine creation, with human slots so external players retain control. */
internal object VanillaGameSetup {
    fun create(seed: Long): GameInfo {
        val parameters = GameParameters().apply {
            baseRuleset = BaseRuleset.Civ_V_Vanilla.fullName
            difficulty = "Prince"
            speed = "Quick"
            startingEra = "Ancient era"
            numberOfCityStates = 0
            shufflePlayerOrder = false
            players = arrayListOf(Player("Rome", PlayerType.Human), Player("Greece", PlayerType.Human), Player("France", PlayerType.Human))
            val ruleset = RulesetCache.getComplexRuleset(this)
            victoryTypes = ArrayList(ruleset.victories.keys)
            maxTurns = ruleset.speeds.getValue(speed).numTotalTurns()
        }
        val map = MapParameters().apply {
            baseRuleset = parameters.baseRuleset
            type = MapType.pangaea
            mapSize = MapSize.Small
            this.seed = seed
        }
        return GameStarter.startNewGame(GameSetupInfo(parameters, map))
    }
}
