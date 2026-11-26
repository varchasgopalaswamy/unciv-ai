package uncivai

import com.unciv.json.json
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.PrintStream
import java.nio.file.Files
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
        val savedPlayers = Json.parseToJsonElement(Files.readString(checkpoint)).jsonObject.getValue("players").jsonObject
        check(savedPlayers.isNotEmpty())
        check(savedPlayers.values.all { "fingerprint" !in it.jsonObject && "revision" !in it.jsonObject })
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(session.turn().cities.single().handle == request.target)
            check(session.turn().revision != receipt.revision) { "Restored observations need a fresh revision" }
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
            val diagnostics = ByteArrayOutputStream()
            val previousError = System.err
            val failure = try {
                System.setErr(PrintStream(diagnostics))
                val result = runCatching { session.execute(request) }.exceptionOrNull()
                // A secondary failure must preserve and report only the original cause.
                runCatching { runtime.haltAfterMutation(IllegalStateException("secondary failure")) }
                result
            } finally {
                System.setErr(previousError)
            }
            check(failure is PlayerSessionException && failure.reason == PlayerSessionFailure.GAME_HALTED)
            check(runtime.mutationFailure === injected)
            check(failure.cause == null)
            check("injected persistence failure" !in failure.message.orEmpty())
            val logged = diagnostics.toString(Charsets.UTF_8)
            check(logged.split("java.io.IOException: injected persistence failure").size == 2)
            check("CheckpointProbe" in logged && "secondary failure" !in logged)
            check(runCatching { runtime.executor.resume() }.isFailure) { "Resume cleared an uncertain mutation" }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val name = session.turn().cities.single().name
            check(name == if (afterReplace) request.selection else originalName)
            val retried = if (afterReplace) request else request.copy(revision = session.turn().revision)
            val receipt = session.execute(retried)
            check(session.turn().cities.single().name == request.selection)
            check(session.execute(retried) == receipt)
        }
    }
}
