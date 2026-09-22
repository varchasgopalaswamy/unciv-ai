package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerOperations
import com.unciv.models.ruleset.Policy
import com.unciv.models.ruleset.tech.Technology
import com.unciv.models.ruleset.unique.GameContext
import com.unciv.models.ruleset.unique.UniqueType
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.util.Collections
import kotlin.math.roundToInt

@Serializable
data class DecisionAvailability(val available: Boolean, val reasons: List<String>)

@Serializable
data class ResearchDetail(
    val name: String,
    val era: String,
    val prerequisites: List<String>,
    val researched: Boolean,
    val repeatable: Boolean,
    val cost: Int,
    val scienceStored: Int,
    val researchNow: DecisionAvailability,
    val freeChoice: DecisionAvailability,
    val prerequisiteQueue: List<String>?,
    val queueAvailable: Boolean?,
    val description: String?,
    val gameplayDescription: String? = null,
)

@Serializable
data class ResearchDetails(
    val epoch: String,
    val revision: String,
    val current: String?,
    val freeTechnologies: Int,
    val technologies: List<ResearchDetail>,
    val researchQueue: List<String> = emptyList(),
)

@Serializable
data class PolicyDetail(
    val name: String,
    val branch: String,
    val kind: String,
    val era: String,
    val prerequisites: List<String>,
    val adopted: Boolean,
    val adoption: DecisionAvailability,
    val description: String?,
    val branchCompletionDescription: String?,
)

@Serializable
data class PolicyDetails(
    val epoch: String,
    val revision: String,
    val storedCulture: Int,
    val culturePerTurn: Int,
    val nextPolicyCost: Int,
    val freePolicies: Int,
    val policies: List<PolicyDetail>,
)

/** Empty names produce a catalogue; named requests include desktop descriptions and research paths. */
internal fun observeResearchDetails(player: Civilization, turn: PlayerTurnSnapshot, names: List<String>): ResearchDetails {
    val rules = player.gameInfo.ruleset.technologies
    validateDetailNames(names, rules.keys)
    val active = canUseDetails(turn)
    return ResearchDetails(
        turn.epoch,
        turn.revision,
        player.tech.currentTechnologyName(),
        player.tech.freeTechs,
        frozenDetails(
            (if (names.isEmpty()) rules.keys.sorted() else names).map { name ->
                val tech = rules.getValue(name)
                val reasons = ArrayList<String>()
                if (!active) reasons.add("This player cannot act now")
                if (player.tech.isResearched(name) && !tech.isContinuallyResearchable()) reasons.add("Already researched")
                val missing = tech.prerequisites.filterNot(player.tech::isResearched).sorted()
                if (missing.isNotEmpty()) reasons.add("Requires: ${missing.joinToString(", ")}")
                for (unique in tech.getMatchingUniques(UniqueType.OnlyAvailable, GameContext.IgnoreConditionals).filterNot { it.conditionalsApply(player.state) }) reasons.add(unique.getDisplayText())
                if (tech.hasUnique(UniqueType.Unavailable, player.state)) reasons.add("Technology requirements are not met")
                val queue = if (names.isEmpty()) null else frozenDetails(player.tech.getRequiredTechsToDestination(tech).map { it.name })
                ResearchDetail(
                    name, tech.era(), frozenDetails(tech.prerequisites.sorted()), player.tech.isResearched(name), tech.isContinuallyResearchable(),
                    player.tech.costOfTech(name), player.tech.researchOfTech(name),
                    availability(reasons + if (player.tech.freeTechs > 0) listOf("Choose your free technology first") else emptyList()),
                    availability(reasons + if (player.tech.freeTechs == 0) listOf("No free technology choice is pending") else emptyList()),
                    queue, queue?.let { active && player.tech.freeTechs == 0 && it.isNotEmpty() },
                    if (names.isEmpty()) null else tech.getDescription(player),
                    if (names.isEmpty()) null else tech.gameplayDescription(player),
                )
            },
        ),
        researchQueue = frozenDetails(player.tech.techsToResearch),
    )
}

