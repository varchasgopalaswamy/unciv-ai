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
enum class CityStateVoteWindow { BEFORE_LAST_TURN, LAST_TURN, CLOSED }

@Serializable
data class CityStateVoteTiming(val window: CityStateVoteWindow, val lastInfluenceTurn: Int?, val guidance: String)

@Serializable
data class DiplomaticBallot(
    val turnsUntilNextVote: Int?,
    val hasVoted: Boolean,
    val votedFor: String?,
    val canVote: Boolean,
    val candidates: List<DiplomaticCandidate>,
    val cityStateVoteTiming: CityStateVoteTiming? = null,
)

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

/** Project the current schedule for known city-states without reading their ballots. */
private fun observeCityStateVoteTiming(player: Civilization): CityStateVoteTiming? {
    val game = player.gameInfo
    if (gameHasResult(game)) return null
    val order = game.civilizations
    val current = order.indexOf(game.currentPlayerCiv)
    val viewer = order.indexOf(player)
    val cityStateCountdowns = order.withIndex().mapNotNull { (seat, civ) ->
        if (civ.isCityState && civ.isAI() && !civ.isDefeated() && player.knows(civ)) {
            civ.getTurnsTillNextDiplomaticVote()?.let { seat to it }
        } else null
    }
    if (cityStateCountdowns.isEmpty()) return null

    // Countdown ticks at the start of each city-state's turn. At zero the AI
    // votes for its current ally during that activation, before the next handoff.
    val deadline = cityStateCountdowns.mapNotNull { (seat, countdown) ->
        if (countdown <= 0) return@mapNotNull null
        val voteTurn = game.turns + countdown - if (seat > current) 1 else 0
        if (viewer < seat) voteTurn else voteTurn - 1
    }.minOrNull()
    val nextViewerTurn = game.turns + if (viewer < current) 1 else 0
    val window = when {
        deadline == null || deadline < nextViewerTurn -> CityStateVoteWindow.CLOSED
        deadline == nextViewerTurn -> CityStateVoteWindow.LAST_TURN
        else -> CityStateVoteWindow.BEFORE_LAST_TURN
    }
    val guidance = when (window) {
        CityStateVoteWindow.CLOSED -> "Your window to influence the earliest scheduled votes of known city-states has passed. Gifts can still change current alliances."
        CityStateVoteWindow.LAST_TURN -> "Turn $deadline is your last turn to influence the earliest pending votes of known city-states. They vote for their ally on their own turns, and intervening players can still change alliances."
        CityStateVoteWindow.BEFORE_LAST_TURN -> "Act by turn $deadline to influence the earliest pending votes of known city-states. They vote for their ally on their own turns, which can precede your ballot."
    }
    return CityStateVoteTiming(window, deadline?.takeIf { it >= 0 }, guidance)
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
            observeCityStateVoteTiming(player),
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
