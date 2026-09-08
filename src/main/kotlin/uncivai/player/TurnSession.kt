package uncivai.player

import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerCityOperations
import com.unciv.logic.civilization.PlayerOperations
import com.unciv.logic.civilization.PlayerTurnRequirements
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.civilization.PlayerWorkerOperations
import com.unciv.logic.civilization.PopupAlert
import uncivai.GameExecutor
import java.util.Collections
import java.util.UUID

/** Reconnect-stable, runtime-owned state. Access only under the game executor. */
internal class TurnSessionState {
    val cityHandles = HashMap<String, String>()
    val unitHandles = HashMap<Int, String>()
    val alertHandles = java.util.IdentityHashMap<PopupAlert, String>()
    val receipts = HashMap<String, Pair<TurnCommandRequest, TurnCommandReceipt>>()
    var fingerprint: PlayerTurnSnapshot? = null
    var revision = UUID.randomUUID().toString()

    // Only the public query presents popup contents. A command receipt presents a revision alone.
    // Reset on process restart: uncommitted acknowledgements need a fresh read; completed receipts replay.
    var observedRevision: String? = null

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
            state.observedRevision = it.revision
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
        if (request.kind == TurnCommandKind.ACKNOWLEDGE_ALERT) {
            if (before.alerts.none { it.handle == request.target && it.canAcknowledge && it.content != null }) {
                fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            }
            if (state.observedRevision != before.revision) fail(PlayerSessionFailure.OBSERVATION_REQUIRED)
        }
        val city = request.target?.takeIf { request.kind in cityCommands }?.let { handle ->
            val cityId = state.cityHandles.entries.firstOrNull { it.value == handle }?.key
            player.cities.singleOrNull { it.id == cityId && it.civ === player }
                ?: fail(PlayerSessionFailure.CITY_UNAVAILABLE)
        }
        val unit = request.target?.takeIf { request.kind in unitCommands }?.let { handle ->
            val unitId = state.unitHandles.entries.firstOrNull { it.value == handle }?.key
            player.units.getCivUnits().singleOrNull { it.id == unitId && it.civ === player && !it.isDestroyed }
                ?: fail(PlayerSessionFailure.UNIT_UNAVAILABLE)
        }
        val destination = if (request.kind == TurnCommandKind.MOVE_UNIT) {
            // Resolve only an advertised option, so arbitrary coordinates never probe hidden tiles.
            val option = before.map.units.single { it.handle == request.target }.moves
                .firstOrNull { it.destination == request.destination } ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            game.tileMap[com.unciv.logic.map.HexCoord(option.destination.x, option.destination.y)]
        } else if (request.kind in tileCommands) {
            val option = before.cities.single { it.handle == request.target }.development!!.tiles
                .singleOrNull { it.position == request.destination } ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            val permitted = when (request.kind) {
                TurnCommandKind.SET_WORKED_TILE -> if (request.worked == true) option.canWork else option.canUnwork
                TurnCommandKind.SET_TILE_LOCKED -> if (request.locked == true) option.canLock else option.canUnlock
                else -> false
            }
            if (!permitted) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            game.tileMap[com.unciv.logic.map.HexCoord(option.position.x, option.position.y)]
        } else {
            null
        }
        if (request.kind == TurnCommandKind.END_TURN && !before.canEndTurn) fail(PlayerSessionFailure.TURN_BLOCKED)
        if (request.kind == TurnCommandKind.START_WORKER_JOB && before.map.units.single { it.handle == request.target }.worker
                ?.options?.none { it.name == request.selection && it.available } != false
        ) {
            fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
        }
        if (request.kind == TurnCommandKind.STOP_WORKER_JOB && before.map.units.single { it.handle == request.target }.worker?.canStop != true) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
        if (request.kind in setOf(TurnCommandKind.REMOVE_CONSTRUCTION, TurnCommandKind.MOVE_CONSTRUCTION)) {
            val option = before.cities.single { it.handle == request.target }.development!!.constructions
                .singleOrNull { it.index == request.index } ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            if ((request.kind == TurnCommandKind.REMOVE_CONSTRUCTION && !option.canRemove) ||
                (request.kind == TurnCommandKind.MOVE_CONSTRUCTION && request.toIndex !in option.moveToIndices)
            ) {
                fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            }
        }

