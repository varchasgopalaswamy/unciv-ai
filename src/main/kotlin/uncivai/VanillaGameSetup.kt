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
    fun create(seed: Long, cityStates: Int = 0, builtInAI: List<String> = emptyList()): GameInfo {
        val parameters = GameParameters().apply {
            baseRuleset = BaseRuleset.Civ_V_Vanilla.fullName
            difficulty = "Prince"
            speed = "Quick"
            startingEra = "Ancient era"
            numberOfCityStates = cityStates
            shufflePlayerOrder = false
            players = arrayListOf(Player("Rome", PlayerType.Human), Player("Greece", PlayerType.Human), Player("France", PlayerType.Human))
            val ruleset = RulesetCache.getComplexRuleset(this)
            require(builtInAI.distinct().size == builtInAI.size && builtInAI.all { it !in setOf("Rome", "Greece", "France") && ruleset.nations[it]?.isMajorCiv == true }) { "Built-in AI civilizations must be distinct major nations other than Rome, Greece and France" }
            players.addAll(builtInAI.map { Player(it, PlayerType.AI) })
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
