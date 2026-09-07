package uncivai.player

import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.Civilization
import uncivai.HeadlessRuntime
import uncivai.RuntimeClosedException

/** Trusted host surface that assigns a fixed player identity to each session. */
class PlayerHost(
    private val runtime: HeadlessRuntime,
) {
    fun bindPlayer(civilizationId: String): PlayerSession = playerOperation {
        runtime.readGame { game ->
            game.findAvailablePlayer(civilizationId)
                ?: throw PlayerSessionException(PlayerSessionFailure.PLAYER_UNAVAILABLE)
        }
        BoundPlayerSession(runtime, civilizationId)
    }
}

interface PlayerSession {
    fun summary(): PlayerSummary
}

private class BoundPlayerSession(
    private val runtime: HeadlessRuntime,
    private val civilizationId: String,
) : PlayerSession {
    override fun summary(): PlayerSummary = playerOperation {
        runtime.readGame { game ->
            val player = game.findAvailablePlayer(civilizationId)
                ?: throw PlayerSessionException(PlayerSessionFailure.PLAYER_UNAVAILABLE)

            PlayerSummary(
                civilization = player.civName,
                ruleset = game.gameParameters.baseRuleset,
                turn = game.turns,
                isYourTurn = game.currentPlayer == player.civID,
                treasury = player.gold,
                cityCount = player.cities.size,
                unitCount = player.units.getCivUnitsSize(),
                currentResearch = player.tech.currentTechnologyName(),
                freePolicies = player.policies.freePolicies,
                freeTechnologies = player.tech.freeTechs,
                freeGreatPeople = player.greatPeople.freeGreatPeople,
            )
        }
    }
}

private fun GameInfo.findAvailablePlayer(civilizationId: String): Civilization? = civilizations.singleOrNull { civilization ->
    civilization.civID == civilizationId &&
        civilization.isHuman() &&
        civilization.isMajorCiv()
}

private inline fun <T> playerOperation(operation: () -> T): T = try {
    operation()
} catch (failure: PlayerSessionException) {
    throw failure
} catch (_: RuntimeClosedException) {
    throw PlayerSessionException(PlayerSessionFailure.SESSION_CLOSED)
} catch (_: Exception) {
    throw PlayerSessionException(PlayerSessionFailure.QUERY_FAILED)
}
