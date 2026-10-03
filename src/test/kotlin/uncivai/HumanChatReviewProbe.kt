package uncivai

import com.unciv.json.json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import java.nio.file.Path

/** Human chat waits durably outside the frozen review position and recipient streams. */
object HumanChatReviewProbe {
    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args.single())
        val checkpoint = root.resolve("queued-chat.json")
        lateinit var privateRequest: JsonObject
        lateinit var privateResponse: JsonObject
        lateinit var publicResponse: JsonObject
        fun rejected(reason: PlayerSessionFailure = PlayerSessionFailure.COMMAND_UNAVAILABLE, block: () -> Unit) {
            val failure = runCatching(block).exceptionOrNull()
            check(failure is PlayerSessionException && failure.reason == reason) { "$failure" }
        }
        HeadlessRuntime.createVanillaGame(root.resolve("initial"), seed = 412L).use { runtime ->
            val executor = runtime.executor
            executor.communicationState.humanPlayers.add("France")
            executor.enableCheckpoints(checkpoint)
            val control = OperatorControl(executor)
            control.handle(OperatorRequest("acquire", "reviews"))
            control.handle(OperatorRequest("barrier", "reviews", pauseReason = "STRATEGIC_REVIEW"))
            val human = PlayerHost(runtime).bindPlayer("France")
            val roman = PlayerHost(runtime).bindPlayer("Rome")
            val before = json().toJson(runtime.game)
            privateRequest = obj("op" to "send", "epoch" to runtime.epoch, "requestId" to "private-chat", "to" to "Rome", "text" to "Private question after review")
            privateResponse = human.communication(privateRequest)
            check(privateResponse.flag("queued"))
            check(human.communication(privateRequest) == privateResponse)
            rejected(PlayerSessionFailure.REQUEST_ID_CONFLICT) {
                human.communication(JsonObject(privateRequest + obj("text" to "Changed request")))
            }
            publicResponse = human.communication(obj("op" to "send", "epoch" to runtime.epoch, "requestId" to "public-chat", "text" to "Public greeting after review", "mentions" to listOf("Greece")))
            check(publicResponse.flag("queued"))
            check(executor.communicationState.queuedMessages.size == 2)
            for (civ in listOf("France", "Rome", "Greece")) {
                val player = PlayerHost(runtime).bindPlayer(civ)
                check(player.communication(obj("op" to "read", "epoch" to runtime.epoch)).array("events").isEmpty())
                val state = player.communication(obj("op" to "state", "epoch" to runtime.epoch))
                check(state.array("queuedMessages").size == if (civ == "France") 2 else 0)
                check((state["context"] as JsonObject).flag("canQueueMessage") == (civ == "France"))
                if (civ != "France") check(!state.toString().contains("Private question"))
            }
            rejected { roman.communication(obj("op" to "send", "epoch" to runtime.epoch, "requestId" to "agent-chat", "to" to "France", "text" to "Agent must wait")) }
            rejected {
                human.communication(obj("op" to "send", "epoch" to runtime.epoch, "requestId" to "native-action", "to" to "Rome", "text" to "Denounce", "nativeAction" to obj("kind" to "DENOUNCE", "target" to "Rome", "revision" to human.turn().revision)))
            }
            rejected { human.communication(obj("op" to "trade_offer", "epoch" to runtime.epoch, "requestId" to "trade", "partner" to "Rome", "give" to emptyList<String>(), "receive" to emptyList<String>())) }
            control.handle(OperatorRequest("barrier", "reviews"))
            rejected { human.communication(JsonObject(privateRequest + obj("requestId" to "ordinary-pause"))) }
            check(json().toJson(runtime.game) == before) { "Queueing chat changed the frozen game" }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("resumed")).use { runtime ->
            val executor = runtime.executor
            val control = OperatorControl(executor)
            control.handle(OperatorRequest("acquire", "resumed-reviews"))
            val human = PlayerHost(runtime).bindPlayer("France")
            check(executor.communicationState.queuedMessages.size == 2)
            check(human.communication(privateRequest) == privateResponse)
            val before = json().toJson(runtime.game)
            control.handle(OperatorRequest("unbarrier", "resumed-reviews"))
            check(executor.communicationState.queuedMessages.isEmpty())
            val privateId = privateResponse.getValue("message").jsonObject.text("message")
            val publicId = publicResponse.getValue("message").jsonObject.text("message")
            for (civ in listOf("France", "Rome", "Greece")) {
                val events = executor.communicationState.streams.getValue(civ)
                check(events.map { it.body.text("message") } == if (civ == "Greece") listOf(publicId) else listOf(privateId, publicId))
                check(
                    events.filter { it.wake }.map { it.body.text("message") } == when (civ) {
                        "Rome" -> listOf(privateId)
                        "Greece" -> listOf(publicId)
                        else -> emptyList()
                    },
                )
            }
            // Lost responses and repeated phase completion cannot publish twice.
            check(human.communication(privateRequest) == privateResponse)
            control.handle(OperatorRequest("unbarrier", "resumed-reviews"))
            check(executor.communicationState.streams.getValue("Rome").size == 2)
            check(json().toJson(runtime.game) == before)

            control.handle(OperatorRequest("barrier", "resumed-reviews", pauseReason = "STRATEGIC_REVIEW"))
            val saved = executor.turnSnapshot()
            human.communication(JsonObject(privateRequest + obj("requestId" to "discarded-chat", "text" to "Discard this attempt")))
            executor.restoreTurn(saved, "rewound-human-chat")
            check(executor.communicationState.queuedMessages.isEmpty())
            check(!executor.communicationState.requests.containsKey("France:discarded-chat"))
            rejected { human.communication(obj("op" to "send", "epoch" to executor.epoch, "requestId" to "during-restore", "to" to "Rome", "text" to "Wait for restore")) }
        }
    }
}
