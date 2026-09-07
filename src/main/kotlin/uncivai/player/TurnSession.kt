package uncivai.player

import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerOperations
import com.unciv.logic.civilization.PlayerTurnRequirements
import com.unciv.logic.civilization.PopupAlert
import uncivai.GameExecutor
import java.util.Collections
import java.util.UUID

/** Reconnect-stable, runtime-owned state. Access only under the game executor. */
internal class TurnSessionState {
    val cityHandles = HashMap<String, String>()
    val alertHandles = java.util.IdentityHashMap<PopupAlert, String>()
    val receipts = HashMap<String, Pair<TurnCommandRequest, TurnCommandReceipt>>()
    var fingerprint: PlayerTurnSnapshot? = null
    var revision = UUID.randomUUID().toString()

    fun invalidate() {
        fingerprint = null
    }
}

internal class TurnSession(private val runtime: GameExecutor, private val civilizationId: String) {
    fun snapshot(): PlayerTurnSnapshot = runtime.accessGame { game ->
        val state = state()
        val previous = state.fingerprint
        snapshot(game, player(game), state).also {
            // Persist newly issued references before a client can submit them after reconnecting.
            if (previous != state.fingerprint) runtime.commitCheckpoint()
        }
    }

    fun execute(request: TurnCommandRequest): TurnCommandReceipt = runtime.accessGame { game ->
        if (request.epoch != runtime.epoch) fail(PlayerSessionFailure.STALE_EPOCH)
        if (request.requestId.length !in 1..128 || request.requestId.any { it.code !in 0x20..0x7e }) {
            fail(PlayerSessionFailure.INVALID_ARGUMENT)
        }
        val player = player(game)
        val state = state()
        val detachedRequest = request.copy(queue = immutable(request.queue))
        state.receipts[request.requestId]?.let { (completedRequest, receipt) ->
            if (completedRequest != detachedRequest) fail(PlayerSessionFailure.REQUEST_ID_CONFLICT)
            return@accessGame receipt
        }
        validateShape(detachedRequest)
        runtime.requireRunning()
        if (game.currentPlayer != player.civID || game.currentPlayerCiv !== player) fail(PlayerSessionFailure.NOT_YOUR_TURN)
        val before = snapshot(game, player, state)
        if (request.revision != before.revision) fail(PlayerSessionFailure.STALE_REVISION)
        if (before.phase != "ACTIVE") fail(PlayerSessionFailure.COMMAND_UNAVAILABLE)
        val operations = PlayerOperations(player)
        val city = request.target?.takeIf { request.kind != TurnCommandKind.ACKNOWLEDGE_ALERT }?.let { handle ->
            val cityId = state.cityHandles.entries.firstOrNull { it.value == handle }?.key
            player.cities.singleOrNull { it.id == cityId && it.civ === player }
                ?: fail(PlayerSessionFailure.CITY_UNAVAILABLE)
        }
        if (request.kind == TurnCommandKind.END_TURN && !before.canEndTurn) fail(PlayerSessionFailure.TURN_BLOCKED)

        try {
            val accepted = when (request.kind) {
                TurnCommandKind.SET_RESEARCH_QUEUE -> operations.trySetResearchQueue(detachedRequest.queue)

                TurnCommandKind.CHOOSE_FREE_TECHNOLOGY -> operations.tryChooseFreeTechnology(request.selection!!)

                TurnCommandKind.ADOPT_POLICY -> operations.tryAdoptPolicy(request.selection!!)

                TurnCommandKind.DISMISS_POLICY_PICKER -> operations.tryDismissPolicyPicker()

                TurnCommandKind.QUEUE_CONSTRUCTION -> operations.tryQueueConstruction(city!!, request.selection!!)

                TurnCommandKind.CHOOSE_FREE_GREAT_PERSON -> operations.tryChooseFreeGreatPerson(request.selection!!) != null

                TurnCommandKind.RENAME_CITY -> operations.tryRenameCity(city!!, request.selection!!)

                TurnCommandKind.ACKNOWLEDGE_ALERT -> {
                    val alert = state.alertHandles.entries.firstOrNull { it.value == request.target }?.key
                    alert != null && operations.tryAcknowledgeAlert(alert)
                }

                TurnCommandKind.END_TURN -> {
                    game.nextTurn()
                    true
                }
            }
            if (!accepted) {
                val reason = if (request.kind == TurnCommandKind.CHOOSE_FREE_GREAT_PERSON &&
                    request.selection in before.greatPersonOptions
                ) {
                    PlayerSessionFailure.PLACEMENT_FAILED
                } else {
                    PlayerSessionFailure.OPTION_UNAVAILABLE
                }
                fail(reason)
            }
            state.invalidate()
            runtime.freeGreatPersonDecisions.invalidate(player.civID)
            val after = snapshot(game, player, state)
            val receipt = TurnCommandReceipt(
                requestId = request.requestId,
                epoch = runtime.epoch,
                kind = request.kind,
                revision = after.revision,
                turn = game.turns,
                isYourTurn = game.currentPlayer == player.civID,
            )
            state.receipts[request.requestId] = detachedRequest to receipt
            runtime.commitCheckpoint()
            receipt
        } catch (failure: PlayerSessionException) {
            throw failure
        } catch (failure: Throwable) {
            runtime.haltAfterMutation(failure)
        }
    }

