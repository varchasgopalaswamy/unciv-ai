package uncivai

import com.unciv.json.json
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path

/** Abrupt process termination on either side of the atomic settlement checkpoint replacement. */
object SettlementCrashProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val mode = args[0]
        val root = Path.of(args[1])
        val checkpoint = root.resolve("game.json")
        val requestFile = root.resolve("request.json")
        when (mode) {
            "initialize" -> {
                HeadlessRuntime.createDuel(root.resolve("data"), seed = 10001).use { runtime ->
                    runtime.executor.enableCheckpoints(checkpoint)
                    val turn = PlayerHost(runtime).bindPlayer("Rome").turn()
                    val request = TurnCommandRequest("settle-once", turn.epoch, turn.revision, TurnCommandKind.FOUND_CITY, turn.map.units.single { it.canFoundCity }.handle)
                    Files.writeString(requestFile, PlayerTurnJson.encode(request))
                }
            }

            "crash-before", "crash-after" -> {
                var armed = false
                val store = CheckpointStore(
                    checkpoint,
                    beforeReplace = { if (armed && mode == "crash-before") Runtime.getRuntime().halt(73) },
                    afterReplace = { if (armed && mode == "crash-after") Runtime.getRuntime().halt(73) },
                )
                HeadlessRuntime.loadCheckpoint(store, root.resolve("crashing")).use { runtime ->
                    val player = PlayerHost(runtime).bindPlayer("Rome")
                    val request = PlayerTurnJson.json.decodeFromString<TurnCommandRequest>(Files.readString(requestFile))
                        .copy(revision = player.turn().revision)
                    Files.writeString(requestFile, PlayerTurnJson.encode(request))
                    armed = true
                    player.execute(request)
                    error("Crash hook was not reached")
                }
            }

            "verify-before", "verify-after" -> {
                HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("recovered")).use { runtime ->
                    val civilization = runtime.game.getCivilization("Rome")
                    val committed = mode == "verify-after"
                    check(civilization.cities.size == if (committed) 1 else 0)
                    check(civilization.units.getCivUnits().count { it.name == "Settler" } == if (committed) 0 else 1)
                    val player = PlayerHost(runtime).bindPlayer("Rome")
                    val original = PlayerTurnJson.json.decodeFromString<TurnCommandRequest>(Files.readString(requestFile))
                    val request = if (committed) original else original.copy(revision = player.turn().revision)
                    val receipt = player.execute(request)
                    check(civilization.cities.size == 1 && civilization.units.getCivUnits().none { it.name == "Settler" })
                    val raw = json().toJson(runtime.game)
                    check(player.execute(request) == receipt)
                    check(json().toJson(runtime.game) == raw)
                }
            }

            else -> error("Unknown probe mode")
        }
    }
}
