package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors

/** Exercises host-authoritative cancellation, including its persisted execution fence. */
object RequestResolutionProbe {
    private const val LEASE = "request-resolution"

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        if (args.getOrNull(1) == "saved") {
            savedGame(root)
            return
        }
        val checkpoint = root.resolve("checkpoint.json")
        lateinit var cancelledCommand: TurnCommandRequest
        lateinit var cancelledOffer: JsonObject
        HeadlessRuntime.createDuel(root.resolve("initial"), 412L).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            val control = OperatorControl(runtime.executor)
            control.handle(OperatorRequest("acquire", LEASE))
            val status = control.handle(OperatorRequest("status")).status
            val activation = control.handle(OperatorRequest("activate", LEASE, status.epoch, status.serverInstance, status.handoff, "Rome", "initial"))
            val player = checkNotNull(control.activationSession(checkNotNull(activation.playerToken)))
            val before = player.turn()
            val completedCommand = TurnCommandRequest("completed-command", before.epoch, before.revision, TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf(before.researchOptions.first()))
            val receipt = player.execute(completedCommand)
            check("error" !in Json.parseToJsonElement(PlayerTurnJson.encode(receipt)).jsonObject)
            cancelledCommand = completedCommand.copy(requestId = "cancelled-command")
            val commandPayload = Json.parseToJsonElement(PlayerTurnJson.encode(cancelledCommand)).jsonObject
            val send = obj("op" to "send", "epoch" to before.epoch, "requestId" to "completed-send", "to" to "Greece", "text" to "Already sent")
            val sent = runtime.executor.communication.handle("Rome", send)
            cancelledOffer = obj(
                "op" to "trade_offer",
                "epoch" to before.epoch,
                "requestId" to "cancelled-offer",
                "partner" to "Greece",
                "give" to listOf(obj("type" to "Gold", "name" to "Gold", "amount" to 100, "duration" to 0)),
                "receive" to listOf(obj("type" to "Luxury_Resource", "name" to "Cotton", "amount" to 1, "duration" to 30)),
            )
            expectOperator("BARRIER_REQUIRED") { resolve(control, before.epoch, "Rome", "player_command", commandPayload) }
            control.handle(OperatorRequest("barrier", LEASE))
            expectOperator("LEASE_LOST") { control.handle(OperatorRequest("resolve_request", "wrong", before.epoch, civilization = "Rome", transport = "player_command", request = commandPayload)) }
            expectOperator("STALE_TURN") { resolve(control, "wrong-epoch", "Rome", "player_command", commandPayload) }
            expectOperator("INVALID_ARGUMENT") { resolve(control, before.epoch, "Rome", "player_communication", obj("op" to "state", "epoch" to before.epoch)) }
            check(resolve(control, before.epoch, "Rome", "player_command", Json.parseToJsonElement(PlayerTurnJson.encode(completedCommand)).jsonObject) == Json.parseToJsonElement(PlayerTurnJson.encode(receipt)).jsonObject)
            check(resolve(control, before.epoch, "Rome", "player_communication", send) == sent)
            val gameBefore = runtime.executor.turnSnapshot().gameJson
            val cancellation = resolve(control, before.epoch, "Rome", "player_command", commandPayload)
            check(cancellation["error"] == JsonPrimitive("REQUEST_CANCELLED"))
            check(resolve(control, before.epoch, "Rome", "player_command", commandPayload) == cancellation)
            check(PlayerHost(runtime).bindPlayer("Rome").completedReceipt(cancelledCommand)?.error == "REQUEST_CANCELLED")
            check(PlayerHost(runtime).bindPlayer("Rome").execute(cancelledCommand).error == "REQUEST_CANCELLED")
            expectOperator("REQUEST_ID_CONFLICT") { resolve(control, before.epoch, "Rome", "player_command", Json.parseToJsonElement(PlayerTurnJson.encode(cancelledCommand.copy(queue = listOf("Different")))).jsonObject) }
            check(resolve(control, before.epoch, "Rome", "player_communication", cancelledOffer)["error"] == JsonPrimitive("REQUEST_CANCELLED"))
            check(runtime.executor.communication.handle("Rome", cancelledOffer)["error"] == JsonPrimitive("REQUEST_CANCELLED"))
            expectPlayer(PlayerSessionFailure.REQUEST_ID_CONFLICT) { runtime.executor.communication.handle("Rome", JsonObject(cancelledOffer + ("partner" to JsonPrimitive("Rome")))) }
            check(runtime.executor.turnSnapshot().gameJson == gameBefore)
            // The resolution for one player never consumes another player's identity.
            check(resolve(control, before.epoch, "Greece", "player_communication", JsonObject(send + ("to" to JsonPrimitive("Rome"))))["error"] == JsonPrimitive("REQUEST_CANCELLED"))
            control.handle(OperatorRequest("unbarrier", LEASE))
            check(runtime.executor.communication.handle("Rome", cancelledOffer)["error"] == JsonPrimitive("REQUEST_CANCELLED"))
            race(runtime, control, before.epoch)
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            val host = PlayerHost(runtime)
            check(host.bindPlayer("Rome").completedReceipt(cancelledCommand)?.error == "REQUEST_CANCELLED")
            check(host.bindPlayer("Rome").execute(cancelledCommand).error == "REQUEST_CANCELLED")
            check(runtime.executor.communication.handle("Rome", cancelledOffer)["error"] == JsonPrimitive("REQUEST_CANCELLED"))
        }
    }

    private fun race(runtime: HeadlessRuntime, control: OperatorControl, epoch: String) {
        control.handle(OperatorRequest("barrier", LEASE))
        val start = CountDownLatch(1)
        val pool = Executors.newFixedThreadPool(2)
        try {
            repeat(4) { index ->
                val request = obj("op" to "send", "epoch" to epoch, "requestId" to "racing-$index", "to" to "Greece", "text" to "Racing message")
                val resolve = pool.submit<JsonObject> {
                    start.await()
                    resolve(control, epoch, "Rome", "player_communication", request)
                }
                val send = pool.submit<JsonObject> {
                    start.await()
                    try {
                        runtime.executor.communication.handle("Rome", request)
                    } catch (failure: PlayerSessionException) {
                        check(failure.reason == PlayerSessionFailure.COMMAND_UNAVAILABLE)
                        obj("blocked" to true)
                    }
                }
                start.countDown()
                check(resolve.get()["error"] == JsonPrimitive("REQUEST_CANCELLED"))
                check(send.get()["message"] == null)
            }
        } finally {
            pool.shutdownNow()
        }
    }

    private fun savedGame(root: Path) {
        val request = Json.parseToJsonElement(Files.readString(root.resolve("request.json"))).jsonObject
        val checkpoint = root.resolve("exploration.json")
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("loaded")).use { runtime ->
            val control = OperatorControl(runtime.executor)
            control.handle(OperatorRequest("acquire", LEASE))
            control.handle(OperatorRequest("barrier", LEASE))
            val before = runtime.executor.turnSnapshot().gameJson
            val receipt = resolve(control, runtime.executor.epoch, "Persia", "player_communication", request)
            check(receipt["error"] == JsonPrimitive("REQUEST_CANCELLED"))
            check(runtime.executor.turnSnapshot().gameJson == before)
            check(runtime.executor.communication.handle("Persia", request) == receipt)
            Files.writeString(root.resolve("receipt.json"), receipt.toString())
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("reloaded")).use { runtime ->
            check(runtime.executor.communication.handle("Persia", request)["error"] == JsonPrimitive("REQUEST_CANCELLED"))
        }
    }

    private fun resolve(control: OperatorControl, epoch: String, civilization: String, transport: String, request: JsonObject) = checkNotNull(control.handle(OperatorRequest("resolve_request", LEASE, epoch, civilization = civilization, transport = transport, request = request)).receipt)

    private fun expectOperator(code: String, block: () -> Unit) {
        val failure = runCatching(block).exceptionOrNull()
        check(failure is OperatorFailure && failure.code == code) { "Expected $code, got $failure" }
    }

    private fun expectPlayer(reason: PlayerSessionFailure, block: () -> Unit) {
        val failure = runCatching(block).exceptionOrNull()
        check(failure is PlayerSessionException && failure.reason == reason) { "Expected $reason, got $failure" }
    }
}
