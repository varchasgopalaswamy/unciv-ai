package uncivai

import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import java.nio.file.Path

object CommunicationWaitProbe {
    @JvmStatic
    fun main(args: Array<String>) = runBlocking {
        val root = Path.of(args.single())
        val checkpoint = root.resolve("wait.json")
        HeadlessRuntime.createBilateralExercise(root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            val rome = PlayerHost(runtime).bindPlayer("Rome")
            val greece = PlayerHost(runtime).bindPlayer("Greece")
            val epoch = runtime.executor.epoch
            var sequence = 0
            fun send(text: String) = rome.communication(obj("op" to "send", "epoch" to epoch, "requestId" to "r${++sequence}", "to" to "Greece", "text" to text))
            fun read(cursor: Int, wait: Int = 25) = obj("op" to "read", "epoch" to epoch, "cursor" to cursor, "waitSeconds" to wait)
            var reads = 0
            val observed = object : PlayerSession by greece {
                override fun communication(request: JsonObject): JsonObject {
                    reads++
                    return greece.communication(request)
                }
            }
            val pending = async(start = CoroutineStart.UNDISPATCHED) { observed.awaitCommunication(read(0)) }
            delay(250)
            check(reads == 1) { "The host polled communication while waiting" }
            // Networknt rejects the request before recording an event, receipt or mutation.
            val before = java.nio.file.Files.readAllBytes(checkpoint)
            check(runCatching { send("x".repeat(4001)) }.exceptionOrNull() is PlayerSessionException)
            check(before.contentEquals(java.nio.file.Files.readAllBytes(checkpoint)))
            send("A committed greeting")
            val result = withTimeout(2000) { pending.await() }
            check(result.getValue("events").jsonArray.single().jsonObject.getValue("body").jsonObject.getValue("text").jsonPrimitive.content == "A committed greeting")
            check(reads == 2)

            val cancelled = async(start = CoroutineStart.UNDISPATCHED) { greece.awaitCommunication(read(1)) }
            cancelled.cancelAndJoin()
            send("Retained after cancellation")
            check(greece.awaitCommunication(read(1)).getValue("events").jsonArray.size == 1)

            // Force a commit precisely between the empty read and the first subscription.
            var race = true
            val racing = object : PlayerSession by greece {
                override fun communication(request: JsonObject): JsonObject {
                    val old = greece.communication(request)
                    if (race) {
                        race = false
                        send("Between read and subscribe")
                    }
                    return old
                }
            }
            check(withTimeout(2000) { racing.awaitCommunication(read(2)) }.getValue("events").jsonArray.size == 1)
            reads = 0
            check(observed.awaitCommunication(read(3, 1)).getValue("events").jsonArray.isEmpty())
            check(reads == 2) { "Quiet timeout must make just the initial and final guarded reads" }

            val offer = rome.communication(obj("op" to "trade_offer", "epoch" to epoch, "requestId" to "offer", "partner" to "Greece", "give" to listOf(obj("type" to "Gold", "name" to "Gold", "amount" to 10, "duration" to -1)), "receive" to emptyList<String>())).getValue("trade").jsonObject
            val negotiation = obj("op" to "negotiation", "epoch" to epoch, "waitSeconds" to 25)
            val settled = async(start = CoroutineStart.UNDISPATCHED) { rome.awaitCommunication(negotiation) }
            check(!settled.isCompleted)
            greece.communication(obj("op" to "trade_decide", "epoch" to epoch, "requestId" to "decline", "proposal" to offer["proposal"], "version" to offer["version"], "decision" to "decline"))
            check(!withTimeout(2000) { settled.await() }.getValue("negotiation").jsonObject.getValue("active").jsonPrimitive.boolean)

            runtime.executor.accessGame { runtime.executor.communicationState.trades.timeoutSeconds = 1 }
            rome.communication(obj("op" to "trade_offer", "epoch" to epoch, "requestId" to "timeout-offer", "partner" to "Greece", "give" to listOf(obj("type" to "Gold", "name" to "Gold", "amount" to 10, "duration" to -1)), "receive" to emptyList<String>()))
            check(!withTimeout(3000) { rome.awaitCommunication(negotiation) }.getValue("negotiation").jsonObject.getValue("active").jsonPrimitive.boolean)

            val control = OperatorControl(runtime.executor)
            val guarded = control.manualSession(greece, "revoked-player-token")
            val currentCursor = greece.communication(read(0, 0)).getValue("nextCursor")
            val revocation = async(start = CoroutineStart.UNDISPATCHED) {
                runCatching { guarded.awaitCommunication(obj("op" to "read", "epoch" to epoch, "cursor" to currentCursor, "waitSeconds" to 25)) }.exceptionOrNull()
            }
            control.handle(OperatorRequest("acquire", leaseId = "lease"))
            control.handle(OperatorRequest("barrier", leaseId = "lease"))
            control.handle(OperatorRequest("rotate_players", leaseId = "lease", epoch = epoch))
            check((withTimeout(2000) { revocation.await() } as PlayerSessionException).reason == PlayerSessionFailure.COMMAND_UNAVAILABLE)

            val cursor = greece.communication(read(0, 0)).getValue("nextCursor")
            val closing = async(start = CoroutineStart.UNDISPATCHED) {
                runCatching { greece.awaitCommunication(obj("op" to "read", "epoch" to epoch, "cursor" to cursor, "waitSeconds" to 25)) }.exceptionOrNull()
            }
            runtime.close()
            check((withTimeout(2000) { closing.await() } as PlayerSessionException).reason == PlayerSessionFailure.SESSION_CLOSED)
        }
        // A failed checkpoint must wake a halted error, never publish the uncommitted message.
        HeadlessRuntime.createBilateralExercise(root.resolve("failure")).use { runtime ->
            var fail = false
            runtime.executor.enableCheckpoints(CheckpointStore(root.resolve("failure.json"), beforeReplace = { check(!fail) { "Injected commit failure" } }))
            val sessions = listOf("Rome", "Greece").associateWith { PlayerHost(runtime).bindPlayer(it) }
            val epoch = runtime.executor.epoch
            val waiting = async(start = CoroutineStart.UNDISPATCHED) { runCatching { sessions.getValue("Greece").awaitCommunication(obj("op" to "read", "epoch" to epoch, "waitSeconds" to 25)) }.exceptionOrNull() }
            fail = true
            check(runCatching { sessions.getValue("Rome").communication(obj("op" to "send", "epoch" to epoch, "requestId" to "r1", "to" to "Greece", "text" to "Uncommitted")) }.isFailure)
            check((withTimeout(2000) { waiting.await() } as PlayerSessionException).reason == PlayerSessionFailure.GAME_HALTED)
        }
    }
}
