package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.models.ruleset.Policy
import com.unciv.models.translations.tr
import com.unciv.ui.objectdescriptions.TechnologyDescriptions
import kotlinx.serialization.Serializable
import kotlin.math.ceil

@Serializable
data class TechnologyUnlocks(
    val units: List<String>,
    val buildings: List<String>,
    val wonders: List<String>,
    val improvements: List<String>,
    val revealsResources: List<String>,
    val effects: List<String>,
)

internal fun technologyUnlocks(player: Civilization, name: String): TechnologyUnlocks {
    val rules = player.gameInfo.ruleset
    val tech = rules.technologies.getValue(name)
    val buildings = TechnologyDescriptions.getEnabledBuildings(name, rules, player).toList()
    return TechnologyUnlocks(
        TechnologyDescriptions.getEnabledUnits(name, rules, player).map { it.name }.sorted().toList(),
        buildings.filterNot { it.isAnyWonder() }.map { it.name }.sorted(),
        buildings.filter { it.isAnyWonder() }.map { it.name }.sorted(),
        rules.tileImprovements.values.filter { it.techRequired == name && (it.uniqueTo == null || player.matchesFilter(it.uniqueTo!!)) }.map { it.name }.sorted(),
        rules.tileResources.values.filter { it.revealedBy == name }.map { it.name }.sorted(),
        tech.uniqueObjects.filterNot { it.isHiddenToUsers() }.map { it.getDisplayText().tr() } +
            TechnologyDescriptions.getAffectedImprovements(name, rules).filter { it.improvement.uniqueTo == null || player.matchesFilter(it.improvement.uniqueTo!!) }.map { it.getText().tr() },
    )
}

@Serializable
data class PolicyBranchProgress(val name: String, val adopted: List<String>, val remaining: List<String>, val complete: Boolean)

@Serializable
data class CultureOverview(
    val storedCulture: Int,
    val culturePerTurn: Int,
    val nextPolicyCost: Int,
    val turnsToNextPolicy: Int?,
    val branches: List<PolicyBranchProgress>,
    val estimateBasis: String = "Policy time uses the current culture rate and cost; expansion, free policies and future rate changes can alter it.",
)

internal fun observeCulture(player: Civilization): CultureOverview {
    val manager = player.policies
    // TurnManager credits the truncated integer rate to stored culture.
    val rate = player.stats.statsForNextTurn.culture.toInt()
    val remaining = manager.getCultureNeededForNextPolicy() - manager.storedCulture
    return CultureOverview(
        manager.storedCulture,
        rate,
        manager.getCultureNeededForNextPolicy(),
        if (manager.freePolicies > 0 || remaining <= 0) {
            0
        } else if (rate > 0) {
            ceil(remaining.toDouble() / rate).toInt()
        } else {
            null
        },
        manager.branches.sortedBy { it.name }.filter { manager.isAdopted(it.name) }.map { branch ->
            val members = branch.policies.filter { it.policyBranchType != Policy.PolicyBranchType.BranchComplete }
            PolicyBranchProgress(
                branch.name,
                members.filter { manager.isAdopted(it.name) }.map { it.name }.sorted(),
                members.filterNot { manager.isAdopted(it.name) }.map { it.name }.sorted(),
                branch in manager.completedBranches,
            )
        },
    )
}

@Serializable
data class GreatPersonProgress(val name: String, val points: Int, val pointsPerTurn: Int, val threshold: Int, val turnsAtCurrentRate: Int?)

internal fun observeGreatPeopleProgress(player: Civilization): List<GreatPersonProgress> {
    val manager = player.greatPeople
    val rates = manager.getGreatPersonPointsForNextTurn()
    return (rates.keys + manager.greatPersonPointsCounter.keys).sorted().filter { it in player.gameInfo.ruleset.units }.map { name ->
        val points = manager.greatPersonPointsCounter[name]
        val threshold = manager.getPointsRequiredForGreatPerson(name)
        val rate = rates[name]
        GreatPersonProgress(
            name,
            points,
            rate,
            threshold,
            if (points >= threshold) {
                0
            } else if (rate > 0) {
                ceil((threshold - points).toDouble() / rate).toInt()
            } else {
                null
            },
        )
    }
}
