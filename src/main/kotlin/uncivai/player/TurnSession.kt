package uncivai.player

import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.CaptureChoice
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerCaptureOperations
import com.unciv.logic.civilization.PlayerCityOperations
import com.unciv.logic.civilization.PlayerCombatOperations
import com.unciv.logic.civilization.PlayerDiplomacyOperations
import com.unciv.logic.civilization.PlayerOperations
import com.unciv.logic.civilization.PlayerPeaceOperations
import com.unciv.logic.civilization.PlayerTurnRequirements
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.civilization.PlayerUnitOrders
import com.unciv.logic.civilization.PlayerWorkerOperations
import com.unciv.logic.civilization.PopupAlert
import com.unciv.logic.trade.TradeOfferType
import com.unciv.logic.trade.TradeRequest
import uncivai.GameExecutor
import java.util.Collections
import java.util.UUID

/** Reconnect-stable, runtime-owned state. Access only under the game executor. */
internal class TurnSessionState {
    val cityHandles = HashMap<String, String>()
    val unitHandles = HashMap<Int, String>()
    val alertHandles = java.util.IdentityHashMap<PopupAlert, String>()
    val tradeHandles = java.util.IdentityHashMap<TradeRequest, String>()
    val receipts = HashMap<String, Pair<TurnCommandRequest, TurnCommandReceipt>>()
    var fingerprint: PlayerTurnSnapshot? = null
    var revision = UUID.randomUUID().toString()

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
}

