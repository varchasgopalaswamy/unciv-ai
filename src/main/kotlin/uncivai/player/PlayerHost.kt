package uncivai.player

import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.Civilization
import uncivai.GameExecutor
import uncivai.HeadlessRuntime
import uncivai.PlayerApiAccounting
import uncivai.RuntimeClosedException
import uncivai.RuntimeHaltedException
import uncivai.RuntimePausedException
import java.util.Collections

/** Trusted host surface that assigns a fixed player identity to each session. */
class PlayerHost(private val runtime: GameExecutor) {
    constructor(runtime: HeadlessRuntime) : this(runtime.executor)

    fun bindPlayer(civilizationId: String): PlayerSession = playerOperation {
        runtime.accessGame { game ->
            game.findAvailablePlayer(civilizationId)
                ?: throw PlayerSessionException(PlayerSessionFailure.PLAYER_UNAVAILABLE)
        }
        BoundPlayerSession(runtime, civilizationId)
    }
}

interface PlayerSession {
    fun communicationChanges(): kotlinx.coroutines.flow.StateFlow<Long> = throw PlayerSessionException(PlayerSessionFailure.COMMAND_UNAVAILABLE)
    fun communication(request: kotlinx.serialization.json.JsonObject): kotlinx.serialization.json.JsonObject = throw PlayerSessionException(PlayerSessionFailure.COMMAND_UNAVAILABLE)
    fun capabilities(): PlayerCapabilities

    fun turn(): PlayerTurnSnapshot

    fun result(): PlayerFinalState

    fun missionPreview(request: MissionPreviewRequest): MissionPreview

    fun movementPreview(request: MovementPreviewRequest): MovementPreview

    fun inspect(request: PlayerInspectionRequest): PlayerInspection

    fun researchDetails(names: List<String> = emptyList()): ResearchDetails

    fun policyDetails(names: List<String> = emptyList()): PolicyDetails

    fun victoryProgress(request: VictoryProgressRequest = VictoryProgressRequest()): VictoryProgress

    /** Read a receipt only when every field matches this player's completed request. */
    fun completedReceipt(request: TurnCommandRequest): TurnCommandReceipt?

    fun execute(request: TurnCommandRequest): TurnCommandReceipt

    fun summary(): PlayerSummary

    fun cities(): OwnedCitiesSnapshot

    fun freeGreatPersonChoice(): FreeGreatPersonSnapshot
}

