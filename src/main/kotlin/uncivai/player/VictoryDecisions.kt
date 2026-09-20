package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerVictoryOperations
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class SpaceshipAssembly(val title: String, val available: Boolean, val unavailableReasons: List<String>, val consumesUnit: Boolean = true)

@Serializable
data class DiplomaticCandidate(val civilizationId: String, val name: String)

@Serializable
data class DiplomaticBallot(val turnsUntilNextVote: Int?, val hasVoted: Boolean, val votedFor: String?, val canVote: Boolean, val candidates: List<DiplomaticCandidate>)

@Serializable
data class DiplomaticVoteRow(val civilizationId: String, val name: String, val votes: Int, val votesReceived: Int?, val choice: String, val votedFor: String?)

@Serializable
data class DiplomaticVotingResults(val minimumVotes: Int, val winner: String?, val unmetWinner: Boolean, val rows: List<DiplomaticVoteRow>, val canAcknowledge: Boolean)

@Serializable
data class VictoryDecisions(val ballot: DiplomaticBallot? = null, val votingResults: DiplomaticVotingResults? = null)

@Serializable
data class VictoryOutcome(val part: String? = null, val assembled: Int? = null, val votedFor: String? = null, val abstained: Boolean = false, val acknowledgedResults: DiplomaticVotingResults? = null)

internal fun observeSpaceshipAssembly(player: Civilization, unit: MapUnit, active: Boolean): SpaceshipAssembly? = PlayerVictoryOperations(player).spaceshipPart(unit)?.let {
    SpaceshipAssembly(it.title.tr(), active && it.available, frozen(it.reasons.map { reason -> reason.tr() }))
}

/** Ballots are private until the native results flag is due. Never serialize the raw vote map
 * or the desktop's winner text: either can contain identities the viewer has not met.
 */
internal fun observeVictoryDecisions(player: Civilization, active: Boolean): VictoryDecisions {
    val game = player.gameInfo
    val operations = PlayerVictoryOperations(player)
    val ballot = if (player.getTurnsTillNextDiplomaticVote() != null || player.civID in game.diplomaticVictoryVotesCast) {
        DiplomaticBallot(
            player.getTurnsTillNextDiplomaticVote(),
            player.civID in game.diplomaticVictoryVotesCast,
            game.diplomaticVictoryVotesCast[player.civID],
            active && operations.canVote(),
            frozen(if (player.mayVoteForDiplomaticVictory()) operations.voteCandidates().map { DiplomaticCandidate(it.civilizationId, it.name) } else emptyList()),
        )
    } else {
        null
    }
    val results = if (player.shouldShowDiplomaticVotingResults() || (game.diplomaticVictoryVotesProcessed && gameHasResult(game))) {
        val breakdown = player.victoryManager.getDiplomaticVictoryVoteBreakdown()
        val known = game.civilizations.filter { it === player || player.knows(it) }.map { it.civID }.toSet()
        val unOwner = player.victoryManager.getUNBuildingAndOwnerNames().second
        DiplomaticVotingResults(
            breakdown.minimumVotes,
            breakdown.winnerId?.takeIf { it in known },
            breakdown.winnerId?.let { it !in known } == true,
            frozen(
                game.getCivsSorted(civToSortFirst = player).filter { it.civID in known }.map { civ ->
                    val choice = game.diplomaticVictoryVotesCast[civ.civID]?.let(game::getCivilization)?.takeUnless { it.isDefeated() }
                    DiplomaticVoteRow(
                        civ.civID,
                        civ.civName,
                        if (civ.civID == unOwner) 2 else 1,
                        breakdown.results[civ.civID].takeIf { civ.isMajorCiv() },
                        when {
                            choice == null -> "ABSTAINED"
                            choice.civID !in known -> "UNMET_CIVILIZATION"
                            else -> "VOTED"
                        },
                        choice?.civID?.takeIf { it in known },
                    )
                }.toList(),
            ),
            active && operations.canAcknowledgeResults(),
        )
    } else {
        null
    }
    return VictoryDecisions(ballot, results)
}

internal fun VictoryOutcome.detached() = copy(acknowledgedResults = acknowledgedResults?.let { it.copy(rows = frozen(it.rows)) })

private fun <T> frozen(values: List<T>): List<T> = Collections.unmodifiableList(values.toList())