internal class TurnSession(private val runtime: GameExecutor, private val civilizationId: String) {
    fun snapshot(): PlayerTurnSnapshot = runtime.accessGame { game ->
        runtime.captureCombatReports()
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
        if (before.phase != "ACTIVE") fail(PlayerSessionFailure.COMMAND_UNAVAILABLE)
        if (gameHasResult(game) && request.kind != TurnCommandKind.RESOLVE_CAPTURED_CITY) fail(PlayerSessionFailure.COMMAND_UNAVAILABLE)
        if (player.hasCaptureDecision() && request.kind !in setOf(TurnCommandKind.RESOLVE_CAPTURED_CITY, TurnCommandKind.ACKNOWLEDGE_ALERT)) fail(PlayerSessionFailure.TURN_BLOCKED)
        val operations = PlayerOperations(player)
        val captures = PlayerCaptureOperations(player)
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
        val peace = PlayerPeaceOperations(player)
        val partner = if (request.kind in partnerCommands) {
            val offered = before.diplomacy.knownCivilizations.singleOrNull { it.civilizationId == request.target }
                ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            val available = when (request.kind) {
                TurnCommandKind.DECLARE_WAR -> offered.declareWar.available
                TurnCommandKind.PROPOSE_PEACE -> offered.negotiatePeace.available
                TurnCommandKind.RETRACT_PEACE -> before.diplomacy.outgoingOffers.any { it.civilizationId == request.target && it.retract.available }
                else -> false
            }
            if (!available) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            game.civilizations.single { it.civID == offered.civilizationId }
        } else {
            null
        }
        val trade = if (request.kind in responseCommands) {
            val offered = before.diplomacy.incomingOffers.singleOrNull { it.handle == request.target }
                ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            val available = when (request.kind) {
                TurnCommandKind.ACCEPT_PEACE -> offered.accept.available
                TurnCommandKind.REJECT_PEACE -> offered.reject.available
                TurnCommandKind.COUNTER_PEACE -> offered.counteroffer.available
                else -> false
            }
            if (!available) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            state.tradeHandles.entries.singleOrNull { it.value == request.target }?.key
                ?.takeIf { candidate -> player.tradeRequests.any { it === candidate } }
                ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
        } else {
            null
        }
        if ((request.kind == TurnCommandKind.DECLARE_WAR || request.kind in responseCommands) &&
            state.observedRevision != before.revision
        ) {
            fail(PlayerSessionFailure.OBSERVATION_REQUIRED)
        }
        val selectedTerms = detachedRequest.peaceTerms
        if (selectedTerms != null) {
            val otherId = partner?.civID ?: trade!!.requestingCiv
            val options = before.diplomacy.knownCivilizations.singleOrNull { it.civilizationId == otherId }?.peaceOptions
                ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            fun offered(selections: List<PeaceTermSelection>, available: List<PeaceTerm>) = selections.all { selection ->
                available.any {
                    it.supported && it.name == selection.name && it.type == selection.type &&
                        it.duration == selection.duration && selection.amount <= it.amount
                }
            }
            if (!offered(selectedTerms.give, options.give) || !offered(selectedTerms.receive, options.receive)) {
                fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            }
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
        } else if (request.kind == TurnCommandKind.ATTACK || request.kind == TurnCommandKind.CITY_ATTACK) {
            val offered = if (request.kind == TurnCommandKind.ATTACK) {
                before.map.units.single { it.handle == request.target }.combat!!.attacks
            } else {
                before.cities.single { it.handle == request.target }.attacks
            }
            val option = offered.singleOrNull { it.destination == request.destination && (request.kind == TurnCommandKind.CITY_ATTACK || it.attackFrom == request.attackFrom) }
                ?: fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
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
        if (request.kind == TurnCommandKind.SET_UNIT_ORDER && before.map.units.single { it.handle == request.target }.combat!!.orders.none { it.name == request.selection && it.available }) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
        if (request.kind == TurnCommandKind.PROMOTE_UNIT && before.map.units.single { it.handle == request.target }.combat!!.promotions?.choices?.none { it.name == request.selection && it.available } != false) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
        if (request.kind == TurnCommandKind.MANAGE_CAPTURED_CITY) {
            if (before.cities.single { it.handle == request.target }.captureOptions.none { it.name == request.selection && it.available }) fail(PlayerSessionFailure.OPTION_UNAVAILABLE)
            if (state.observedRevision != before.revision) fail(PlayerSessionFailure.OBSERVATION_REQUIRED)
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

                TurnCommandKind.MANAGE_CAPTURED_CITY -> captures.tryManageCity(city!!, CaptureChoice.valueOf(request.selection!!))

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

                TurnCommandKind.DECLARE_WAR -> PlayerDiplomacyOperations(player).tryDeclareWar(partner!!)

                TurnCommandKind.PROPOSE_PEACE -> peace.tryPropose(partner!!, selectedTerms!!.give.map { it.engineTerm() }, selectedTerms.receive.map { it.engineTerm() })

                TurnCommandKind.ACCEPT_PEACE -> peace.tryAccept(trade!!)

                TurnCommandKind.REJECT_PEACE -> peace.tryReject(trade!!)

                TurnCommandKind.COUNTER_PEACE -> peace.tryCounteroffer(trade!!, selectedTerms!!.give.map { it.engineTerm() }, selectedTerms.receive.map { it.engineTerm() })

                TurnCommandKind.RETRACT_PEACE -> peace.tryRetract(partner!!)

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
            game.checkForVictory()
            state.invalidate()
            runtime.captureCombatReports()
            runtime.freeGreatPersonDecisions.invalidate(player.civID)
            val after = snapshot(game, player, state)
            if (request.kind != TurnCommandKind.END_TURN) {
                state.reviewActions.add(confirmedAction(request, before, after, combatResult))
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
                    TurnCommandKind.ACCEPT_PEACE -> peace.acceptancePopupContent(game.getCivilization(trade!!.requestingCiv))?.toPlayerContent()
                    TurnCommandKind.DECLARE_WAR -> PlayerDiplomacyOperations(player).declarationResponse(partner!!)?.toPlayerContent()
                    else -> null
                },
                combat = combatResult,
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
        val ordinaryActions = phase == "ACTIVE" && !gameHasResult(game) && !player.hasCaptureDecision()
        if (phase == "ACTIVE" && state.reviewTurn != game.turns) {
            state.reviewTurn = game.turns
            state.reviewActions.clear()
        }
        state.alertHandles.keys.removeIf { old -> player.popupAlerts.none { it === old } }
        val alerts = player.popupAlerts.map { alert ->
            val contact = observeFirstContact(operations, alert)
            val content = observeAlertContent(operations, alert)
            val capture = captures.captureDecision(alert)?.observation(active = phase == "ACTIVE")
            val informational = alert.type in PlayerOperations.informationalAlerts && content != null
            val supported = informational || capture != null
            PlayerAlert(
                handle = state.alertHandles.getOrPut(alert) { UUID.randomUUID().toString() },
                kind = alert.type.name,
                subject = when (alert.type) {
                    AlertType.TechResearched, AlertType.WonderBuilt -> alert.value.takeIf { content != null }
                    AlertType.FirstContact -> contact?.civilizationName
                    else -> null
                },
                canAcknowledge = phase == "ACTIVE" && !gameHasResult(game) && informational,
                firstContact = contact,
                content = content,
                capture = capture,
            ) to supported
        }
        if (alerts.isNotEmpty()) {
            // Support is a capability even while paused or waiting; action flags require an active turn.
            pending.add(PendingTurnDecision("PopupAlerts", alerts.all { it.second }))
        }
        val diplomacy = observeDiplomacy(player, state, active = ordinaryActions)
        if (player.tradeRequests.isNotEmpty()) {
            // Even an unsupported offer can be read and rejected if its sender is a known major.
            pending.add(PendingTurnDecision("TradeRequests", diplomacy.incomingOffers.size == player.tradeRequests.size))
        }
        if (player.shouldShowDiplomaticVotingResults()) pending.add(PendingTurnDecision("DiplomaticVotingResults", false))
        val map = observeMap(player, state, canAct = ordinaryActions)
        val fingerprint = PlayerTurnSnapshot(
            epoch = runtime.epoch,
            revision = "",
            turn = game.turns,
            phase = phase,
            canEndTurn = ordinaryActions && pending.isEmpty(),
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
                        development = observeCityDevelopment(city, active = ordinaryActions),
                        attacks = immutable(if (ordinaryActions) PlayerCombatOperations(player).attacks(city).map { it.observation() } else emptyList()),
                        captureOptions = immutable(captures.cityChoices(city).map { it.observation(ordinaryActions) }),
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
            scenario = scenario(game, player, diplomacy),
            combatReports = immutable(state.combatReports.values.drop(state.deliveredCombatReports)),
            result = observeResult(player),
        )
        if (state.fingerprint != fingerprint) {
            state.fingerprint = fingerprint
            state.revision = UUID.randomUUID().toString()
        }
        return fingerprint.copy(revision = state.revision, turnReview = fingerprint.turnReview?.copy(id = state.revision))
    }

    private fun scenario(game: GameInfo, player: Civilization, diplomacy: DiplomacyObservation): GameplayScenario? {
        if (game.tileMap.mapParameters.name != "Close-start duel") return null
        val opponent = diplomacy.knownCivilizations.singleOrNull() ?: return GameplayScenario("CLOSE_START_DIPLOMACY", "AWAITING_WAR")
        // Only the public declaration between these two known players contributes; never expose other receipts.
        val warOccurred = runtime.turnSessions.any { (actor, session) ->
            session.receipts.values.any { (command, _) ->
                command.kind == TurnCommandKind.DECLARE_WAR &&
                    (
                        (actor == player.civID && command.target == opponent.civilizationId) ||
                            (actor == opponent.civilizationId && command.target == player.civID)
                        )
            }
        }
        return GameplayScenario(
            "CLOSE_START_DIPLOMACY",
            when {
                opponent.atWar -> "AT_WAR"
                warOccurred -> "COMPLETE"
                else -> "AWAITING_WAR"
            },
        )
    }

    private fun player(game: GameInfo): Civilization = game.civilizations.singleOrNull {
        it.civID == civilizationId && it.isHuman() && it.isMajorCiv()
    } ?: fail(PlayerSessionFailure.PLAYER_UNAVAILABLE)

    private fun state() = runtime.turnSessions.getOrPut(civilizationId) { TurnSessionState() }

    private fun validateShape(request: TurnCommandRequest) {
        val needsTarget = request.kind in cityCommands || request.kind in unitCommands || request.kind in partnerCommands || request.kind in responseCommands || request.kind in setOf(TurnCommandKind.ACKNOWLEDGE_ALERT, TurnCommandKind.RESOLVE_CAPTURED_CITY)
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
            TurnCommandKind.MANAGE_CAPTURED_CITY,
        )
        if (needsTarget != (request.target != null) || needsSelection != (request.selection != null) ||
            request.target?.isEmpty() == true || request.selection?.isEmpty() == true || request.revision.isEmpty() ||
            (request.kind == TurnCommandKind.SET_RESEARCH_QUEUE) != request.queue.isNotEmpty() ||
            request.queue.size > 1024 || request.queue.any { it.isEmpty() } ||
            (request.kind in setOf(TurnCommandKind.MOVE_UNIT, TurnCommandKind.ATTACK, TurnCommandKind.CITY_ATTACK) || request.kind in tileCommands) != (request.destination != null) ||
            (request.kind == TurnCommandKind.ATTACK) != (request.attackFrom != null) ||
            (request.kind == TurnCommandKind.SET_WORKED_TILE) != (request.worked != null) ||
            (request.kind == TurnCommandKind.SET_TILE_LOCKED) != (request.locked != null) ||
            (request.kind in setOf(TurnCommandKind.REMOVE_CONSTRUCTION, TurnCommandKind.MOVE_CONSTRUCTION)) != (request.index != null) ||
            (request.kind == TurnCommandKind.MOVE_CONSTRUCTION) != (request.toIndex != null) ||
            (request.index != null && request.index < 0) || (request.toIndex != null && request.toIndex < 0) ||
            (request.confirmBreakPromise && request.kind != TurnCommandKind.FOUND_CITY) ||
            (request.kind == TurnCommandKind.DECLARE_WAR) != request.confirmWar ||
            (request.kind != TurnCommandKind.END_TURN && (request.reviewId != null || request.deferReason != null)) ||
            (request.reviewId != null && request.reviewId.length !in 1..128) ||
            (request.deferReason != null && (request.deferReason.isBlank() || request.deferReason.length > 500 || request.deferReason.any { it.isISOControl() })) ||
            (request.kind in setOf(TurnCommandKind.PROPOSE_PEACE, TurnCommandKind.COUNTER_PEACE)) != (request.peaceTerms != null)
        ) {
            fail(PlayerSessionFailure.INVALID_ARGUMENT)
        }
        request.peaceTerms?.let { terms ->
            for (side in listOf(terms.give, terms.receive)) {
                if (side.isEmpty() || side.size > 256 || side.any { it.name.isEmpty() || it.name.length > 256 || it.amount <= 0 || TradeOfferType.entries.none { type -> type.name == it.type } } ||
                    side.map { it.type to it.name }.toSet().size != side.size
                ) {
                    fail(PlayerSessionFailure.INVALID_ARGUMENT)
                }
            }
        }
    }

    companion object {
        private val partnerCommands = setOf(TurnCommandKind.DECLARE_WAR, TurnCommandKind.PROPOSE_PEACE, TurnCommandKind.RETRACT_PEACE)
        private val responseCommands = setOf(TurnCommandKind.ACCEPT_PEACE, TurnCommandKind.REJECT_PEACE, TurnCommandKind.COUNTER_PEACE)
        private val tileCommands = setOf(TurnCommandKind.SET_WORKED_TILE, TurnCommandKind.SET_TILE_LOCKED)
        private val cityCommands = tileCommands + setOf(TurnCommandKind.RENAME_CITY, TurnCommandKind.QUEUE_CONSTRUCTION, TurnCommandKind.REMOVE_CONSTRUCTION, TurnCommandKind.MOVE_CONSTRUCTION, TurnCommandKind.CITY_ATTACK, TurnCommandKind.MANAGE_CAPTURED_CITY)
        private val unitCommands = setOf(TurnCommandKind.MOVE_UNIT, TurnCommandKind.FOUND_CITY, TurnCommandKind.START_WORKER_JOB, TurnCommandKind.STOP_WORKER_JOB, TurnCommandKind.ATTACK, TurnCommandKind.SET_UNIT_ORDER, TurnCommandKind.PROMOTE_UNIT)
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
