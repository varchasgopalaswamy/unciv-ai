package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.models.ruleset.Milestone
import com.unciv.models.ruleset.MilestoneType
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class VictoryPartProgress(val name: String, val completed: Int, val required: Int)

@Serializable
data class VictoryMilestoneProgress(val description: String, val display: String, val status: String, val parts: List<VictoryPartProgress>, val objectives: List<VictoryObjectiveProgress>, val objectiveRequirement: VictoryObjectiveRequirement? = null)

@Serializable
data class VictoryObjectiveRequirement(val rule: String, val requiredCount: Int, val completedCount: Int)

@Serializable
data class VictoryObjectiveProgress(val kind: String, val name: String?, val completed: Boolean)

@Serializable
data class VictoryPathProgress(val name: String, val description: String, val milestones: List<VictoryMilestoneProgress>)

@Serializable
data class RivalVictoryProgress(val civilization: String, val defeated: Boolean, val nextMilestones: Map<String, String>)

@Serializable
data class VictoryProgress(
    val turn: Int,
    val maxTurns: Int?,
    val sections: List<String>,
    val availability: Map<String, VictorySectionAvailability>,
    val victories: List<VictoryPathProgress>,
    val rivals: List<RivalVictoryProgress>,
    val metrics: List<VictoryMetric>,
    val rankings: VictoryRankings?,
    val charts: VictoryCharts?,
    val demographics: VictoryDemographics?,
    val milestoneBasis: String = "Current and nextMilestones identify the next unmet victory requirement, not a project under construction or an estimated finish. Completed predicates use native victory-screen information; message claims and directly visible construction are separate evidence.",
)

/** Pure milestone predicates and text shared with the victory screen; no UI actors or hidden rival details. */
internal fun observeVictoryProgress(player: Civilization, turn: PlayerTurnSnapshot, request: VictoryProgressRequest): VictoryProgress {
    validateVictoryRequest(player, request)
    val enabled = player.gameInfo.getEnabledVictories()
    val selected = enabled.filterKeys { request.names.isEmpty() || it in request.names }
    val availability = victorySectionAvailability(player)
    fun includes(section: String) = section in request.sections && availability.getValue(section).available
    val metrics = selectedVictoryMetrics(player, request)
    return VictoryProgress(
        turn = turn.turn,
        maxTurns = turn.rules.maxTurns,
        sections = frozenVictory(request.sections.toList()),
        availability = availability,
        victories = frozenVictory(
            (if (includes("our_status")) selected else emptyMap()).map { (name, victory) ->
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
                                    // Part names/counts are public victory requirements,
                                    // useful for planning before Apollo is completed.
                                    if (milestone.type == MilestoneType.AddedSSPartsInCapital) {
                                        victory.requiredSpaceshipPartsAsCounter.map { (part, required) ->
                                            VictoryPartProgress(part, player.victoryManager.currentsSpaceshipParts[part], required)
                                        }
                                    } else {
                                        emptyList()
                                    },
                                ),
                                if (current && !completed) observeVictoryObjectives(player, milestone) else emptyList(),
                                if (milestone.type == MilestoneType.CompletePolicyBranches) {
                                    VictoryObjectiveRequirement("AT_LEAST", milestone.params[0].toInt(), player.policies.completedBranches.size)
                                } else {
                                    null
                                },
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
                includes("global_status") && it !== player && it.isMajorCiv() && canIdentifyVictoryCivilization(player, it) &&
                    (request.civilizations.isEmpty() || it.civName in request.civilizations)
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
        metrics = if (listOf("rankings", "charts", "demographics").any { includes(it) }) frozenVictory(metrics.map { victoryMetric(it) }) else emptyList(),
        rankings = if (includes("rankings")) observeVictoryRankings(player, metrics, request) else null,
        charts = if (includes("charts")) observeVictoryCharts(player, metrics, request) else null,
        demographics = if (includes("demographics")) observeVictoryDemographics(player, metrics) else null,
    )
}

/** Vanilla milestone substeps, with the same exploration gates as Our status. */
private fun observeVictoryObjectives(player: Civilization, milestone: Milestone): List<VictoryObjectiveProgress> = frozenVictory(
    when (milestone.type) {
        MilestoneType.CompletePolicyBranches -> player.gameInfo.ruleset.policyBranches.values.map {
            val finisher = it.policies.last().name
            VictoryObjectiveProgress("POLICY_BRANCH", it.name, player.policies.isAdopted(finisher))
        }

        MilestoneType.CaptureAllCapitals -> {
            val capitals = player.gameInfo.getCities().filter { it.isOriginalCapital }.associateBy { it.foundingCivObject }
            player.gameInfo.civilizations.filter { it.isMajorCiv() }.mapNotNull { other ->
                val capital = capitals[other]
                if (capital != null) {
                    val known = player.hasExplored(capital.getCenterTile())
                    if (!known && player.shouldHideCivCount()) {
                        null
                    } else {
                        VictoryObjectiveProgress("CAPITAL", capital.name.takeIf { known }, capital.civ == player)
                    }
                } else {
                    val known = canIdentifyVictoryCivilization(player, other)
                    if (!known && player.shouldHideCivCount()) {
                        null
                    } else {
                        VictoryObjectiveProgress("DEFEAT_CIVILIZATION", other.civName.takeIf { known }, other.isDefeated())
                    }
                }
            }.sortedWith(compareBy<VictoryObjectiveProgress> { it.kind }.thenBy { it.name ?: "" }.thenBy { it.completed })
        }

        MilestoneType.DestroyAllPlayers -> player.gameInfo.civilizations.filter {
            it !== player && it.isMajorCiv() && (canIdentifyVictoryCivilization(player, it) || !player.shouldHideCivCount())
        }.map {
            VictoryObjectiveProgress("DEFEAT_CIVILIZATION", it.civName.takeIf { _ -> canIdentifyVictoryCivilization(player, it) }, it.isDefeated())
        }.sortedBy { it.name ?: "" }

        else -> emptyList()
    },
)

private fun <T> frozenVictory(values: List<T>): List<T> = Collections.unmodifiableList(values.toList())
