package uncivai

import com.unciv.json.json
import kotlinx.serialization.Serializable
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import java.io.IOException
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

@Serializable
private data class RollbackExpected(
    val priorRequest: TurnCommandRequest,
    val priorReceipt: TurnCommandReceipt,
    val before: PlayerTurnSnapshot,
    val mutated: PlayerTurnSnapshot,
    val attemptRequests: List<TurnCommandRequest>,
    val serverInstance: String,
    val activationId: String,
    val endRequest: TurnCommandRequest? = null,
    val endReceipt: TurnCommandReceipt? = null,
)

/** Direct operator/player calls keep these recovery tests independent of transport startup. */
object ScheduledRollbackProbe {
    private const val LEASE = "rollback-test-lease"

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        when (val stage = args[1]) {
            "mutate" -> mutate(root)
            "rollback" -> rollback(root)
            "verify" -> verify(root)
            "failure-before", "failure-after" -> persistenceFailure(root.resolve(stage), stage == "failure-after")
            else -> error("Unknown rollback fixture stage: $stage")
        }
    }

    private fun mutate(root: Path) {
        HeadlessRuntime.createDuel(root.resolve("initial"), 412L).use { runtime ->
            runtime.executor.enableCheckpoints(root.resolve("checkpoint.json"))
            val manual = PlayerHost(runtime).bindPlayer("Rome")
            val initial = manual.turn()
            val priorRequest = command(initial, TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf(initial.researchOptions.first()))
            val priorReceipt = manual.execute(priorRequest)
            val control = OperatorControl(runtime.executor)
            control.handle(OperatorRequest("acquire", LEASE))
            val response = activate(control)
            val player = checkNotNull(control.activationSession(checkNotNull(response.playerToken)))
            val before = player.turn()
            val rollback = rollbackRequest(response.status, checkNotNull(response.activationId))
            val unchanged = json().toJson(runtime.game)
            expectOperator("LEASE_LOST", control, rollback.copy(leaseId = "wrong-lease"))
            expectOperator("STALE_TURN", control, rollback.copy(epoch = "other-epoch"))
            expectOperator("STALE_TURN", control, rollback.copy(serverInstance = "other-server"))
            expectOperator("STALE_TURN", control, rollback.copy(handoff = 42))
            expectOperator("STALE_TURN", control, rollback.copy(civilization = "Greece"))
            expectOperator("INVALID_ARGUMENT", control, rollback.copy(activationId = null))
            expectOperator("STALE_ACTIVATION", control, rollback.copy(activationId = "other-activation"))
            check(json().toJson(runtime.game) == unchanged)
            val commands = alterTurn(player)
            val mutated = player.turn()
            check(mutated.cities.size == 1 && mutated.map.units.none { it.name == "Settler" })
            check(mutated.researchQueue != before.researchQueue)
            check(mutated.map.units.single { it.name == "Warrior" }.position != before.map.units.single { it.name == "Warrior" }.position)
            check(mutated.turnReview!!.actions.map { it.kind } == before.turnReview!!.actions.map { it.kind } + commands.map { it.kind })
            val checkpoint = readCheckpoint(root.resolve("checkpoint.json"))
            check(checkpoint.turnStart?.handoff == 0L && checkpoint.players.getValue("Rome").receipts.size > 1)
            Files.writeString(
                root.resolve("expected.json"),
                PlayerTurnJson.json.encodeToString(RollbackExpected(priorRequest, priorReceipt, before, mutated, commands, response.status.serverInstance, checkNotNull(response.activationId))),
            )
            // Leave an interrupted activation in the durable checkpoint.
        }
    }

    private fun rollback(root: Path) {
        val saved = expected(root)
        HeadlessRuntime.loadCheckpoint(root.resolve("checkpoint.json"), root.resolve("restored")).use { runtime ->
            val control = OperatorControl(runtime.executor)
            val restored = control.handle(OperatorRequest("status")).status
            check(restored.phase == "PAUSED" && restored.handoff == 0L && restored.epoch == saved.before.epoch)
            check(restored.serverInstance != saved.serverInstance && restored.activeCivilization == "Rome")
            check(runtime.game.getCivilization("Rome").cities.size == 1) { "Restart unexpectedly undid an uncommitted attempt before rollback" }
            control.handle(OperatorRequest("acquire", LEASE))
            expectOperator("STALE_TURN", control, rollbackRequest(restored, saved.activationId).copy(serverInstance = saved.serverInstance))
            val response = activate(control)
            val token = checkNotNull(response.playerToken)
            val discardedPlayer = checkNotNull(control.activationSession(token))
            check(discardedPlayer.turn().researchQueue == saved.mutated.researchQueue)
            check(discardedPlayer.turn().turnReview!!.actions == saved.mutated.turnReview!!.actions)
            expectOperator("STALE_ACTIVATION", control, rollbackRequest(response.status, saved.activationId))
            val request = rollbackRequest(response.status, checkNotNull(response.activationId))
            val rolledBack = control.handle(request).status
            check(rolledBack.phase == "PAUSED" && rolledBack.handoff == 0L && rolledBack.epoch == saved.before.epoch)
            check(control.activationSession(token) == null)
            expectPlayer(PlayerSessionFailure.COMMAND_UNAVAILABLE) { discardedPlayer.turn() }
            val resetSnapshot = PlayerHost(runtime).bindPlayer("Rome").turn()
            checkReset(resetSnapshot, saved)
            val receipts = runtime.executor.turnSessions.getValue("Rome").receipts
            check(receipts[saved.priorRequest.requestId]?.second == saved.priorReceipt)
            check(saved.attemptRequests.none { it.requestId in receipts }) { "Discarded command receipts survived rollback" }
            val resetGame = json().toJson(runtime.game)
            // The response was lost: the same rollback request is harmless and durable.
            val repeated = control.handle(request).status
            check(repeated.handoff == rolledBack.handoff && repeated.turn == rolledBack.turn && repeated.phase == "PAUSED")
            check(json().toJson(runtime.game) == resetGame)
            check(PlayerHost(runtime).bindPlayer("Rome").turn().revision == resetSnapshot.revision) { "Repeated rollback rotated the revision again" }
            val abortedActivation = OperatorRequest("activate", LEASE, rolledBack.epoch, rolledBack.serverInstance, rolledBack.handoff, "Rome", request.activationId)
            expectOperator("STALE_ACTIVATION", control, abortedActivation)
            val fresh = activate(control)
            check(fresh.playerToken != token)
            val player = checkNotNull(control.activationSession(checkNotNull(fresh.playerToken)))
            checkReset(player.turn(), saved)
            check(player.execute(saved.priorRequest) == saved.priorReceipt) { "Rollback lost a previously completed command receipt" }
            expectPlayer(PlayerSessionFailure.STALE_REVISION) { player.execute(saved.attemptRequests.first()) }
            val stale = command(saved.before, TurnCommandKind.SET_RESEARCH_QUEUE, queue = saved.before.researchQueue)
            expectPlayer(PlayerSessionFailure.STALE_REVISION) { player.execute(stale) }
            val freshTurn = player.turn()
            val warrior = freshTurn.map.units.single { it.name == "Warrior" }
            player.execute(command(freshTurn, TurnCommandKind.MOVE_UNIT, warrior.handle).copy(destination = warrior.moves.first().destination))
            val freshState = player.turn()
            val freshGame = json().toJson(runtime.game)
            expectOperator("STALE_ACTIVATION", control, request)
            expectOperator("STALE_ACTIVATION", control, abortedActivation)
            check(control.activationSession(checkNotNull(fresh.playerToken)) != null)
            check(player.turn() == freshState && json().toJson(runtime.game) == freshGame) { "A delayed rollback erased the fresh attempt" }
            val endRequest = finishTurn(player)
            check(player.turn().cities.single().handle != saved.mutated.cities.single().handle) {
                "Rollback recycled a discarded city handle"
            }
            val endReceipt = player.execute(endRequest)
            val finished = control.handle(OperatorRequest("status")).status
            check(finished.handoff == 1L && finished.activeCivilization == "Greece")
            check(readCheckpoint(root.resolve("checkpoint.json")).turnStart == null)
            val committedGame = json().toJson(runtime.game)
            expectOperator("STALE_TURN", control, request)
            expectOperator("TURN_CHECKPOINT_UNAVAILABLE", control, rollbackRequest(finished, "not-yet-activated"))
            check(json().toJson(runtime.game) == committedGame)
            check(PlayerHost(runtime).bindPlayer("Rome").execute(endRequest) == endReceipt)
            Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(saved.copy(endRequest = endRequest, endReceipt = endReceipt)))
        }
    }

    private fun verify(root: Path) {
        val saved = expected(root)
        HeadlessRuntime.loadCheckpoint(root.resolve("checkpoint.json"), root.resolve("committed-restart")).use { runtime ->
            val control = OperatorControl(runtime.executor)
            val status = control.handle(OperatorRequest("status")).status
            check(status.epoch == saved.before.epoch && status.handoff == 1L && status.activeCivilization == "Greece" && status.phase == "PAUSED")
            check(runtime.game.getCivilization("Rome").cities.size == 1)
            val player = PlayerHost(runtime).bindPlayer("Rome")
            check(player.execute(saved.priorRequest) == saved.priorReceipt)
            check(player.execute(checkNotNull(saved.endRequest)) == saved.endReceipt)
            val review = checkNotNull(player.turn().previousTurnReview)
            check(review.actions.count { it.kind == TurnCommandKind.SET_RESEARCH_QUEUE } == 1) { "Discarded research commands polluted the completed review" }
            check(review.actions.count { it.kind == TurnCommandKind.FOUND_CITY } == 1)
            check(review.actions.count { it.kind == TurnCommandKind.MOVE_UNIT } == 1)
            val receipts = runtime.executor.turnSessions.getValue("Rome").receipts
            check(saved.attemptRequests.none { it.requestId in receipts })
            control.handle(OperatorRequest("acquire", LEASE))
            expectOperator("STALE_TURN", control, rollbackRequest(status, saved.activationId).copy(handoff = 0, civilization = "Rome"))
            // Acquiring the next player's turn creates its own rollback point.
            // Restoring it must preserve all committed effects of Rome's turn.
            control.handle(rollbackRequest(status, "greece-before-activate"))
            check(runtime.game.getCivilization("Rome").cities.size == 1)
            check(player.execute(checkNotNull(saved.endRequest)) == saved.endReceipt)
        }
    }

    private fun persistenceFailure(root: Path, afterReplace: Boolean) {
        val path = root.resolve("checkpoint.json")
        val injected = IOException("Injected rollback persistence failure")
        var armed = false
        val fail = { if (armed) throw injected }
        val store = if (afterReplace) CheckpointStore(path, afterReplace = fail) else CheckpointStore(path, beforeReplace = fail)
        lateinit var before: PlayerTurnSnapshot
        lateinit var activationId: String
        HeadlessRuntime.createDuel(root.resolve("initial"), 412L).use { runtime ->
            runtime.executor.enableCheckpoints(store)
            val control = OperatorControl(runtime.executor)
            control.handle(OperatorRequest("acquire", LEASE))
            val activation = activate(control)
            activationId = checkNotNull(activation.activationId)
            val token = checkNotNull(activation.playerToken)
            val player = checkNotNull(control.activationSession(token))
            before = player.turn()
            alterTurn(player)
            armed = true
            val failure = runCatching { control.handle(rollbackRequest(activation.status, activationId)) }.exceptionOrNull()
            check(failure is RuntimeHaltedException)
            check(runtime.mutationFailure === injected)
            check(control.handle(OperatorRequest("status")).status.phase == "HALTED")
            check(control.activationSession(token) == null)
            expectOperator("GAME_HALTED", control, OperatorRequest("activate", LEASE, before.epoch, activation.status.serverInstance, 0, "Rome", "new-activation"))
        }
        HeadlessRuntime.loadCheckpoint(path, root.resolve("recovered")).use { runtime ->
            val control = OperatorControl(runtime.executor)
            val status = control.handle(OperatorRequest("status")).status
            check(status.epoch == before.epoch && status.handoff == 0L && status.phase == "PAUSED")
            check(runtime.game.getCivilization("Rome").cities.size == if (afterReplace) 0 else 1)
            val recoveredSnapshot = PlayerHost(runtime).bindPlayer("Rome").turn()
            control.handle(OperatorRequest("acquire", LEASE))
            control.handle(rollbackRequest(status, activationId))
            val player = PlayerHost(runtime).bindPlayer("Rome")
            val restored = player.turn()
            if (afterReplace) check(restored.revision == recoveredSnapshot.revision) { "Restart lost the completed rollback identity" }
            check(restored.cities.isEmpty() && restored.researchQueue == before.researchQueue)
            check(restored.map.units.map { it.name to it.position } == before.map.units.map { it.name to it.position })
            check(runtime.executor.turnSessions.getValue("Rome").receipts.isEmpty())
            check(readCheckpoint(path).turnStart != null)
        }
    }

    private fun checkReset(snapshot: PlayerTurnSnapshot, saved: RollbackExpected) {
        check(snapshot.epoch == saved.before.epoch && snapshot.turn == saved.before.turn)
        check(snapshot.revision != saved.before.revision && snapshot.revision != saved.mutated.revision)
        check(snapshot.cities == saved.before.cities && snapshot.researchQueue == saved.before.researchQueue)
        check(snapshot.map.tiles == saved.before.map.tiles) { "Rollback failed to restore explored map information" }
        check(snapshot.map.units.map { listOf(it.handle, it.name, it.position, it.movement, it.health) } == saved.before.map.units.map { listOf(it.handle, it.name, it.position, it.movement, it.health) })
        check(snapshot.previousTurnReview == saved.before.previousTurnReview)
        if (snapshot.phase == "ACTIVE") {
            check(snapshot.turnReview!!.actions == saved.before.turnReview!!.actions) { "Rollback failed to restore confirmed actions" }
            check(snapshot.turnReview.units == saved.before.turnReview.units)
        } else {
            check(snapshot.turnReview == null)
        }
    }

    private fun alterTurn(player: PlayerSession): List<TurnCommandRequest> {
        val requests = mutableListOf<TurnCommandRequest>()
        fun execute(request: TurnCommandRequest) {
            player.execute(request)
            requests += request
        }
        var snapshot = player.turn()
        execute(command(snapshot, TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf(snapshot.researchOptions.first { it !in snapshot.researchQueue })))
        snapshot = player.turn()
        val settler = snapshot.map.units.single { it.canFoundCity && it.settlementPromises.isEmpty() }
        execute(command(snapshot, TurnCommandKind.FOUND_CITY, target = settler.handle))
        snapshot = player.turn()
        val warrior = snapshot.map.units.single { it.name == "Warrior" }
        execute(command(snapshot, TurnCommandKind.MOVE_UNIT, target = warrior.handle).copy(destination = warrior.moves.first().destination))
        return requests
    }

    private fun finishTurn(player: PlayerSession): TurnCommandRequest {
        repeat(40) {
            val snapshot = player.turn()
            val settler = snapshot.map.units.firstOrNull { it.canFoundCity && it.settlementPromises.isEmpty() }
            val city = snapshot.cities.firstOrNull { it.constructionQueue.isEmpty() }
            val alert = snapshot.alerts.firstOrNull { it.canAcknowledge }
            val request = when {
                settler != null && snapshot.cities.isEmpty() -> command(snapshot, TurnCommandKind.FOUND_CITY, settler.handle)
                alert != null -> command(snapshot, TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle)
                snapshot.researchQueue.isEmpty() -> command(snapshot, TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf(snapshot.researchOptions.first()))
                city != null -> command(snapshot, TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, city.constructionOptions.first())
                snapshot.policyOptions.isNotEmpty() && snapshot.pending.any { it.kind == "Policy" } -> command(snapshot, TurnCommandKind.ADOPT_POLICY, selection = snapshot.policyOptions.first())
                snapshot.canEndTurn -> command(snapshot, TurnCommandKind.END_TURN)
                else -> error("Unsupported fixture decision: ${snapshot.pending}")
            }
            player.execute(request)
            if (request.kind == TurnCommandKind.END_TURN) return request
        }
        error("Fixture turn did not finish")
    }

    private fun activate(control: OperatorControl): OperatorResponse {
        val status = control.handle(OperatorRequest("status")).status
        return control.handle(OperatorRequest("activate", LEASE, status.epoch, status.serverInstance, status.handoff, status.activeCivilization, UUID.randomUUID().toString()))
    }

    private fun rollbackRequest(status: OperatorStatus, activationId: String) = OperatorRequest("rollback", LEASE, status.epoch, status.serverInstance, status.handoff, status.activeCivilization, activationId)

    private fun command(snapshot: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null, queue: List<String> = emptyList()) = TurnCommandRequest(UUID.randomUUID().toString(), snapshot.epoch, snapshot.revision, kind, target, selection, queue).reviewedFor(snapshot)

    private fun expectOperator(code: String, control: OperatorControl, request: OperatorRequest) {
        val failure = runCatching { control.handle(request) }.exceptionOrNull()
        check(failure is OperatorFailure && failure.code == code) { "Expected $code, got $failure" }
    }

    private fun expectPlayer(reason: PlayerSessionFailure, block: () -> Any) {
        val failure = runCatching(block).exceptionOrNull()
        check(failure is PlayerSessionException && failure.reason == reason) { "Expected $reason, got $failure" }
    }

    private fun expected(root: Path) = PlayerTurnJson.json.decodeFromString<RollbackExpected>(Files.readString(root.resolve("expected.json")))

    private fun readCheckpoint(path: Path) = PlayerTurnJson.json.decodeFromString<GameCheckpoint>(Files.readString(path))
}
