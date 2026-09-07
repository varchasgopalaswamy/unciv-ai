package uncivai.player

import com.unciv.logic.GameInfo
import com.unciv.logic.city.City
import com.unciv.logic.civilization.Civilization
import uncivai.HeadlessRuntime
import uncivai.RuntimeClosedException
import uncivai.RuntimeHaltedException
import java.util.Collections
import java.util.UUID

/** Trusted host surface that assigns a fixed player identity to each session. */
class PlayerHost internal constructor(
    private val runtime: HeadlessRuntime,
    private val cityRenamer: (City, String) -> Unit,
) {
    constructor(runtime: HeadlessRuntime) : this(runtime, { city, name -> city.name = name })

    fun bindPlayer(civilizationId: String): PlayerSession = playerOperation {
        runtime.accessGame { game ->
            game.findAvailablePlayer(civilizationId)
                ?: throw PlayerSessionException(PlayerSessionFailure.PLAYER_UNAVAILABLE)
        }
        BoundPlayerSession(runtime, civilizationId, cityRenamer)
    }
}

interface PlayerSession {
    fun summary(): PlayerSummary

    fun cities(): OwnedCitiesSnapshot

    fun renameCity(request: RenameCityRequest): RenameCityReceipt
}

private class BoundPlayerSession(
    private val runtime: HeadlessRuntime,
    private val civilizationId: String,
    private val cityRenamer: (City, String) -> Unit,
) : PlayerSession {
    private val cityIdsByHandle = HashMap<String, String>()
    private val handlesByCityId = HashMap<String, String>()
    private val renameReceipts = HashMap<String, CompletedRename>()

    override fun summary(): PlayerSummary = playerOperation {
        runtime.accessGame { game ->
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

    override fun cities(): OwnedCitiesSnapshot = playerOperation {
        runtime.accessGame { game ->
            val player = game.findAvailablePlayer(civilizationId)
                ?: throw PlayerSessionException(PlayerSessionFailure.PLAYER_UNAVAILABLE)
            val cities = player.cities
                .asSequence()
                .filter { city -> city.civ === player }
                .map { city ->
                    OwnedCitySummary(
                        handle = handleFor(city.id),
                        name = city.name,
                    )
                }
                .toList()

            OwnedCitiesSnapshot(
                epoch = runtime.epoch,
                cities = Collections.unmodifiableList(cities),
            )
        }
    }

    override fun renameCity(request: RenameCityRequest): RenameCityReceipt = playerOperation {
        runtime.accessGame { game ->
            if (request.epoch != runtime.epoch) {
                throw PlayerSessionException(PlayerSessionFailure.STALE_EPOCH)
            }
            if (!request.hasValidRequestId()) {
                throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
            }
            val player = game.findAvailablePlayer(civilizationId)
                ?: throw PlayerSessionException(PlayerSessionFailure.PLAYER_UNAVAILABLE)

            val completed = renameReceipts[request.requestId]
            if (completed != null) {
                if (completed.request != request) {
                    throw PlayerSessionException(PlayerSessionFailure.REQUEST_ID_CONFLICT)
                }
                return@accessGame completed.receipt
            }
            if (!request.hasValidRenameArguments()) {
                throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
            }

            if (game.currentPlayer != player.civID || game.currentPlayerCiv !== player) {
                throw PlayerSessionException(PlayerSessionFailure.NOT_YOUR_TURN)
            }

            val cityId = cityIdsByHandle[request.cityHandle]
                ?: throw PlayerSessionException(PlayerSessionFailure.CITY_UNAVAILABLE)
            val city = player.cities.singleOrNull {
                it.id == cityId && it.civ === player
            } ?: throw PlayerSessionException(PlayerSessionFailure.CITY_UNAVAILABLE)
            val receipt = RenameCityReceipt(
                requestId = request.requestId,
                epoch = runtime.epoch,
                cityHandle = request.cityHandle,
                previousName = city.name,
                name = request.name,
            )
            val completedRename = CompletedRename(request, receipt)

            try {
                cityRenamer(city, request.name)
                renameReceipts[request.requestId] = completedRename
            } catch (failure: Throwable) {
                runtime.haltAfterMutation(failure)
            }
            receipt
        }
    }

    private fun handleFor(cityId: String): String = handlesByCityId.getOrPut(cityId) {
        var handle: String
        do {
            handle = UUID.randomUUID().toString()
        } while (handle in cityIdsByHandle)
        cityIdsByHandle[handle] = cityId
        handle
    }
}

private data class CompletedRename(
    val request: RenameCityRequest,
    val receipt: RenameCityReceipt,
)

private fun RenameCityRequest.hasValidRequestId(): Boolean = requestId.length in 1..128 &&
    requestId.all { character -> character.code in 0x20..0x7e }

private fun RenameCityRequest.hasValidRenameArguments(): Boolean = name.isNotEmpty() &&
    name.length <= 32 &&
    name.none { character -> character in "[]{}\"\\<>\r\n" }

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
} catch (_: RuntimeHaltedException) {
    throw PlayerSessionException(PlayerSessionFailure.GAME_HALTED)
} catch (_: Exception) {
    throw PlayerSessionException(PlayerSessionFailure.QUERY_FAILED)
}
