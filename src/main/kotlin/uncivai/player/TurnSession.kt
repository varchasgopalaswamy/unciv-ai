package uncivai.player

import com.unciv.logic.GameInfo
import com.unciv.logic.city.CityFocus
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.CaptureChoice
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.Notification
import com.unciv.logic.civilization.PlayerCaptureOperations
import com.unciv.logic.civilization.PlayerCityManagementOperations
import com.unciv.logic.civilization.PlayerCityOperations
import com.unciv.logic.civilization.PlayerCombatOperations
import com.unciv.logic.civilization.PlayerDiplomacyOperations
import com.unciv.logic.civilization.PlayerFriendshipOperations
import com.unciv.logic.civilization.PlayerGreatPersonOperations
import com.unciv.logic.civilization.PlayerOperations
import com.unciv.logic.civilization.PlayerPurchaseOperations
import com.unciv.logic.civilization.PlayerTurnRequirements
import com.unciv.logic.civilization.PlayerUnitEconomyOperations
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.civilization.PlayerUnitOrders
import com.unciv.logic.civilization.PlayerWorkerOperations
import com.unciv.logic.civilization.PopupAlert
import uncivai.GameExecutor
import java.util.Collections

/** Reconnect-stable, runtime-owned state. Access only under the game executor. */
internal class TurnSessionState(savedIdentifiers: Map<PlayerIdentifier, Long> = emptyMap()) {
    val identifiers = PlayerIdentifiers(savedIdentifiers)
    val cityHandles = HashMap<String, String>()
    val unitHandles = HashMap<Int, String>()
    val alertHandles = java.util.IdentityHashMap<PopupAlert, String>()
    val notificationHandles = java.util.IdentityHashMap<Notification, String>()
    val dismissedNotifications = HashSet<String>()
    val receipts = HashMap<String, Pair<TurnCommandRequest, TurnCommandReceipt>>()
    var fingerprint: PlayerTurnSnapshot? = null
    var revision = ""
    var checkpointRevision: String? = null
    var freeGreatPersonDecision: SharedFreeGreatPersonDecision? = null

    // Only the public query presents popup contents. A command receipt presents a revision alone.
    // Reset on process restart: uncommitted acknowledgements need a fresh read; completed receipts replay.
    var observedRevision: String? = null
    var reviewTurn: Int? = null
    val reviewActions = ArrayList<ConfirmedTurnAction>()
    var previousTurnReview: CompletedTurnReview? = null
    val combatReports = LinkedHashMap<String, CombatReport>()
    var deliveredCombatReports = 0

    fun invalidate() {
        fingerprint = null
    }

    fun cityHandle(cityId: String): String = cityHandles.getOrPut(cityId) { identifiers.next(PlayerIdentifier.CITY) }
}

internal class TurnSession(private val runtime: GameExecutor, private val civilizationId: String) {
    fun completedReceipt(request: TurnCommandRequest): TurnCommandReceipt? = runtime.accessGame {
        val completed = runtime.turnSessions[civilizationId]?.receipts?.get(request.requestId)
        if (completed?.first == request.detached()) completed.second.detached() else null
    }

    fun snapshot(): PlayerTurnSnapshot = runtime.accessGame { game ->
        runtime.captureCombatReports()
        val state = state()
        snapshot(game, player(game), state).also {
            // Persist newly issued references before a client can submit them after reconnecting.
            // A rejected command may already have refreshed the fingerprint without a commit.
            if (state.checkpointRevision != state.revision) runtime.commitCheckpoint()
            state.observedRevision = it.revision
        }
    }

    fun researchDetails(names: List<String>): ResearchDetails = details(names, ::observeResearchDetails)

    fun policyDetails(names: List<String>): PolicyDetails = details(names, ::observePolicyDetails)

    fun victoryProgress(names: List<String>): VictoryProgress = details(names, ::observeVictoryProgress)

