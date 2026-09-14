package uncivai.player

import com.unciv.logic.GameInfo
import kotlinx.serialization.Serializable
import java.util.Collections

/** Public match settings, available equally to every player. */
@Serializable
data class GameRules(
    val ruleset: String,
    val difficulty: String,
    val speed: String,
    val victoryTypes: List<String>,
    val maxTurns: Int?,
)

internal fun observeGameRules(game: GameInfo): GameRules = game.gameParameters.let { parameters ->
    GameRules(
        ruleset = parameters.baseRuleset,
        difficulty = parameters.difficulty,
        speed = parameters.speed,
        victoryTypes = Collections.unmodifiableList(ArrayList(parameters.victoryTypes)),
        maxTurns = parameters.maxTurns.takeIf {
            parameters.victoryTypes.any { game.ruleset.victories[it]?.enablesMaxTurns() == true }
        },
    )
}
