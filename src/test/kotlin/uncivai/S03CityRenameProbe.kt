package uncivai

import com.unciv.json.json
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path
import java.util.UUID
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** Rename authority and replay go through the same command path as other actions. */
object S03CityRenameProbe {
    @JvmStatic fun main(args: Array<String>) {
        val save = Path.of(args[0])
        HeadlessRuntime.load(save, Path.of(args[1])).use { runtime ->
            val game = runtime.game
            val rome = game.getCivilization("Rome")
            val greece = game.getCivilization("Greece")
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val initial = session.turn()
            val city = rome.cities.single()
            val handle = initial.cities.single().handle
            fun request(name: String, target: String = handle): TurnCommandRequest {
                val turn = session.turn()
                return TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, TurnCommandKind.RENAME_CITY, target, name)
            }
            fun reject(reason: PlayerSessionFailure, request: TurnCommandRequest) {
                val before = json().toJson(game)
                val failure = runCatching { session.execute(request) }.exceptionOrNull()
                check(failure is PlayerSessionException && failure.reason == reason) { "Expected $reason, got $failure" }
                check(failure.cause == null && json().toJson(game) == before)
            }
            reject(PlayerSessionFailure.STALE_EPOCH, request("Old").copy(epoch = "expired"))
            reject(PlayerSessionFailure.CITY_UNAVAILABLE, request("Foreign", greece.cities.single().id))
            reject(PlayerSessionFailure.INVALID_ARGUMENT, request(""))
            for (name in listOf("[invalid]", "x".repeat(51), "a\nb")) reject(PlayerSessionFailure.OPTION_UNAVAILABLE, request(name))
            val accepted = request("Nova Roma")
            val receipt = session.execute(accepted)
            check(city.name == "Nova Roma" && initial.cities.single().name == "Rome")
            check(PlayerHost(runtime).bindPlayer("Rome").execute(accepted) == receipt)
            reject(PlayerSessionFailure.REQUEST_ID_CONFLICT, accepted.copy(selection = "Conflict"))
            reject(PlayerSessionFailure.STALE_REVISION, accepted.copy(requestId = "stale"))
            val second = request("Róma 東京")
            val pool = Executors.newFixedThreadPool(2)
            try {
                val receipts = pool.invokeAll(List(2) { Callable { session.execute(second) } }).map { it.get(10, TimeUnit.SECONDS) }
                check(receipts[0] == receipts[1] && city.name == "Róma 東京")
            } finally {
                pool.shutdownNow()
                check(pool.awaitTermination(5, TimeUnit.SECONDS))
            }
            check(session.execute(accepted) == receipt && city.name == "Róma 東京")
            game.currentPlayer = greece.civID
            game.currentPlayerCiv = greece
            reject(PlayerSessionFailure.NOT_YOUR_TURN, request("Out of turn"))
            check(session.execute(accepted) == receipt)
            game.currentPlayer = rome.civID
            game.currentPlayerCiv = rome
            city.moveToCiv(greece)
            check(session.execute(accepted) == receipt)
            reject(PlayerSessionFailure.COMMAND_UNAVAILABLE, request("Lost city"))
        }
        println("S03-VERIFIED:${save.fileName}")
    }
}
