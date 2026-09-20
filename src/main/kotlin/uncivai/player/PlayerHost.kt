package uncivai.player

import com.unciv.logic.GameInfo
import com.unciv.logic.city.City
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerOperations
import com.unciv.logic.map.mapunit.MapUnit
import uncivai.GameExecutor
import uncivai.HeadlessRuntime
import uncivai.PlayerApiAccounting
import uncivai.RuntimeClosedException
import uncivai.RuntimeHaltedException
import uncivai.RuntimePausedException
import java.util.Collections

/** Trusted host surface that assigns a fixed player identity to each session. */
class PlayerHost internal constructor(
    private val runtime: GameExecutor,
    private val cityRenamer: (City, String) -> Unit,
    private val greatPersonChooser: (Civilization, String) -> MapUnit?,
) {
    constructor(runtime: HeadlessRuntime) : this(runtime.executor)

    constructor(runtime: GameExecutor) : this(
        runtime,
        { city, name -> check(PlayerOperations(city.civ).tryRenameCity(city, name)) },
        { civilization, unitName -> PlayerOperations(civilization).tryChooseFreeGreatPerson(unitName) },
    )

    internal constructor(
        runtime: HeadlessRuntime,
        cityRenamer: (City, String) -> Unit,
        greatPersonChooser: (Civilization, String) -> MapUnit?,
    ) : this(runtime.executor, cityRenamer, greatPersonChooser)

    internal constructor(
        runtime: HeadlessRuntime,
        cityRenamer: (City, String) -> Unit,
    ) : this(
        runtime.executor,
        cityRenamer,
        { civilization, unitName -> civilization.greatPeople.chooseFreeGreatPerson(unitName) },
    )

    fun bindPlayer(civilizationId: String): PlayerSession = playerOperation {
        runtime.accessGame { game ->
            game.findAvailablePlayer(civilizationId)
                ?: throw PlayerSessionException(PlayerSessionFailure.PLAYER_UNAVAILABLE)
        }
        BoundPlayerSession(runtime, civilizationId, cityRenamer, greatPersonChooser)
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

    fun researchDetails(names: List<String> = emptyList()): ResearchDetails

    fun policyDetails(names: List<String> = emptyList()): PolicyDetails

    fun victoryProgress(names: List<String> = emptyList()): VictoryProgress

    /** Read a receipt only when every field matches this player's completed request. */
    fun completedReceipt(request: TurnCommandRequest): TurnCommandReceipt?

    fun execute(request: TurnCommandRequest): TurnCommandReceipt

    fun summary(): PlayerSummary

    fun cities(): OwnedCitiesSnapshot

    fun renameCity(request: RenameCityRequest): RenameCityReceipt

    fun freeGreatPersonChoice(): FreeGreatPersonSnapshot

    fun chooseFreeGreatPerson(request: ChooseFreeGreatPersonRequest): ChooseFreeGreatPersonReceipt
}

private class BoundPlayerSession(
    private val runtime: GameExecutor,
    private val civilizationId: String,
    private val cityRenamer: (City, String) -> Unit,
    private val greatPersonChooser: (Civilization, String) -> MapUnit?,
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
            PlayerCapabilities(durableRecovery = runtime.durableRecovery, legacyMutationsSupported = !runtime.durableRecovery)
        }
    }

    override fun turn() = playerOperation { turnSession.snapshot() }

    override fun result() = playerOperation { turnSession.snapshot().finalState(civilizationId) }

    override fun movementPreview(request: MovementPreviewRequest) = playerOperation { turnSession.movementPreview(request) }

    override fun missionPreview(request: MissionPreviewRequest) = playerOperation { turnSession.missionPreview(request) }

    override fun researchDetails(names: List<String>) = playerOperation { turnSession.researchDetails(names) }

    override fun policyDetails(names: List<String>) = playerOperation { turnSession.policyDetails(names) }

    override fun victoryProgress(names: List<String>) = playerOperation { turnSession.victoryProgress(names) }

    override fun completedReceipt(request: TurnCommandRequest) = playerOperation { turnSession.completedReceipt(request) }

    override fun execute(request: TurnCommandRequest) = playerOperation { turnSession.execute(request) }

    private val completedCommands = HashMap<String, CompletedCommand>()
    private var currentDecisionHandle: String? = null

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

    override fun renameCity(request: RenameCityRequest): RenameCityReceipt = playerOperation {
        runtime.accessGame { game ->
            if (request.epoch != runtime.epoch) {
                throw PlayerSessionException(PlayerSessionFailure.STALE_EPOCH)
            }
            if (!request.requestId.hasValidRequestId()) {
                throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
            }
            val player = game.findAvailablePlayer(civilizationId)
                ?: throw PlayerSessionException(PlayerSessionFailure.PLAYER_UNAVAILABLE)

            val completed = completedCommands[request.requestId]
            if (completed != null) {
                if (completed is CompletedCommand.RenameCity && completed.request == request) {
                    return@accessGame completed.receipt
                }
                throw PlayerSessionException(PlayerSessionFailure.REQUEST_ID_CONFLICT)
            }
            if (!request.hasValidRenameArguments()) {
                throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
            }

            runtime.requireRunning()
            if (runtime.durableRecovery || gameHasResult(game) || player.hasCaptureDecision()) throw PlayerSessionException(PlayerSessionFailure.COMMAND_UNAVAILABLE)

            if (game.currentPlayer != player.civID || game.currentPlayerCiv !== player) {
                throw PlayerSessionException(PlayerSessionFailure.NOT_YOUR_TURN)
            }

            val cityId = state().cityHandles.entries.firstOrNull { it.value == request.cityHandle }?.key
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
            val completedRename = CompletedCommand.RenameCity(request, receipt)

            try {
                cityRenamer(city, request.name)
                completedCommands[request.requestId] = completedRename
            } catch (failure: Throwable) {
                runtime.haltAfterMutation(failure)
            }
            receipt
        }
    }

    override fun freeGreatPersonChoice(): FreeGreatPersonSnapshot = playerOperation {
        runtime.accessGame { game ->
            val player = game.findAvailablePlayer(civilizationId)
                ?: throw PlayerSessionException(PlayerSessionFailure.PLAYER_UNAVAILABLE)
            val sharedDecision = synchronizeFreeGreatPersonDecision(game, player)
            val decision = if (sharedDecision == null) {
                clearCurrentDecision()
                null
            } else {
                val fingerprint = sharedDecision.fingerprint
                FreeGreatPersonDecision(
                    handle = sharedDecision.handle.also { currentDecisionHandle = it },
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

    override fun chooseFreeGreatPerson(
        request: ChooseFreeGreatPersonRequest,
    ): ChooseFreeGreatPersonReceipt = playerOperation {
        runtime.accessGame { game ->
            if (request.epoch != runtime.epoch) {
                throw PlayerSessionException(PlayerSessionFailure.STALE_EPOCH)
            }
            if (!request.requestId.hasValidRequestId()) {
                throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
            }
            val player = game.findAvailablePlayer(civilizationId)
                ?: throw PlayerSessionException(PlayerSessionFailure.PLAYER_UNAVAILABLE)

            val completed = completedCommands[request.requestId]
            if (completed != null) {
                if (completed is CompletedCommand.ChooseFreeGreatPerson && completed.request == request) {
                    return@accessGame completed.receipt
                }
                throw PlayerSessionException(PlayerSessionFailure.REQUEST_ID_CONFLICT)
            }

            if (!game.isCurrentPlayer(player)) {
                throw PlayerSessionException(PlayerSessionFailure.NOT_YOUR_TURN)
            }

            runtime.requireRunning()
            if (runtime.durableRecovery || gameHasResult(game) || player.hasCaptureDecision()) throw PlayerSessionException(PlayerSessionFailure.COMMAND_UNAVAILABLE)

            val sharedDecision = synchronizeFreeGreatPersonDecision(game, player)
            if (sharedDecision == null) {
                clearCurrentDecision()
                throw PlayerSessionException(PlayerSessionFailure.DECISION_UNAVAILABLE)
            }
            if (sharedDecision.handle != request.decisionHandle ||
                currentDecisionHandle != request.decisionHandle
            ) {
                if (currentDecisionHandle != sharedDecision.handle) clearCurrentDecision()
                throw PlayerSessionException(PlayerSessionFailure.DECISION_UNAVAILABLE)
            }
            if (request.unitName !in sharedDecision.fingerprint.options) {
                throw PlayerSessionException(PlayerSessionFailure.OPTION_UNAVAILABLE)
            }

            val receipt = try {
                val placedUnit = greatPersonChooser(player, request.unitName)
                if (placedUnit == null) {
                    null
                } else {
                    ChooseFreeGreatPersonReceipt(
                        requestId = request.requestId,
                        epoch = runtime.epoch,
                        decisionHandle = request.decisionHandle,
                        unitName = request.unitName,
                        remainingChoices = player.greatPeople.freeGreatPeople,
                    ).also { successfulReceipt ->
                        state().freeGreatPersonDecision = null
                        clearCurrentDecision()
                        completedCommands[request.requestId] = CompletedCommand.ChooseFreeGreatPerson(
                            request,
                            successfulReceipt,
                        )
                    }
                }
            } catch (failure: Throwable) {
                runtime.haltAfterMutation(failure)
            }
            receipt ?: throw PlayerSessionException(PlayerSessionFailure.PLACEMENT_FAILED)
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

    private fun clearCurrentDecision() {
        currentDecisionHandle = null
    }
}

private sealed interface CompletedCommand {
    data class RenameCity(
        val request: RenameCityRequest,
        val receipt: RenameCityReceipt,
    ) : CompletedCommand

    data class ChooseFreeGreatPerson(
        val request: ChooseFreeGreatPersonRequest,
        val receipt: ChooseFreeGreatPersonReceipt,
    ) : CompletedCommand
}

private fun String.hasValidRequestId(): Boolean = length in 1..128 &&
    all { character -> character.code in 0x20..0x7e }

private fun RenameCityRequest.hasValidRenameArguments(): Boolean = PlayerOperations.isValidCityName(name)

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
