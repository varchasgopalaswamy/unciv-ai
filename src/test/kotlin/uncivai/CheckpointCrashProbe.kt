package uncivai

import com.unciv.json.json
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path

/** Each mode runs in a separate JVM. Crash modes deliberately skip all shutdown hooks. */
object CheckpointCrashProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val mode = args[0]
        val save = Path.of(args[1])
        val root = Path.of(args[2])
        val checkpoint = root.resolve("match.json")
        val requestFile = root.resolve("request.json")
        if (mode == "initialize") {
            HeadlessRuntime.load(save, root.resolve("initial")).use { runtime ->
                runtime.executor.enableCheckpoints(checkpoint)
                val snapshot = PlayerHost(runtime).bindPlayer("Rome").turn()
                val request = TurnCommandRequest(
                    "crash-free-tech",
                    snapshot.epoch,
                    snapshot.revision,
                    TurnCommandKind.CHOOSE_FREE_TECHNOLOGY,
                    selection = snapshot.researchOptions.first(),
                )
                Files.writeString(requestFile, PlayerTurnJson.encode(request))
            }
            return
        }
        val request = PlayerTurnJson.json.decodeFromString<TurnCommandRequest>(Files.readString(requestFile))
        if (mode.startsWith("crash-")) {
            val crash = { Runtime.getRuntime().halt(73) }
            val store = if (mode == "crash-before") {
                CheckpointStore(checkpoint, beforeReplace = crash)
            } else {
                CheckpointStore(checkpoint, afterReplace = crash)
            }
            HeadlessRuntime.loadCheckpoint(store, root.resolve(mode)).use { runtime ->
                PlayerHost(runtime).bindPlayer("Rome").execute(request)
                error("Crash hook did not run")
            }
        } else {
            HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve(mode)).use { runtime ->
                val player = runtime.game.getCivilization("Rome")
                val session = PlayerHost(runtime).bindPlayer("Rome")
                if (mode == "verify-before") {
                    check(player.tech.freeTechs == 1)
                } else {
                    check(player.tech.freeTechs == 0)
                }
                val receipt = session.execute(request)
                check(player.tech.freeTechs == 0)
                check(player.tech.isResearched(request.selection!!))
                val after = json().toJson(runtime.game)
                check(session.execute(request) == receipt)
                check(json().toJson(runtime.game) == after)
            }
        }
    }
}
