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

data class VanillaOptions(
    val mapType: String = MapType.pangaea,
    val difficulty: String = "Prince",
    val speed: String = "Quick",
    val startingEra: String = "Ancient era",
    val victories: List<String>? = null,
)

/** Ordinary engine creation, with human slots so external players retain control. */
internal object VanillaGameSetup {
    fun create(
        seed: Long,
        cityStates: Int = 0,
        builtInAI: List<String> = emptyList(),
        agentPlayers: List<String> = listOf("Rome", "Greece", "France"),
        size: MapSize = MapSize.Small,
        options: VanillaOptions = VanillaOptions(),
    ): GameInfo {
        val parameters = GameParameters().apply {
            baseRuleset = BaseRuleset.Civ_V_Vanilla.fullName
            difficulty = options.difficulty
            speed = options.speed
            startingEra = options.startingEra
            numberOfCityStates = cityStates
            shufflePlayerOrder = false
            val ruleset = RulesetCache.getComplexRuleset(this)
            require(difficulty in ruleset.difficulties) { "Unknown difficulty: $difficulty" }
            require(speed in ruleset.speeds) { "Unknown game speed: $speed" }
            require(startingEra in ruleset.eras) { "Unknown starting era: $startingEra" }
            require(options.mapType in MapType.allValues) { "Unknown generated map type: ${options.mapType}" }
            require(options.victories == null || (options.victories.isNotEmpty() && options.victories.all { it in ruleset.victories })) {
                "Enabled victories must be native Vanilla victory names"
            }
            val nations = agentPlayers + builtInAI
            require(agentPlayers.isNotEmpty() && nations.distinct().size == nations.size && nations.all { ruleset.nations[it]?.isMajorCiv == true }) {
                "Players must be distinct major nations, with at least one externally controlled player"
            }
            players = ArrayList(agentPlayers.map { Player(it, PlayerType.Human) })
            players.addAll(builtInAI.map { Player(it, PlayerType.AI) })
            victoryTypes = ArrayList(options.victories ?: ruleset.victories.keys)
            maxTurns = ruleset.speeds.getValue(speed).numTotalTurns()
        }
        val map = MapParameters().apply {
            baseRuleset = parameters.baseRuleset
            type = options.mapType
            mapSize = size
            this.seed = seed
        }
        return GameStarter.startNewGame(GameSetupInfo(parameters, map))
    }
}
