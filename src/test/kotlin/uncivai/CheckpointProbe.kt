package uncivai

import com.unciv.json.json
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import java.io.IOException
import java.nio.file.Path

object CheckpointProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val save = Path.of(args[0])
        val root = Path.of(args[1])
        val checkpoint = root.resolve("game.checkpoint.json")
        lateinit var request: TurnCommandRequest
        lateinit var receipt: TurnCommandReceipt
        HeadlessRuntime.load(save, root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            CheckpointStore(checkpoint).use { competing ->
                check(runCatching { competing.read() }.isFailure) { "A second writer opened an active checkpoint" }
            }
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(session.capabilities().durableRecovery)
            val snapshot = session.turn()
            request = TurnCommandRequest(
                "durable-rename",
                snapshot.epoch,
                snapshot.revision,
                TurnCommandKind.RENAME_CITY,
                snapshot.cities.single().handle,
                "Persistent Roma",
            )
            receipt = session.execute(request)
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(session.turn().cities.single().handle == request.target)
            check(session.turn().revision == receipt.revision)
            val before = json().toJson(runtime.game)
            check(session.execute(request) == receipt)
            check(json().toJson(runtime.game) == before)
            runtime.executor.pause()
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored-paused")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(session.turn().phase == "PAUSED")
            check(session.execute(request) == receipt) { "Successful replay should work while paused" }
            runtime.executor.resume()
        }
        for (afterReplace in listOf(false, true)) {
            verifyFailedAcknowledgement(save, root.resolve("failure-$afterReplace"), afterReplace)
        }
    }

    private fun verifyFailedAcknowledgement(save: Path, root: Path, afterReplace: Boolean) {
        val checkpoint = root.resolve("game.checkpoint.json")
        var armed = false
        val injected = IOException("injected persistence failure")
        val fail = { if (armed) throw injected }
        val store = if (afterReplace) {
            CheckpointStore(checkpoint, afterReplace = fail)
        } else {
            CheckpointStore(checkpoint, beforeReplace = fail)
        }
        lateinit var request: TurnCommandRequest
        lateinit var originalName: String
        HeadlessRuntime.load(save, root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(store)
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val snapshot = session.turn()
            originalName = snapshot.cities.single().name
            request = TurnCommandRequest(
                "uncertain-rename",
                snapshot.epoch,
                snapshot.revision,
                TurnCommandKind.RENAME_CITY,
                snapshot.cities.single().handle,
                "Committed or pending",
            )
            armed = true
            val failure = runCatching { session.execute(request) }.exceptionOrNull()
            check(failure is PlayerSessionException && failure.reason == PlayerSessionFailure.GAME_HALTED)
            check(runtime.mutationFailure === injected)
            check(failure.cause == null)
            check(runCatching { runtime.executor.resume() }.isFailure) { "Resume cleared an uncertain mutation" }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val name = session.turn().cities.single().name
            check(name == if (afterReplace) request.selection else originalName)
            val receipt = session.execute(request)
            check(session.turn().cities.single().name == request.selection)
            check(session.execute(request) == receipt)
        }
    }
}
