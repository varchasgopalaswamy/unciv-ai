package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.models.ruleset.MilestoneType
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class VictoryPartProgress(val name: String, val completed: Int, val required: Int)

@Serializable
data class VictoryMilestoneProgress(val description: String, val display: String, val status: String, val parts: List<VictoryPartProgress>)

@Serializable
data class VictoryPathProgress(val name: String, val description: String, val milestones: List<VictoryMilestoneProgress>)

@Serializable
data class RivalVictoryProgress(val civilization: String, val defeated: Boolean, val nextMilestones: Map<String, String>)

@Serializable
data class VictoryProgress(val turn: Int, val score: Int, val maxTurns: Int?, val victories: List<VictoryPathProgress>, val rivals: List<RivalVictoryProgress>)

/** Pure milestone predicates and text shared with the victory screen; no UI actors or hidden rival details. */
internal fun observeVictoryProgress(player: Civilization, turn: PlayerTurnSnapshot, names: List<String>): VictoryProgress {
    val enabled = player.gameInfo.getEnabledVictories()
    if (names.size > 16 || names.any { it !in enabled }) throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
    val selected = enabled.filterKeys { names.isEmpty() || it in names }
    return VictoryProgress(
        turn = turn.turn,
        // RankingType.Score displays an integer, not the underlying fractional score.
        score = player.calculateTotalScore().toInt(),
        maxTurns = turn.rules.maxTurns,
        victories = frozenVictory(
            selected.map { (name, victory) ->
                var firstIncomplete = true
                VictoryPathProgress(
                    name,
                    victory.victoryScreenHeader.tr(),
                    frozenVictory(
                        victory.milestoneObjects.map { milestone ->
                            val completed = milestone.hasBeenCompletedBy(player)
                            val current = !completed && firstIncomplete
                            if (current) firstIncomplete = false
                            VictoryMilestoneProgress(
                                milestone.uniqueDescription.tr(),
                                milestone.getVictoryScreenButtonHeaderText(completed, player).tr(),
                                if (completed) {
                                    "Completed"
                                } else if (current) {
                                    "Current"
                                } else {
                                    "Incomplete"
                                },
                                frozenVictory(
                                    if (current && milestone.type == MilestoneType.AddedSSPartsInCapital) {
                                        victory.requiredSpaceshipPartsAsCounter.map { (part, required) ->
                                            VictoryPartProgress(part, player.victoryManager.currentsSpaceshipParts[part], required)
                                        }
                                    } else {
                                        emptyList()
                                    },
                                ),
                            )
                        },
                    ),
                )
            },
        ),
        // VictoryScreenCivGroup conceals additionalInfo for living unmet rivals.
        // Filter before ordering or evaluating progress to avoid leaking their ranking.
        rivals = frozenVictory(
            player.gameInfo.civilizations.filter {
                it !== player && it.isMajorCiv() && (player.knows(it) || it.isDefeated() || player.isDefeated())
            }.sortedBy { it.civName }.map { other ->
                RivalVictoryProgress(
                    other.civName,
                    other.isDefeated(),
                    Collections.unmodifiableMap(
                        selected.mapValues { (_, victory) ->
                            (other.victoryManager.getNextMilestone(victory)?.getVictoryScreenButtonHeaderText(false, other) ?: "Done!").tr()
                        },
                    ),
                )
            },
        ),
    )
}

private fun <T> frozenVictory(values: List<T>): List<T> = Collections.unmodifiableList(values.toList())