    private fun <T> details(names: List<String>, observe: (Civilization, PlayerTurnSnapshot, List<String>) -> T): T = runtime.accessGame { game ->
        val state = state()
        val player = player(game)
        val turn = snapshot(game, player, state)
        val result = observe(player, turn, names.toList())
        if (state.checkpointRevision != state.revision) runtime.commitCheckpoint()
        // Research/policy details do not establish that popup or unit-action content was read.
        result
    }

    fun execute(request: TurnCommandRequest): TurnCommandReceipt = runtime.accessGame { game ->
        if (request.epoch != runtime.epoch) fail(PlayerSessionFailure.STALE_EPOCH)
        if (request.requestId.length !in 1..128 || request.requestId.any { it.code !in 0x20..0x7e }) {
            fail(PlayerSessionFailure.INVALID_ARGUMENT)
        }
        val player = player(game)
        val state = state()
        val detachedRequest = request.detached()
        state.receipts[request.requestId]?.let { (completedRequest, receipt) ->
            if (completedRequest != detachedRequest) fail(PlayerSessionFailure.REQUEST_ID_CONFLICT)
            return@accessGame receipt
        }
        validateShape(detachedRequest)
        runtime.requireRunning()
        if (game.currentPlayer != player.civID || game.currentPlayerCiv !== player) fail(PlayerSessionFailure.NOT_YOUR_TURN)
        val before = snapshot(game, player, state)
        if (request.revision != before.revision) fail(PlayerSessionFailure.STALE_REVISION)
        runtime.communication.validate(player.civID, request, request.target?.takeIf { request.kind == TurnCommandKind.DECLARE_WAR }, game.turns)
        if (before.phase != "ACTIVE") fail(PlayerSessionFailure.COMMAND_UNAVAILABLE)
        if (gameHasResult(game) && request.kind != TurnCommandKind.RESOLVE_CAPTURED_CITY) fail(PlayerSessionFailure.COMMAND_UNAVAILABLE)
        if (player.hasCaptureDecision() && request.kind !in setOf(TurnCommandKind.RESOLVE_CAPTURED_CITY, TurnCommandKind.ACKNOWLEDGE_ALERT)) fail(PlayerSessionFailure.TURN_BLOCKED)
        val operations = PlayerOperations(player)
        val captures = PlayerCaptureOperations(player)
        if (request.kind == TurnCommandKind.RESPOND_FRIENDSHIP) {
            val decision = before.alerts.singleOrNull { it.handle == request.target }?.friendship ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            if (request.selection !in setOf("Accept", "Decline") || (request.selection == "Accept" && !decision.accept.available) || !decision.canDecline) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            if (state.observedRevision != before.revision) fail(PlayerSessionFailure.OBSERVATION_REQUIRED)
        }
        if (request.kind == TurnCommandKind.RESOLVE_CAPTURED_CITY) {
            val decision = before.alerts.singleOrNull { it.handle == request.target }?.capture
                ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            if (decision.options.none { it.name == request.selection && it.available }) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            if (state.observedRevision != before.revision) fail(PlayerSessionFailure.OBSERVATION_REQUIRED)
        }
        if (request.kind == TurnCommandKind.ACKNOWLEDGE_ALERT) {
            if (before.alerts.none { it.handle == request.target && it.canAcknowledge && it.content != null }) {
                fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            }
            if (state.observedRevision != before.revision) fail(PlayerSessionFailure.OBSERVATION_REQUIRED)
        }
        val dismissed = if (request.kind == TurnCommandKind.DISMISS_NOTIFICATIONS) {
            val selected = detachedRequest.targets.map { handle ->
                before.notifications.singleOrNull { it.handle == handle && it.canDismiss }
                    ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            }
            if (state.observedRevision != before.revision) fail(PlayerSessionFailure.OBSERVATION_REQUIRED)
            immutable(selected)
        } else {
            emptyList()
        }
        val partner = if (request.kind == TurnCommandKind.DECLARE_WAR) {
            val offered = before.diplomacy.knownCivilizations.singleOrNull { it.civilizationId == request.target }
                ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            if (!offered.declareWar.available) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            if (state.observedRevision != before.revision) fail(PlayerSessionFailure.OBSERVATION_REQUIRED)
            game.getCivilization(offered.civilizationId)
        } else if (request.kind == TurnCommandKind.RESPOND_FRIENDSHIP && request.selection == "Accept") {
            game.getCivilization(before.alerts.single { it.handle == request.target }.friendship!!.civilizationId)
        } else {
            null
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
        } else if (request.kind == TurnCommandKind.REASSIGN_CITY_TILE) {
            val option = before.cities.single { it.handle == request.target }.management!!.sharedTiles
                .singleOrNull { it.position == request.destination && it.action.available } ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            game.tileMap[com.unciv.logic.map.HexCoord(option.position.x, option.position.y)]
        } else if (request.kind == TurnCommandKind.BUY_TILE) {
            val option = before.cities.single { it.handle == request.target }.purchases!!.tiles
                .singleOrNull { it.position == request.destination } ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            if (!option.available) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            game.tileMap[com.unciv.logic.map.HexCoord(option.position.x, option.position.y)]
        } else if (request.kind == TurnCommandKind.ATTACK || request.kind == TurnCommandKind.CITY_ATTACK) {
            val offered = if (request.kind == TurnCommandKind.ATTACK) {
                before.map.units.single { it.handle == request.target }.combat!!.attacks
            } else {
                before.cities.single { it.handle == request.target }.attacks
            }
            val option = offered.singleOrNull { it.destination == request.destination && (request.kind == TurnCommandKind.CITY_ATTACK || it.attackFrom == request.attackFrom) }
                ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            game.tileMap[com.unciv.logic.map.HexCoord(option.destination.x, option.destination.y)].also { tile ->
                val defender = if (tile.isCityCenter()) tile.getCity()?.civ else (tile.militaryUnit ?: tile.civilianUnit)?.civ
                runtime.communication.validate(player.civID, request, defender?.civID, game.turns)
            }
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
        if (request.kind == TurnCommandKind.SET_UNIT_ORDER && before.map.units.single { it.handle == request.target }.combat!!.orders.none { it.name == request.selection && it.available }) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
        if (request.kind == TurnCommandKind.ACTIVATE_GREAT_PERSON) {
            if (before.map.units.single { it.handle == request.target }.greatPerson?.abilities?.none { it.name == request.selection && it.available } != false) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            if (state.observedRevision != before.revision) fail(PlayerSessionFailure.OBSERVATION_REQUIRED)
        }
        if (request.kind == TurnCommandKind.PROMOTE_UNIT && before.map.units.single { it.handle == request.target }.combat!!.promotions?.choices?.none { it.name == request.selection && it.available } != false) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
        if (request.kind == TurnCommandKind.MANAGE_CAPTURED_CITY) {
            if (before.cities.single { it.handle == request.target }.captureOptions.none { it.name == request.selection && it.available }) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            if (state.observedRevision != before.revision) fail(PlayerSessionFailure.OBSERVATION_REQUIRED)
        }
        if (request.kind in cityManagementCommands) {
            val management = before.cities.single { it.handle == request.target }.management!!
            val action = when (request.kind) {
                TurnCommandKind.SET_CITY_FOCUS -> management.focusOptions.singleOrNull { it.name == request.selection }?.action
                TurnCommandKind.SET_AVOID_GROWTH -> management.avoidGrowth
                TurnCommandKind.SET_MANUAL_SPECIALISTS -> management.specialistControl
                TurnCommandKind.RESET_CITIZENS -> management.resetCitizens
                TurnCommandKind.ASSIGN_SPECIALIST -> management.specialists.singleOrNull { it.name == request.selection }?.assign
                TurnCommandKind.UNASSIGN_SPECIALIST -> management.specialists.singleOrNull { it.name == request.selection }?.unassign
                TurnCommandKind.SELL_BUILDING -> management.buildingSales.singleOrNull { it.name == request.selection }?.action
                TurnCommandKind.REASSIGN_CITY_TILE -> management.sharedTiles.singleOrNull { it.position == request.destination }?.action
                else -> null
            }
            if (action?.available != true) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            if (state.observedRevision != before.revision) fail(PlayerSessionFailure.OBSERVATION_REQUIRED)
        }
        if (request.kind in economicCommands) {
            val confirmation = when (request.kind) {
                TurnCommandKind.SELL_BUILDING -> before.cities.single { it.handle == request.target }.management!!.buildingSales
                    .single { it.name == request.selection }.confirmation

                TurnCommandKind.UPGRADE_UNIT -> (
                    before.map.units.single { it.handle == request.target }.economy!!.upgrades
                        .singleOrNull { it.targetName == request.selection && it.available } ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
                    ).confirmation

                TurnCommandKind.PILLAGE -> (
                    before.map.units.single { it.handle == request.target }.economy!!.pillage
                        ?.takeIf { it.available } ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
                    ).confirmation

                TurnCommandKind.DISBAND_UNIT -> (
                    before.map.units.single { it.handle == request.target }.economy!!.disband
                        ?.takeIf { it.available } ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
                    ).confirmation

                TurnCommandKind.BUY_CONSTRUCTION -> (
                    before.cities.single { it.handle == request.target }.purchases!!.constructions
                        .singleOrNull { it.name == request.selection && it.available } ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
                    ).confirmation

                TurnCommandKind.BUY_TILE -> before.cities.single { it.handle == request.target }.purchases!!.tiles
                    .single { it.position == request.destination }.confirmation

                else -> null
            }
            if (state.observedRevision != before.revision) fail(PlayerSessionFailure.OBSERVATION_REQUIRED)
            if (confirmation != null && !request.confirmAction) fail(PlayerSessionFailure.INVALID_ARGUMENT)
        }
        if (request.kind == TurnCommandKind.END_TURN) {
            if (!before.canEndTurn) fail(PlayerSessionFailure.TURN_BLOCKED)
            if (state.observedRevision != before.revision) fail(PlayerSessionFailure.OBSERVATION_REQUIRED)
            val review = before.turnReview!!
            if (request.reviewId != review.id || (review.deferredUnits().isNotEmpty() && request.deferReason.isNullOrBlank())) {
                fail(PlayerSessionFailure.TURN_REVIEW_REQUIRED)
            }
        }
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
            val cultureBefore = player.policies.storedCulture
            runtime.captureCombatReports()
            var combatResult: CombatOutcome? = null
            val accepted = when (request.kind) {
                TurnCommandKind.ATTACK -> {
                    val origin = request.attackFrom!!
                    combatResult = PlayerCombatOperations(player).tryAttack(unit!!, destination!!, game.tileMap[com.unciv.logic.map.HexCoord(origin.x, origin.y)])?.observation()
                    combatResult != null
                }

                TurnCommandKind.CITY_ATTACK -> {
                    combatResult = PlayerCombatOperations(player).tryAttack(city!!, destination!!)?.observation()
                    combatResult != null
                }

                TurnCommandKind.SET_UNIT_ORDER -> PlayerUnitOrders(player).tryOrder(unit!!, PlayerUnitOrders.Order.valueOf(request.selection!!))

                TurnCommandKind.PROMOTE_UNIT -> PlayerUnitOrders(player).tryPromote(unit!!, request.selection!!)

                TurnCommandKind.RESOLVE_CAPTURED_CITY -> {
                    val alert = state.alertHandles.entries.singleOrNull { it.value == request.target }?.key
                    alert != null && captures.tryResolve(alert, CaptureChoice.valueOf(request.selection!!))
                }

                TurnCommandKind.RESPOND_FRIENDSHIP -> {
                    val alert = state.alertHandles.entries.singleOrNull { it.value == request.target }?.key
                    alert != null && PlayerFriendshipOperations(player).tryRespond(alert, request.selection == "Accept")
                }

                TurnCommandKind.MANAGE_CAPTURED_CITY -> captures.tryManageCity(city!!, CaptureChoice.valueOf(request.selection!!))

                TurnCommandKind.UPGRADE_UNIT -> PlayerUnitEconomyOperations(player).tryUpgrade(unit!!, request.selection!!) != null

                TurnCommandKind.PILLAGE -> PlayerUnitEconomyOperations(player).tryPillage(unit!!)

                TurnCommandKind.DISBAND_UNIT -> PlayerUnitEconomyOperations(player).tryDisband(unit!!)

                TurnCommandKind.ACTIVATE_GREAT_PERSON -> PlayerGreatPersonOperations(player).tryActivate(unit!!, request.selection!!)

                TurnCommandKind.BUY_CONSTRUCTION -> PlayerPurchaseOperations(player).tryBuyConstruction(city!!, request.selection!!)

                TurnCommandKind.BUY_TILE -> PlayerPurchaseOperations(player).tryBuyTile(city!!, destination!!)

                TurnCommandKind.SET_CITY_FOCUS -> PlayerCityManagementOperations(player).trySetFocus(city!!, CityFocus.valueOf(request.selection!!))

                TurnCommandKind.SET_AVOID_GROWTH -> PlayerCityManagementOperations(player).trySetAvoidGrowth(city!!, request.enabled!!)

                TurnCommandKind.SET_MANUAL_SPECIALISTS -> PlayerCityManagementOperations(player).trySetManualSpecialists(city!!, request.enabled!!)

                TurnCommandKind.ASSIGN_SPECIALIST, TurnCommandKind.UNASSIGN_SPECIALIST -> PlayerCityManagementOperations(player).trySetSpecialist(city!!, request.selection!!, request.kind == TurnCommandKind.ASSIGN_SPECIALIST)

                TurnCommandKind.RESET_CITIZENS -> PlayerCityManagementOperations(player).tryReassignPopulation(city!!, resetLocked = true)

                TurnCommandKind.SELL_BUILDING -> PlayerCityManagementOperations(player).trySellBuilding(city!!, request.selection!!)

                TurnCommandKind.REASSIGN_CITY_TILE -> PlayerCityManagementOperations(player).tryReassignTile(city!!, destination!!)

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

                TurnCommandKind.DISMISS_NOTIFICATIONS -> {
                    // Presentation state only. Keep native messages for UI history and combat reporting.
                    state.dismissedNotifications.addAll(detachedRequest.targets)
                    true
                }

                TurnCommandKind.DECLARE_WAR -> PlayerDiplomacyOperations(player).tryDeclareWar(partner!!)

                TurnCommandKind.END_TURN -> {
                    state.previousTurnReview = CompletedTurnReview(
                        before.turn,
                        immutable(state.reviewActions),
                        immutable(before.turnReview!!.deferredUnits()),
                        request.deferReason,
                    )
                    state.reviewActions.clear()
                    state.reviewTurn = null
                    state.deliveredCombatReports = state.combatReports.size
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
            if (request.kind != TurnCommandKind.DISMISS_NOTIFICATIONS) game.checkForVictory()
            state.invalidate()
            runtime.captureCombatReports()
            state.freeGreatPersonDecision = null
            val after = snapshot(game, player, state)
            val economyResult = if (request.kind in economicCommands) economicOutcome(request, before, after) else null
            if (request.kind != TurnCommandKind.END_TURN) {
                state.reviewActions.add(confirmedAction(request, before, after, combatResult, economyResult))
                // This is part of the same mutation/revision. Reuse the just-built map observation.
                state.fingerprint = state.fingerprint!!.let { fingerprint ->
                    fingerprint.copy(turnReview = fingerprint.turnReview?.copy(actions = immutable(state.reviewActions)))
                }
            }
            val receipt = TurnCommandReceipt(
                requestId = request.requestId,
                epoch = runtime.epoch,
                kind = request.kind,
                revision = after.revision,
                turn = game.turns,
                isYourTurn = game.currentPlayer == player.civID,
                feedback = when (request.kind) {
                    TurnCommandKind.DECLARE_WAR -> PlayerDiplomacyOperations(player).declarationResponse(partner!!)?.toPlayerContent()
                    else -> null
                },
                combat = combatResult,
                economy = economyResult,
                cityManagement = if (request.kind in cityManagementCommands) cityManagementOutcome(before, after) else null,
                greatPerson = if (request.kind == TurnCommandKind.ACTIVATE_GREAT_PERSON) greatPersonOutcome(request, before, after, player.policies.storedCulture - cultureBefore) else null,
                dismissedNotifications = dismissed,
            )
            state.receipts[request.requestId] = detachedRequest to receipt
            runtime.communication.enacted(player.civID, partner?.civID, receipt)
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
        val captures = PlayerCaptureOperations(player)
        val pending = PlayerTurnRequirements.pending(player).map { kind ->
            PendingTurnDecision(kind.name, kind in supportedRequirements)
        }.toMutableList()
        val phase = when {
            runtime.isPaused() -> "PAUSED"
            player.isDefeated() -> "DEFEATED"
            gameFinished(game) -> "FINISHED"
            game.currentPlayer == player.civID && game.currentPlayerCiv === player -> "ACTIVE"
            else -> "WAITING"
        }
        val negotiating = runtime.communicationState.trades.activeProposal != null
        val ordinaryActions = phase == "ACTIVE" && !negotiating && !gameHasResult(game) && !player.hasCaptureDecision()
        if (phase == "ACTIVE" && state.reviewTurn != game.turns) {
            state.reviewTurn = game.turns
            state.reviewActions.clear()
        }
        state.alertHandles.keys.removeIf { old -> player.popupAlerts.none { it === old } }
        val alerts = player.popupAlerts.map { alert ->
            val contact = observeFirstContact(operations, alert)
            val content = observeAlertContent(operations, alert)
            val capture = captures.captureDecision(alert)?.observation(active = phase == "ACTIVE" && !negotiating)
            val friendship = PlayerFriendshipOperations(player).decision(alert)?.let {
                FriendshipDecision(
                    it.civilizationId,
                    it.content.toPlayerContent(),
                    DiplomaticAction(ordinaryActions && it.unavailableReason == null, immutable(listOfNotNull(it.unavailableReason))),
                    ordinaryActions,
                )
            }
            val informational = alert.type in PlayerOperations.informationalAlerts && content != null
            val supported = informational || capture != null || friendship != null
            PlayerAlert(
                handle = state.alertHandles.getOrPut(alert) { state.identifiers.next(PlayerIdentifier.ALERT) },
                kind = alert.type.name,
                subject = when (alert.type) {
                    AlertType.TechResearched, AlertType.WonderBuilt -> alert.value.takeIf { content != null }
                    AlertType.FirstContact -> contact?.civilizationName
                    else -> null
                },
                canAcknowledge = phase == "ACTIVE" && !negotiating && !gameHasResult(game) && informational,
                firstContact = contact,
                content = content,
                capture = capture,
                friendship = friendship,
            ) to supported
        }
        if (alerts.isNotEmpty()) {
            // Support is a capability even while paused or waiting; action flags require an active turn.
            pending.add(PendingTurnDecision("PopupAlerts", alerts.all { it.second }))
        }
        val diplomacy = observeDiplomacy(player, active = ordinaryActions, communicationAvailable = !runtime.communicationPaused && !gameHasResult(game))
        if (player.tradeRequests.isNotEmpty()) {
            // Canonical proposals never create engine requests. Imported UI requests are explicit blockers.
            pending.add(PendingTurnDecision("TradeRequests", false))
        }
        if (player.shouldShowDiplomaticVotingResults()) pending.add(PendingTurnDecision("DiplomaticVotingResults", false))
        val map = observeMap(player, state, canAct = ordinaryActions)
        val fingerprint = PlayerTurnSnapshot(
            epoch = runtime.epoch,
            revision = "",
            turn = game.turns,
            phase = phase,
            canEndTurn = ordinaryActions && pending.isEmpty(),
            negotiation = runtime.communication.trades.negotiation(civilizationId),
            pending = immutable(pending),
            alerts = immutable(alerts.map { it.first }),
            notifications = observeNotifications(player, state, ordinaryActions),
            cities = immutable(
                player.cities.filter { it.civ === player }.map { city ->
                    TurnCity(
                        handle = state.cityHandle(city.id),
                        name = city.name,
                        puppet = city.isPuppet,
                        constructionQueue = immutable(city.cityConstructions.constructionQueue),
                        constructionOptions = immutable(operations.queueableConstructions(city).sorted()),
                        development = observeCityDevelopment(city, active = ordinaryActions),
                        attacks = immutable(if (ordinaryActions) PlayerCombatOperations(player).attacks(city).map { it.observation() } else emptyList()),
                        captureOptions = immutable(captures.cityChoices(city).map { it.observation(ordinaryActions) }),
                        purchases = observePurchases(city, active = ordinaryActions),
                        management = observeCityManagement(city, ordinaryActions) { owned -> state.cityHandle(owned.id) },
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
            map = map,
            research = observeResearchDevelopment(player),
            diplomacy = diplomacy,
            economy = observeEconomy(player),
            turnReview = if (phase == "ACTIVE") {
                TurnReview(
                    id = "",
                    actions = immutable(state.reviewActions),
                    units = immutable(map.units.map { ReviewUnit(it.handle, it.name, it.position, it.movement, it.idle, it.moves.size) }),
                )
            } else {
                null
            },
            previousTurnReview = state.previousTurnReview,
            rules = observeGameRules(game),
            scenario = communicationScenario(runtime, game, player),
            combatReports = immutable(state.combatReports.values.drop(state.deliveredCombatReports)),
            result = observeResult(player),
        )
        if (state.fingerprint != fingerprint) {
            state.fingerprint = fingerprint
            state.revision = state.identifiers.next(PlayerIdentifier.REVISION)
        }
        return fingerprint.copy(revision = state.revision, turnReview = fingerprint.turnReview?.copy(id = state.revision))
    }

    private fun player(game: GameInfo): Civilization = game.civilizations.singleOrNull {
        it.civID == civilizationId && it.isHuman() && it.isMajorCiv()
    } ?: fail(PlayerSessionFailure.PLAYER_UNAVAILABLE)

    private fun state() = runtime.turnSessions.getOrPut(civilizationId) { TurnSessionState() }

    private fun validateShape(request: TurnCommandRequest) {
        val needsTarget = request.kind in cityCommands || request.kind in unitCommands || request.kind in partnerCommands || request.kind in setOf(TurnCommandKind.ACKNOWLEDGE_ALERT, TurnCommandKind.RESOLVE_CAPTURED_CITY, TurnCommandKind.RESPOND_FRIENDSHIP)
        val needsSelection = request.kind in setOf(
            TurnCommandKind.CHOOSE_FREE_TECHNOLOGY,
            TurnCommandKind.ADOPT_POLICY,
            TurnCommandKind.QUEUE_CONSTRUCTION,
            TurnCommandKind.CHOOSE_FREE_GREAT_PERSON,
            TurnCommandKind.RENAME_CITY,
            TurnCommandKind.START_WORKER_JOB,
            TurnCommandKind.SET_UNIT_ORDER,
            TurnCommandKind.PROMOTE_UNIT,
            TurnCommandKind.RESOLVE_CAPTURED_CITY,
            TurnCommandKind.RESPOND_FRIENDSHIP,
            TurnCommandKind.MANAGE_CAPTURED_CITY,
            TurnCommandKind.BUY_CONSTRUCTION,
            TurnCommandKind.UPGRADE_UNIT,
            TurnCommandKind.SET_CITY_FOCUS,
            TurnCommandKind.ASSIGN_SPECIALIST,
            TurnCommandKind.UNASSIGN_SPECIALIST,
            TurnCommandKind.SELL_BUILDING,
            TurnCommandKind.ACTIVATE_GREAT_PERSON,
        )
        if (needsTarget != (request.target != null) || needsSelection != (request.selection != null) ||
            request.target?.isEmpty() == true || request.selection?.isEmpty() == true || request.revision.isEmpty() ||
            (request.kind == TurnCommandKind.SET_RESEARCH_QUEUE) != request.queue.isNotEmpty() ||
            request.queue.size > 1024 || request.queue.any { it.isEmpty() } ||
            (request.kind == TurnCommandKind.DISMISS_NOTIFICATIONS) != request.targets.isNotEmpty() ||
            request.targets.size > 256 || request.targets.any { it.isBlank() || it.length > 128 } || request.targets.distinct().size != request.targets.size ||
            (request.kind in setOf(TurnCommandKind.MOVE_UNIT, TurnCommandKind.ATTACK, TurnCommandKind.CITY_ATTACK) || request.kind in tileCommands) != (request.destination != null) ||
            (request.kind == TurnCommandKind.ATTACK) != (request.attackFrom != null) ||
            (request.kind == TurnCommandKind.SET_WORKED_TILE) != (request.worked != null) ||
            (request.kind == TurnCommandKind.SET_TILE_LOCKED) != (request.locked != null) ||
            (request.kind in setOf(TurnCommandKind.SET_AVOID_GROWTH, TurnCommandKind.SET_MANUAL_SPECIALISTS)) != (request.enabled != null) ||
            (request.kind in setOf(TurnCommandKind.REMOVE_CONSTRUCTION, TurnCommandKind.MOVE_CONSTRUCTION)) != (request.index != null) ||
            (request.kind == TurnCommandKind.MOVE_CONSTRUCTION) != (request.toIndex != null) ||
            (request.index != null && request.index < 0) || (request.toIndex != null && request.toIndex < 0) ||
            (request.confirmBreakPromise && request.kind != TurnCommandKind.FOUND_CITY) ||
            (request.confirmAction && request.kind !in economicCommands) ||
            (request.kind == TurnCommandKind.DECLARE_WAR) != request.confirmWar ||
            (request.kind != TurnCommandKind.END_TURN && (request.reviewId != null || request.deferReason != null)) ||
            (request.reviewId != null && request.reviewId.length !in 1..128) ||
            (request.deferReason != null && (request.deferReason.isBlank() || request.deferReason.length > 500 || request.deferReason.any { it.isISOControl() }))
        ) {
            fail(PlayerSessionFailure.INVALID_ARGUMENT)
        }
    }

    companion object {
        private val partnerCommands = setOf(TurnCommandKind.DECLARE_WAR)
        private val cityManagementCommands = setOf(TurnCommandKind.SET_CITY_FOCUS, TurnCommandKind.SET_AVOID_GROWTH, TurnCommandKind.SET_MANUAL_SPECIALISTS, TurnCommandKind.ASSIGN_SPECIALIST, TurnCommandKind.UNASSIGN_SPECIALIST, TurnCommandKind.RESET_CITIZENS, TurnCommandKind.SELL_BUILDING, TurnCommandKind.REASSIGN_CITY_TILE)
        private val tileCommands = setOf(TurnCommandKind.SET_WORKED_TILE, TurnCommandKind.SET_TILE_LOCKED, TurnCommandKind.BUY_TILE, TurnCommandKind.REASSIGN_CITY_TILE)
        private val cityCommands = tileCommands + cityManagementCommands + setOf(TurnCommandKind.RENAME_CITY, TurnCommandKind.QUEUE_CONSTRUCTION, TurnCommandKind.REMOVE_CONSTRUCTION, TurnCommandKind.MOVE_CONSTRUCTION, TurnCommandKind.CITY_ATTACK, TurnCommandKind.MANAGE_CAPTURED_CITY, TurnCommandKind.BUY_CONSTRUCTION)
        private val unitCommands = setOf(TurnCommandKind.MOVE_UNIT, TurnCommandKind.FOUND_CITY, TurnCommandKind.START_WORKER_JOB, TurnCommandKind.STOP_WORKER_JOB, TurnCommandKind.ATTACK, TurnCommandKind.SET_UNIT_ORDER, TurnCommandKind.PROMOTE_UNIT, TurnCommandKind.UPGRADE_UNIT, TurnCommandKind.PILLAGE, TurnCommandKind.DISBAND_UNIT, TurnCommandKind.ACTIVATE_GREAT_PERSON)
        private val economicCommands = setOf(TurnCommandKind.BUY_CONSTRUCTION, TurnCommandKind.BUY_TILE, TurnCommandKind.UPGRADE_UNIT, TurnCommandKind.PILLAGE, TurnCommandKind.DISBAND_UNIT, TurnCommandKind.SELL_BUILDING)
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