    private fun snapshot(game: GameInfo, player: Civilization, state: TurnSessionState): PlayerTurnSnapshot {
        val operations = PlayerOperations(player)
        val pending = PlayerTurnRequirements.pending(player).map { kind ->
            PendingTurnDecision(kind.name, kind in supportedRequirements)
        }.toMutableList()
        if (player.popupAlerts.isNotEmpty()) {
            pending.add(
                PendingTurnDecision("PopupAlerts", player.popupAlerts.all { it.type in PlayerOperations.informationalAlerts }),
            )
        }
        if (player.tradeRequests.isNotEmpty()) pending.add(PendingTurnDecision("TradeRequests", false))
        if (player.shouldShowDiplomaticVotingResults()) pending.add(PendingTurnDecision("DiplomaticVotingResults", false))
        val phase = when {
            runtime.isPaused() -> "PAUSED"
            player.isDefeated() -> "DEFEATED"
            !game.oneMoreTurnMode && (game.victoryData != null || game.civilizations.any { it.victoryManager.hasWon() }) -> "FINISHED"
            game.currentPlayer == player.civID && game.currentPlayerCiv === player -> "ACTIVE"
            else -> "WAITING"
        }
        state.alertHandles.keys.removeIf { old -> player.popupAlerts.none { it === old } }
        val fingerprint = PlayerTurnSnapshot(
            epoch = runtime.epoch,
            revision = "",
            turn = game.turns,
            phase = phase,
            canEndTurn = phase == "ACTIVE" && pending.isEmpty(),
            pending = immutable(pending),
            alerts = immutable(
                player.popupAlerts.map { alert ->
                    PlayerAlert(
                        handle = state.alertHandles.getOrPut(alert) { UUID.randomUUID().toString() },
                        kind = alert.type.name,
                        subject = when (alert.type) {
                            AlertType.TechResearched, AlertType.WonderBuilt -> alert.value
                            else -> null
                        },
                        canAcknowledge = alert.type in PlayerOperations.informationalAlerts,
                    )
                },
            ),
            cities = immutable(
                player.cities.filter { it.civ === player }.map { city ->
                    TurnCity(
                        handle = state.cityHandles.getOrPut(city.id) { UUID.randomUUID().toString() },
                        name = city.name,
                        puppet = city.isPuppet,
                        constructionQueue = immutable(city.cityConstructions.constructionQueue),
                        constructionOptions = immutable(operations.queueableConstructions(city).sorted()),
                    )
                },
            ),
            researchQueue = immutable(player.tech.techsToResearch),
            researchOptions = immutable(game.ruleset.technologies.keys.filter { player.tech.canBeResearched(it) }.sorted()),
            freeTechnologies = player.tech.freeTechs,
            policyOptions = immutable(game.ruleset.policies.keys.filter { operations.canAdoptPolicy(it) }.sorted()),
            freePolicies = player.policies.freePolicies,
            freeGreatPeople = player.greatPeople.freeGreatPeople,
            mayaLimited = player.greatPeople.mayaLimitedFreeGP > 0,
            greatPersonOptions = immutable(player.greatPeople.getFreeGreatPersonOptions().map { it.name }.sorted()),
            idleUnits = player.units.getDueUnits().count(),
        )
        if (state.fingerprint != fingerprint) {
            state.fingerprint = fingerprint
            state.revision = UUID.randomUUID().toString()
        }
        return fingerprint.copy(revision = state.revision)
    }

    private fun player(game: GameInfo): Civilization = game.civilizations.singleOrNull {
        it.civID == civilizationId && it.isHuman() && it.isMajorCiv()
    } ?: fail(PlayerSessionFailure.PLAYER_UNAVAILABLE)

    private fun state() = runtime.turnSessions.getOrPut(civilizationId) { TurnSessionState() }

    private fun validateShape(request: TurnCommandRequest) {
        val needsTarget = request.kind in setOf(TurnCommandKind.RENAME_CITY, TurnCommandKind.QUEUE_CONSTRUCTION, TurnCommandKind.ACKNOWLEDGE_ALERT)
        val needsSelection = request.kind in setOf(
            TurnCommandKind.CHOOSE_FREE_TECHNOLOGY,
            TurnCommandKind.ADOPT_POLICY,
            TurnCommandKind.QUEUE_CONSTRUCTION,
            TurnCommandKind.CHOOSE_FREE_GREAT_PERSON,
            TurnCommandKind.RENAME_CITY,
        )
        if (needsTarget != (request.target != null) || needsSelection != (request.selection != null) ||
            request.target?.isEmpty() == true || request.selection?.isEmpty() == true || request.revision.isEmpty() ||
            (request.kind == TurnCommandKind.SET_RESEARCH_QUEUE) != request.queue.isNotEmpty() ||
            request.queue.size > 1024 || request.queue.any { it.isEmpty() }
        ) {
            fail(PlayerSessionFailure.INVALID_ARGUMENT)
        }
    }

    companion object {
        private val supportedRequirements = setOf(
            PlayerTurnRequirements.Kind.FreeGreatPerson,
            PlayerTurnRequirements.Kind.Construction,
            PlayerTurnRequirements.Kind.Research,
            PlayerTurnRequirements.Kind.Policy,
        )
    }
}

private fun fail(reason: PlayerSessionFailure): Nothing = throw PlayerSessionException(reason)

private fun <T> immutable(values: Collection<T>): List<T> = Collections.unmodifiableList(ArrayList(values))
