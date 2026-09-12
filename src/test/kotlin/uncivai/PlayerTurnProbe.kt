package uncivai

import com.unciv.json.json
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path
import java.util.UUID
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

object PlayerTurnProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        HeadlessRuntime.load(Path.of(args[0]), Path.of(args[1])).use { runtime ->
            val host = PlayerHost(runtime)
            val rome = host.bindPlayer("Rome")
            val greece = host.bindPlayer("Greece")
            val game = runtime.game
            val initialRaw = json().toJson(game)
            val initial = rome.turn()
            check(json().toJson(game) == initialRaw) { "Turn query mutated saved game state" }
            check(initial.phase == "ACTIVE" && greece.turn().phase == "WAITING")
            check(!initial.canEndTurn && !rome.capabilities().completeGameplay)
            check(initial.pending.take(4).map { it.kind } == listOf("FreeGreatPerson", "Construction", "Research", "Policy"))
            check(initial == rome.turn())
            check(initial == PlayerHost(runtime).bindPlayer("Rome").turn()) { "Reconnect changed player references" }

            val foreign = game.getCivilization("Greece")
            foreign.addGold(314)
            foreign.cities.single().name = "SECRET-FOREIGN-CITY"
            check(rome.turn() == initial) { "Hidden state changed a player revision or observation" }
            check("SECRET-FOREIGN-CITY" !in PlayerTurnJson.encode(rome.turn()))
            val greekHandle = greece.turn().cities.single().handle
            check(greekHandle == initial.cities.single().handle) { "City references must be player-local" }
            expect(PlayerSessionFailure.CITY_UNAVAILABLE) {
                rome.execute(command(rome, TurnCommandKind.RENAME_CITY, target = foreign.cities.single().id, selection = "Stolen"))
            }
            expect(PlayerSessionFailure.NOT_YOUR_TURN) {
                greece.execute(command(greece, TurnCommandKind.CHOOSE_FREE_TECHNOLOGY, selection = greece.turn().researchOptions.first()))
            }
            expect(PlayerSessionFailure.TURN_BLOCKED) { rome.execute(command(rome, TurnCommandKind.END_TURN)) }
            val beforeInvalid = json().toJson(game)
            expect(PlayerSessionFailure.OPTION_UNAVAILABLE) {
                rome.execute(command(rome, TurnCommandKind.CHOOSE_FREE_TECHNOLOGY, selection = "Unknown technology"))
            }
            check(json().toJson(game) == beforeInvalid)

            val choose = command(rome, TurnCommandKind.CHOOSE_FREE_TECHNOLOGY, selection = initial.researchOptions.first())
            val receipt = rome.execute(choose)
            val afterChoice = json().toJson(game)
            check(PlayerHost(runtime).bindPlayer("Rome").execute(choose) == receipt)
            check(json().toJson(game) == afterChoice)
            expect(PlayerSessionFailure.REQUEST_ID_CONFLICT) { rome.execute(choose.copy(selection = "Another")) }
            expect(PlayerSessionFailure.STALE_REVISION) { rome.execute(choose.copy(requestId = "stale")) }
            expect(PlayerSessionFailure.STALE_EPOCH) { rome.execute(choose.copy(epoch = "old")) }

            val rename = command(rome, TurnCommandKind.RENAME_CITY, target = initial.cities.single().handle, selection = "Roma")
            val executor = Executors.newFixedThreadPool(2)
            try {
                val results = executor.invokeAll(List(2) { Callable { rome.execute(rename) } })
                    .map { it.get(10, TimeUnit.SECONDS) }
                check(results[0] == results[1])
            } finally {
                executor.shutdownNow()
                check(executor.awaitTermination(5, TimeUnit.SECONDS))
            }
            val pauseCommand = command(rome, TurnCommandKind.RENAME_CITY, target = initial.cities.single().handle, selection = "Paused")
            runtime.executor.pause()
            check(rome.turn().phase == "PAUSED")
            expect(PlayerSessionFailure.GAME_PAUSED) { rome.execute(pauseCommand) }
            runtime.executor.resume()
            expect(PlayerSessionFailure.STALE_REVISION) { rome.execute(pauseCommand) }

            resolveTurn(rome)
            val beforeEnd = rome.turn()
            check(beforeEnd.canEndTurn) { "Unresolved: ${beforeEnd.pending}" }
            val end = command(rome, TurnCommandKind.END_TURN)
            val ended = rome.execute(end)
            check(!ended.isYourTurn)
            check(game.currentPlayer == "Greece")
            check(rome.turn().phase == "WAITING")
            check(greece.turn().phase == "ACTIVE")
            val afterEnd = json().toJson(game)
            check(PlayerHost(runtime).bindPlayer("Rome").execute(end) == ended)
            check(json().toJson(game) == afterEnd) { "Retry advanced another player's turn" }
            check(greece.turn().freeTechnologies == 2) { "Built-in AI consumed the next human's decisions" }
            check(greece.turn().freeGreatPeople == 2)
            resolveTurn(greece)
            greece.execute(command(greece, TurnCommandKind.END_TURN))
            check(game.currentPlayer == "Rome" && game.turns == beforeEnd.turn + 1)
            runtime.close()
            expect(PlayerSessionFailure.SESSION_CLOSED) { rome.turn() }
        }
    }

    private fun resolveTurn(session: PlayerSession) {
        repeat(30) {
            val snapshot = session.turn()
            if (snapshot.canEndTurn) return
            val pending = snapshot.pending.firstOrNull() ?: error("No pending decision but turn is blocked")
            check(pending.supported) { "Unsupported decision: $pending" }
            val request = when (pending.kind) {
                "FreeGreatPerson" -> command(session, TurnCommandKind.CHOOSE_FREE_GREAT_PERSON, selection = snapshot.greatPersonOptions.first())

                "Construction" -> {
                    val city = snapshot.cities.first { !it.puppet && it.constructionQueue.isEmpty() }
                    command(session, TurnCommandKind.QUEUE_CONSTRUCTION, target = city.handle, selection = "Worker")
                }

                "Research" -> if (snapshot.freeTechnologies > 0) {
                    command(session, TurnCommandKind.CHOOSE_FREE_TECHNOLOGY, selection = snapshot.researchOptions.first())
                } else {
                    command(session, TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf(snapshot.researchOptions.first()))
                }

                "Policy" -> command(session, TurnCommandKind.ADOPT_POLICY, selection = snapshot.policyOptions.first())

                "PopupAlerts" -> command(session, TurnCommandKind.ACKNOWLEDGE_ALERT, target = snapshot.alerts.first().handle)

                else -> error("Unhandled test decision $pending")
            }
            session.execute(request)
        }
        error("Turn choices did not converge")
    }

    private fun command(
        session: PlayerSession,
        kind: TurnCommandKind,
        target: String? = null,
        selection: String? = null,
        queue: List<String> = emptyList(),
    ): TurnCommandRequest {
        val snapshot = session.turn()
        return TurnCommandRequest(UUID.randomUUID().toString(), snapshot.epoch, snapshot.revision, kind, target, selection, queue).reviewedFor(snapshot)
    }

    private fun expect(reason: PlayerSessionFailure, operation: () -> Any?) {
        val failure = runCatching(operation).exceptionOrNull()
        check(failure is PlayerSessionException && failure.reason == reason) { "Expected $reason, got $failure" }
        check(failure.cause == null)
    }
}