        try {
            val accepted = when (request.kind) {
                TurnCommandKind.MOVE_UNIT -> PlayerUnitOperations(player).tryMove(unit!!, destination!!)

                TurnCommandKind.FOUND_CITY -> PlayerUnitOperations(player).tryFoundCity(unit!!, request.confirmBreakPromise)

                TurnCommandKind.START_WORKER_JOB -> PlayerWorkerOperations(player).tryStartJob(unit!!, request.selection!!)

                TurnCommandKind.STOP_WORKER_JOB -> PlayerWorkerOperations(player).tryStopJob(unit!!)

                TurnCommandKind.SET_WORKED_TILE -> PlayerCityOperations(player).trySetWorkedTile(city!!, destination!!, request.worked!!)

                TurnCommandKind.SET_TILE_LOCKED -> PlayerCityOperations(player).trySetTileLocked(city!!, destination!!, request.locked!!)

                TurnCommandKind.REMOVE_CONSTRUCTION -> PlayerCityOperations(player).tryRemoveConstruction(city!!, request.index!!)

                TurnCommandKind.MOVE_CONSTRUCTION -> PlayerCityOperations(player).tryMoveConstruction(city!!, request.index!!, request.toIndex!!)

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
                    runtime.handoff++
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
        val phase = when {
            runtime.isPaused() -> "PAUSED"
            player.isDefeated() -> "DEFEATED"
            !game.oneMoreTurnMode && (game.victoryData != null || game.civilizations.any { it.victoryManager.hasWon() }) -> "FINISHED"
            game.currentPlayer == player.civID && game.currentPlayerCiv === player -> "ACTIVE"
            else -> "WAITING"
        }
        state.alertHandles.keys.removeIf { old -> player.popupAlerts.none { it === old } }
        val alerts = player.popupAlerts.map { alert ->
            val contact = observeFirstContact(operations, alert)
            val content = observeAlertContent(operations, alert)
            val supported = alert.type in PlayerOperations.informationalAlerts && content != null
            PlayerAlert(
                handle = state.alertHandles.getOrPut(alert) { UUID.randomUUID().toString() },
                kind = alert.type.name,
                subject = when (alert.type) {
                    AlertType.TechResearched, AlertType.WonderBuilt -> alert.value.takeIf { content != null }
                    AlertType.FirstContact -> contact?.civilizationName
                    else -> null
                },
                canAcknowledge = phase == "ACTIVE" && supported,
                firstContact = contact,
                content = content,
            ) to supported
        }
        if (alerts.isNotEmpty()) {
            // Support is a capability even while paused or waiting; action flags require an active turn.
            pending.add(PendingTurnDecision("PopupAlerts", alerts.all { it.second }))
        }
        if (player.tradeRequests.isNotEmpty()) pending.add(PendingTurnDecision("TradeRequests", false))
        if (player.shouldShowDiplomaticVotingResults()) pending.add(PendingTurnDecision("DiplomaticVotingResults", false))
        val fingerprint = PlayerTurnSnapshot(
            epoch = runtime.epoch,
            revision = "",
            turn = game.turns,
            phase = phase,
            canEndTurn = phase == "ACTIVE" && pending.isEmpty(),
            pending = immutable(pending),
            alerts = immutable(alerts.map { it.first }),
            cities = immutable(
                player.cities.filter { it.civ === player }.map { city ->
                    TurnCity(
                        handle = state.cityHandles.getOrPut(city.id) { UUID.randomUUID().toString() },
                        name = city.name,
                        puppet = city.isPuppet,
                        constructionQueue = immutable(city.cityConstructions.constructionQueue),
                        constructionOptions = immutable(operations.queueableConstructions(city).sorted()),
                        development = observeCityDevelopment(city, active = phase == "ACTIVE"),
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
            map = observeMap(player, state, canAct = phase == "ACTIVE"),
            research = observeResearchDevelopment(player),
            diplomacy = observeDiplomacy(player),
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
        val needsTarget = request.kind in cityCommands || request.kind in unitCommands || request.kind == TurnCommandKind.ACKNOWLEDGE_ALERT
        val needsSelection = request.kind in setOf(
            TurnCommandKind.CHOOSE_FREE_TECHNOLOGY,
            TurnCommandKind.ADOPT_POLICY,
            TurnCommandKind.QUEUE_CONSTRUCTION,
            TurnCommandKind.CHOOSE_FREE_GREAT_PERSON,
            TurnCommandKind.RENAME_CITY,
            TurnCommandKind.START_WORKER_JOB,
        )
        if (needsTarget != (request.target != null) || needsSelection != (request.selection != null) ||
            request.target?.isEmpty() == true || request.selection?.isEmpty() == true || request.revision.isEmpty() ||
            (request.kind == TurnCommandKind.SET_RESEARCH_QUEUE) != request.queue.isNotEmpty() ||
            request.queue.size > 1024 || request.queue.any { it.isEmpty() } ||
            (request.kind == TurnCommandKind.MOVE_UNIT || request.kind in tileCommands) != (request.destination != null) ||
            (request.kind == TurnCommandKind.SET_WORKED_TILE) != (request.worked != null) ||
            (request.kind == TurnCommandKind.SET_TILE_LOCKED) != (request.locked != null) ||
            (request.kind in setOf(TurnCommandKind.REMOVE_CONSTRUCTION, TurnCommandKind.MOVE_CONSTRUCTION)) != (request.index != null) ||
            (request.kind == TurnCommandKind.MOVE_CONSTRUCTION) != (request.toIndex != null) ||
            (request.index != null && request.index < 0) || (request.toIndex != null && request.toIndex < 0) ||
            (request.confirmBreakPromise && request.kind != TurnCommandKind.FOUND_CITY)
        ) {
            fail(PlayerSessionFailure.INVALID_ARGUMENT)
        }
    }

    companion object {
        private val tileCommands = setOf(TurnCommandKind.SET_WORKED_TILE, TurnCommandKind.SET_TILE_LOCKED)
        private val cityCommands = tileCommands + setOf(TurnCommandKind.RENAME_CITY, TurnCommandKind.QUEUE_CONSTRUCTION, TurnCommandKind.REMOVE_CONSTRUCTION, TurnCommandKind.MOVE_CONSTRUCTION)
        private val unitCommands = setOf(TurnCommandKind.MOVE_UNIT, TurnCommandKind.FOUND_CITY, TurnCommandKind.START_WORKER_JOB, TurnCommandKind.STOP_WORKER_JOB)
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
