package uncivai

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.RenameCityRequest
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path

/** Real engine and HTTPS: immutable world, bounded counters, off-turn decisions and durable deadlines. */
object NegotiationProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        val checkpoint = root.resolve("negotiation.json")
        lateinit var pending: JsonObject
        var sequence = 0
        HeadlessRuntime.createBilateralExercise(root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                val token = checkNotNull(api.activate().playerToken)
                val before = api.turn(token)
                val epoch = runtime.executor.epoch
                fun command(civ: String, op: String, vararg fields: Pair<String, Any?>) = api.communicate(api.playerToken(civ), obj("op" to op, "epoch" to epoch, "requestId" to "n${++sequence}", *fields))
                val gold = obj("type" to "Gold", "name" to "Gold", "amount" to 1, "duration" to -1)
                fun offer(civ: String, previous: JsonObject? = null): JsonObject {
                    val fields = if (previous == null) emptyArray() else arrayOf("proposal" to previous["proposal"], "version" to previous["version"])
                    return command(civ, "trade_offer", "partner" to if (civ == "Rome") "Greece" else "Rome", "give" to listOf(gold), "receive" to emptyList<String>(), *fields)
                }
                check(offer("Greece")["error"] != null) // Off-turn invitations use chat.
                val initial = offer("Rome")["trade"]!!.jsonObject
                val deadline = initial["deadlineMillis"]!!.jsonPrimitive.long
                check(initial["counteroffersRemaining"]!!.jsonPrimitive.int == 3)
                val frozen = api.turn(token)
                check(!frozen.canEndTurn && frozen.negotiation["active"]!!.jsonPrimitive.boolean)
                val city = before.cities.single { it.name == "Antium" }
                for (request in listOf(
                    TurnCommandRequest("end", epoch, before.revision, TurnCommandKind.END_TURN).reviewedFor(before),
                    TurnCommandRequest("rename", epoch, before.revision, TurnCommandKind.RENAME_CITY, city.handle, "Changed"),
                    TurnCommandRequest("war", epoch, before.revision, TurnCommandKind.DECLARE_WAR, "Greece", confirmWar = true),
                )) {
                    api.reject(token, request, "NEGOTIATION_ACTIVE")
                }
                val legacy = PlayerHost(runtime).bindPlayer("Rome")
                val failed = runCatching { legacy.renameCity(RenameCityRequest("legacy", epoch, city.handle, "Changed")) }.exceptionOrNull()
                check((failed as PlayerSessionException).reason == PlayerSessionFailure.NEGOTIATION_ACTIVE)
                check(runtime.game.getCivilization("Rome").cities.single { it.name == "Antium" }.population.population == 3)
                check(runtime.executor.handoff == 0L && runtime.game.currentPlayer == "Rome")
                check(offer("Rome")["error"] != null) // New IDs cannot bypass the counter limit.
                check(runtime.executor.communication.trades.negotiation("Egypt") == obj("active" to true))
                command("Greece", "send", "to" to "Rome", "text" to "Considering these terms.")
                check(api.turn(token).revision == frozen.revision)
                var latest = initial
                for (civ in listOf("Greece", "Rome", "Greece")) {
                    latest = offer(civ, latest)["trade"]!!.jsonObject
                    check(latest["deadlineMillis"]!!.jsonPrimitive.long == deadline)
                }
                check(latest["counteroffersRemaining"]!!.jsonPrimitive.int == 0)
                check(!latest["counterofferAvailable"]!!.jsonPrimitive.boolean)
                check(offer("Rome", latest)["error"] != null)
                val accepted = command("Rome", "trade_decide", "proposal" to latest["proposal"], "version" to latest["version"], "decision" to "accept")
                check(accepted["outcome"] != null && runtime.executor.communicationState.trades.activeProposal == null)
                check(runtime.game.getCivilization("Rome").gold == 1001 && runtime.game.getCivilization("Greece").gold == 999)
                api.reject(token, TurnCommandRequest("old-city", epoch, before.revision, TurnCommandKind.RENAME_CITY, city.handle, "Changed"), "STALE_REVISION")
                pending = offer("Rome")["trade"]!!.jsonObject
                runtime.executor.commitCheckpoint()
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            val deadline = pending["deadlineMillis"]!!.jsonPrimitive.long
            val sessions = listOf("Rome", "Greece").associateWith { PlayerHost(runtime).bindPlayer(it) }
            fun read() = sessions.getValue("Greece").communication(obj("op" to "trade_read", "epoch" to runtime.executor.epoch, "proposal" to pending["proposal"]))["trade"]!!.jsonObject
            check(read()["deadlineMillis"]!!.jsonPrimitive.long == deadline)
            check(read()["status"]!!.jsonPrimitive.content == "PENDING")
            runtime.executor.clockMillis = { deadline }
            check(read()["status"]!!.jsonPrimitive.content == "EXPIRED")
            check(runtime.executor.communicationState.trades.activeProposal == null)
            val late = sessions.getValue("Greece").communication(obj("op" to "trade_decide", "epoch" to runtime.executor.epoch, "requestId" to "late", "proposal" to pending["proposal"], "version" to pending["version"], "decision" to "accept"))
            check(late["error"] != null && runtime.game.getCivilization("Rome").gold == 1001)
            runtime.executor.clockMillis = System::currentTimeMillis
            runtime.executor.resume()
            val rome = sessions.getValue("Rome")
            val view = rome.turn()
            rome.execute(TurnCommandRequest("resumed", view.epoch, view.revision, TurnCommandKind.RENAME_CITY, view.cities.first().handle, "Resumed"))
            check(runtime.game.getCivilization("Rome").cities.any { it.name == "Resumed" })
            // A real deadline task closes an unattended negotiation without any player polling.
            runtime.executor.communicationState.trades.timeoutSeconds = 1
            runtime.executor.communicationState.trades.counterofferLimit = 0
            val gold = obj("type" to "Gold", "name" to "Gold", "amount" to 1, "duration" to -1)
            val opened = rome.communication(obj("op" to "trade_offer", "epoch" to runtime.executor.epoch, "requestId" to "timer", "partner" to "Greece", "give" to listOf(gold), "receive" to emptyList<String>()))["trade"]!!.jsonObject
            check(!opened["counterofferAvailable"]!!.jsonPrimitive.boolean)
            Thread.sleep(1500)
            val saved = kotlinx.serialization.json.Json.decodeFromString<GameCheckpoint>(java.nio.file.Files.readString(checkpoint))
            check(saved.communication.trades.proposals.getValue(opened["proposal"]!!.jsonPrimitive.content).last().status == "EXPIRED")
        }
    }
}