private class BoundPlayerSession(
    private val runtime: GameExecutor,
    private val civilizationId: String,
) : PlayerSession,
    PlayerApiAccounting {
    private val turnSession = TurnSession(runtime, civilizationId)

    override fun beginApiCall(tool: String, arguments: kotlinx.serialization.json.JsonObject?) = playerOperation { runtime.telemetry.startCall(civilizationId, tool, arguments) }
    override fun finishApiCall(id: String, outcome: String) = runtime.telemetry.finishCall(id, outcome)

    override fun communicationChanges() = playerOperation {
        runtime.accessGame { runtime.communicationChanges.forPlayer(civilizationId) }
    }

    override fun communication(request: kotlinx.serialization.json.JsonObject) = playerOperation {
        runtime.communication.handle(civilizationId, request)
    }

    override fun capabilities() = playerOperation {
        runtime.accessGame {
            PlayerCapabilities(durableRecovery = runtime.durableRecovery)
        }
    }

    override fun turn() = playerOperation { turnSession.snapshot() }

    override fun result() = playerOperation { turnSession.snapshot().finalState(civilizationId) }

    override fun movementPreview(request: MovementPreviewRequest) = playerOperation { turnSession.movementPreview(request) }

    override fun inspect(request: PlayerInspectionRequest) = playerOperation { turnSession.inspect(request) }

    override fun missionPreview(request: MissionPreviewRequest) = playerOperation { turnSession.missionPreview(request) }

    override fun researchDetails(names: List<String>) = playerOperation { turnSession.researchDetails(names) }

    override fun policyDetails(names: List<String>) = playerOperation { turnSession.policyDetails(names) }

    override fun victoryProgress(request: VictoryProgressRequest) = playerOperation { turnSession.victoryProgress(request) }

    override fun completedReceipt(request: TurnCommandRequest) = playerOperation { turnSession.completedReceipt(request) }

    override fun execute(request: TurnCommandRequest) = playerOperation { turnSession.execute(request) }

    private fun state(): TurnSessionState = runtime.turnSessions.getOrPut(civilizationId) { TurnSessionState() }

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
            val state = state()
            val previousHandles = state.cityHandles.size
            val cities = player.cities
                .asSequence()
                .filter { city -> city.civ === player }
                .map { city ->
                    OwnedCitySummary(
                        handle = state.cityHandle(city.id),
                        name = city.name,
                    )
                }
                .toList()

            if (previousHandles != state.cityHandles.size) runtime.commitCheckpoint()

            OwnedCitiesSnapshot(
                epoch = runtime.epoch,
                cities = Collections.unmodifiableList(cities),
            )
        }
    }

    override fun freeGreatPersonChoice(): FreeGreatPersonSnapshot = playerOperation {
        runtime.accessGame { game ->
            val player = game.findAvailablePlayer(civilizationId)
                ?: throw PlayerSessionException(PlayerSessionFailure.PLAYER_UNAVAILABLE)
            val sharedDecision = synchronizeFreeGreatPersonDecision(game, player)
            val decision = if (sharedDecision == null) {
                null
            } else {
                val fingerprint = sharedDecision.fingerprint
                FreeGreatPersonDecision(
                    handle = sharedDecision.handle,
                    remainingChoices = fingerprint.remainingChoices,
                    mayaLimited = fingerprint.mayaLimitedChoices > 0,
                    options = Collections.unmodifiableList(ArrayList(fingerprint.options)),
                )
            }

            FreeGreatPersonSnapshot(
                epoch = runtime.epoch,
                decision = decision,
            )
        }
    }

    private fun synchronizeFreeGreatPersonDecision(
        game: GameInfo,
        player: Civilization,
    ): SharedFreeGreatPersonDecision? {
        val greatPeople = player.greatPeople
        val fingerprint = if (greatPeople.freeGreatPeople <= 0) {
            null
        } else {
            val options = greatPeople.getFreeGreatPersonOptions()
                .map { unit -> unit.name }
                .sorted()
            FreeGreatPersonFingerprint(
                remainingChoices = greatPeople.freeGreatPeople,
                mayaLimitedChoices = greatPeople.mayaLimitedFreeGP,
                options = options,
                turn = game.turns,
                isYourTurn = game.isCurrentPlayer(player),
            )
        }
        val state = state()
        val previous = state.freeGreatPersonDecision
        return state.synchronizeFreeGreatPersonDecision(fingerprint).also {
            if (it != previous) runtime.commitCheckpoint()
        }
    }
}

private fun GameInfo.findAvailablePlayer(civilizationId: String): Civilization? = civilizations.singleOrNull { civilization ->
    civilization.civID == civilizationId &&
        civilization.isHuman() &&
        civilization.isMajorCiv()
}

private fun GameInfo.isCurrentPlayer(player: Civilization): Boolean = currentPlayer == player.civID && currentPlayerCiv === player

private inline fun <T> playerOperation(operation: () -> T): T = try {
    operation()
} catch (failure: PlayerSessionException) {
    throw failure
} catch (_: RuntimeClosedException) {
    throw PlayerSessionException(PlayerSessionFailure.SESSION_CLOSED)
} catch (_: RuntimeHaltedException) {
    throw PlayerSessionException(PlayerSessionFailure.GAME_HALTED)
} catch (_: RuntimePausedException) {
    throw PlayerSessionException(PlayerSessionFailure.GAME_PAUSED)
} catch (_: Exception) {
    throw PlayerSessionException(PlayerSessionFailure.QUERY_FAILED)
}
