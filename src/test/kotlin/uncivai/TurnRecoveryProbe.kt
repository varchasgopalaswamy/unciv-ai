package uncivai

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
private data class TurnRecoveryExpected(
    val boundary: GameCheckpoint,
    val before: PlayerTurnSnapshot,
    val abandoned: List<TurnCommandRequest>,
    val endRequest: TurnCommandRequest? = null,
    val endReceipt: TurnCommandReceipt? = null,
)

/** Crash recovery uses the same coordinated snapshot and restore operations as the scheduler. */
object TurnRecoveryProbe {
    private const val LEASE = "turn-recovery-test"
    private const val RESTORED = "restored-turn-epoch"

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        when (val stage = args[1]) {
            "mutate" -> mutate(root)
            "restore" -> restore(root)
            "verify" -> verify(root)
            "failure-before", "failure-after" -> persistenceFailure(root.resolve(stage), stage == "failure-after")
            else -> error(stage)
        }
    }

    private fun boundary(control: OperatorControl): GameCheckpoint {
        control.handle(OperatorRequest("barrier", LEASE))
        val saved = checkNotNull(control.handle(OperatorRequest("snapshot", LEASE)).checkpoint)
        control.handle(OperatorRequest("unbarrier", LEASE))
        return saved
    }

    private fun rewind(control: OperatorControl, saved: GameCheckpoint) {
        control.handle(OperatorRequest("barrier", LEASE))
        control.handle(OperatorRequest("restore", leaseId = LEASE, epoch = RESTORED, checkpoint = saved))
    }

    private fun release(control: OperatorControl) {
        val rotated = control.handle(OperatorRequest("rotate_players", leaseId = LEASE, epoch = RESTORED))
        check(rotated.players!!.size == 2)
        control.handle(OperatorRequest("restore_complete", leaseId = LEASE, epoch = RESTORED))
        control.handle(OperatorRequest("unbarrier", LEASE))
    }

    private fun mutate(root: Path) {
        HeadlessRuntime.createDuel(root.resolve("initial"), 412L).use { runtime ->
            runtime.executor.enableCheckpoints(root.resolve("checkpoint.json"))
            val control = OperatorControl(runtime.executor)
            control.handle(OperatorRequest("acquire", LEASE))
            val saved = boundary(control)
            val activation = activate(control)
            val player = checkNotNull(control.activationSession(checkNotNull(activation.playerToken)))
            val before = player.turn()
            val commands = alterTurn(player)
            check(player.turn().cities.size == 1)
            Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(TurnRecoveryExpected(saved, before, commands)))
        }
    }

    private fun restore(root: Path) {
        val saved = expected(root)
        HeadlessRuntime.loadCheckpoint(root.resolve("checkpoint.json"), root.resolve("restart")).use { runtime ->
            check(runtime.game.getCivilization("Rome").cities.size == 1)
            val control = OperatorControl(runtime.executor)
            control.handle(OperatorRequest("acquire", LEASE))
            val interrupted = activate(control)
            val oldToken = checkNotNull(interrupted.playerToken)
            val oldPlayer = checkNotNull(control.activationSession(oldToken))
            rewind(control, saved.boundary)
            check(control.activationSession(oldToken) == null)
            expectPlayer(PlayerSessionFailure.COMMAND_UNAVAILABLE) { oldPlayer.turn() }
            val reset = runtime.executor.turnSnapshot()
            rewind(control, saved.boundary) // Lost acknowledgement: restoration is repeatable.
            check(runtime.executor.turnSnapshot().gameJson == reset.gameJson)
            check(runtime.game.getCivilization("Rome").cities.isEmpty())
            check(saved.abandoned.none { it.requestId in runtime.executor.turnSessions["Rome"]?.receipts.orEmpty() })
            expectOperator("RESTORE_INCOMPLETE", control, OperatorRequest("activate", LEASE, RESTORED, interrupted.status.serverInstance, 0, "Rome", "too-early"))
            release(control)
            val fresh = activate(control)
            val player = checkNotNull(control.activationSession(checkNotNull(fresh.playerToken)))
            val before = player.turn()
            check(before.epoch == RESTORED && before.turn == saved.before.turn)
            check(before.map.units.map { it.name to it.position } == saved.before.map.units.map { it.name to it.position })
            check(before.researchQueue == saved.before.researchQueue)
            val end = finishTurn(player)
            val receipt = player.execute(end)
            check(control.handle(OperatorRequest("status")).status.handoff == 1L)
            control.handle(OperatorRequest("barrier", LEASE))
            expectOperator("TURN_ALREADY_COMMITTED", control, OperatorRequest("restore", leaseId = LEASE, epoch = "wrong-rewind", checkpoint = saved.boundary))
            Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(saved.copy(endRequest = end, endReceipt = receipt)))
        }
    }

    private fun verify(root: Path) {
        val saved = expected(root)
        HeadlessRuntime.loadCheckpoint(root.resolve("checkpoint.json"), root.resolve("committed")).use { runtime ->
            check(runtime.executor.epoch == RESTORED && runtime.executor.handoff == 1L)
            check(runtime.game.getCivilization("Rome").cities.size == 1)
            check(PlayerHost(runtime).bindPlayer("Rome").execute(checkNotNull(saved.endRequest)) == saved.endReceipt)
            check(saved.abandoned.none { it.requestId in runtime.executor.turnSessions["Rome"]?.receipts.orEmpty() })
        }
    }

    private fun persistenceFailure(root: Path, afterReplace: Boolean) {
        val path = root.resolve("checkpoint.json")
        val injected = IOException("Injected turn restore persistence failure")
        var armed = false
        val fail = { if (armed) throw injected }
        val store = if (afterReplace) CheckpointStore(path, afterReplace = fail) else CheckpointStore(path, beforeReplace = fail)
        lateinit var saved: GameCheckpoint
        HeadlessRuntime.createDuel(root.resolve("initial"), 412L).use { runtime ->
            runtime.executor.enableCheckpoints(store)
            val control = OperatorControl(runtime.executor)
            control.handle(OperatorRequest("acquire", LEASE))
            saved = boundary(control)
            val active = activate(control)
            alterTurn(checkNotNull(control.activationSession(checkNotNull(active.playerToken))))
            control.handle(OperatorRequest("barrier", LEASE))
            armed = true
            val failure = runCatching { control.handle(OperatorRequest("restore", leaseId = LEASE, epoch = RESTORED, checkpoint = saved)) }.exceptionOrNull()
            check(failure is RuntimeHaltedException)
            check(runtime.mutationFailure === injected)
        }
        HeadlessRuntime.loadCheckpoint(path, root.resolve("recovered")).use { runtime ->
            check(runtime.game.getCivilization("Rome").cities.size == if (afterReplace) 0 else 1)
            val control = OperatorControl(runtime.executor)
            control.handle(OperatorRequest("acquire", LEASE))
            rewind(control, saved)
            release(control)
            check(runtime.executor.epoch == RESTORED && runtime.executor.handoff == 0L)
            check(runtime.game.getCivilization("Rome").cities.isEmpty())
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

    private fun command(snapshot: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null, queue: List<String> = emptyList()) = TurnCommandRequest(UUID.randomUUID().toString(), snapshot.epoch, snapshot.revision, kind, target, selection, queue).reviewedFor(snapshot)

    private fun expectOperator(code: String, control: OperatorControl, request: OperatorRequest) {
        val failure = runCatching { control.handle(request) }.exceptionOrNull()
        check(failure is OperatorFailure && failure.code == code) { "Expected $code, got $failure" }
    }

    private fun expectPlayer(reason: PlayerSessionFailure, block: () -> Any) {
        val failure = runCatching(block).exceptionOrNull()
        check(failure is PlayerSessionException && failure.reason == reason) { "Expected $reason, got $failure" }
    }

    private fun expected(root: Path) = PlayerTurnJson.json.decodeFromString<TurnRecoveryExpected>(Files.readString(root.resolve("expected.json")))
}