/** The engine prepends these exact Civilopedia lines to its mechanical description.
 * Strip only that source-identified prefix, without changing the ruleset or guessing
 * which effect descriptions are prose. Desktop/audit descriptions stay intact.
 */
internal fun Technology.gameplayDescription(player: Civilization): String {
    val description = getDescription(player)
    val prefix = civilopediaText.filter { it.text.isNotEmpty() && it.header == 0 }.joinToString("\n") { it.text.tr() }
    if (prefix.isEmpty()) return description
    return if (description == prefix) "" else description.removePrefix("$prefix\n")
}

internal fun observePolicyDetails(player: Civilization, turn: PlayerTurnSnapshot, names: List<String>): PolicyDetails {
    val rules = player.gameInfo.ruleset.policies
    validateDetailNames(names, rules.keys)
    val manager = player.policies
    val active = canUseDetails(turn)
    return PolicyDetails(
        turn.epoch,
        turn.revision,
        manager.storedCulture,
        player.stats.statsForNextTurn.culture.roundToInt(),
        manager.getCultureNeededForNextPolicy(),
        manager.freePolicies,
        frozenDetails(
            (if (names.isEmpty()) rules.keys.sorted() else names).map { name ->
                val policy = rules.getValue(name)
                val reasons = ArrayList<String>()
                if (!active) reasons.add("This player cannot act now")
                if (manager.isAdopted(name)) reasons.add("Already adopted")
                if (policy.policyBranchType == Policy.PolicyBranchType.BranchComplete) reasons.add("Granted automatically when the branch is completed")
                val requires = policy.requires.orEmpty()
                val missing = requires.filterNot(manager::isAdopted)
                if (missing.isNotEmpty()) reasons.add("Requires: ${missing.joinToString(", ")}")
                if (player.gameInfo.ruleset.eras.getValue(policy.branch.era).eraNumber > player.getEraNumber()) reasons.add("Unlocked in ${policy.branch.era}")
                if (manager.freePolicies == 0 && manager.storedCulture < manager.getCultureNeededForNextPolicy()) reasons.add("Not enough culture")
                for (unique in policy.getMatchingUniques(UniqueType.OnlyAvailable, GameContext.IgnoreConditionals).filterNot { it.conditionalsApply(player.state) }) reasons.add(unique.getDisplayText())
                if (policy.hasUnique(UniqueType.Unavailable, player.state)) reasons.add("Policy requirements are not met")
                val available = active && PlayerOperations(player).canAdoptPolicy(name)
                if (!available && reasons.isEmpty()) reasons.add("This policy cannot be adopted now")
                PolicyDetail(
                    name, policy.branch.name, policy.policyBranchType.name, policy.branch.era, frozenDetails(requires), manager.isAdopted(name),
                    DecisionAvailability(available, frozenDetails(reasons.map { it.tr() })),
                    if (names.isEmpty()) null else policy.getDescription(),
                    if (names.isNotEmpty() && policy.policyBranchType == Policy.PolicyBranchType.BranchStart) policy.branch.policies.last().getDescription() else null,
                )
            },
        ),
    )
}

private fun canUseDetails(turn: PlayerTurnSnapshot) = turn.phase == "ACTIVE" && turn.result == null && turn.alerts.none { it.capture != null }

private fun validateDetailNames(names: List<String>, available: Set<String>) {
    if (names.size > 16 || names.any { it.isBlank() || it.length > 256 } || names.distinct().size != names.size) throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
    if (names.any { it !in available }) throw PlayerSessionException(PlayerSessionFailure.OPTION_UNAVAILABLE)
}

private fun availability(reasons: List<String>) = DecisionAvailability(reasons.isEmpty(), frozenDetails(reasons.map { it.tr() }))
private fun <T> frozenDetails(values: Collection<T>): List<T> = Collections.unmodifiableList(ArrayList(values))
