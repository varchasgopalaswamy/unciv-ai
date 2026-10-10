package uncivai

import com.unciv.json.json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import java.nio.file.Path

/** Exact private audiences use the same atomic streams, receipts and recovery as direct chat. */
object GroupChatProbe {
    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args.single())
        val checkpoint = root.resolve("groups.json")
        val players = listOf("Rome", "Greece", "France", "China")
        lateinit var original: JsonObject
        lateinit var response: JsonObject
        lateinit var queuedRequest: JsonObject
        lateinit var queuedResponse: JsonObject
        lateinit var beforeQueue: Map<String, Int>
        fun rejected(block: () -> Unit) {
            val failure = runCatching(block).exceptionOrNull()
            check(failure is PlayerSessionException && failure.reason == PlayerSessionFailure.INVALID_ARGUMENT) { "$failure" }
        }
        HeadlessRuntime.createVanillaGame(root.resolve("initial"), seed = 415L, builtInAI = listOf("Egypt"), agentPlayers = players).use { runtime ->
            val executor = runtime.executor
            val rome = runtime.game.getCivilization("Rome")
            rome.diplomacyFunctions.makeCivilizationsMeet(runtime.game.getCivilization("Greece"))
            rome.diplomacyFunctions.makeCivilizationsMeet(runtime.game.getCivilization("Egypt"))
            executor.communicationState.humanPlayers.add("Rome")
            executor.enableCheckpoints(checkpoint)
            val sessions = players.associateWith { PlayerHost(runtime).bindPlayer(it) }
            val sender = sessions.getValue("Rome")
            val outsider = sessions.getValue("China")
            val outsiderRevision = outsider.turn().revision
            val signals = sessions.mapValues { it.value.communicationChanges() }
            val outsiderSignal = signals.getValue("China").value
            val nativeBefore = json().toJson(runtime.game)
            var sequence = 0
            fun send(civ: String, vararg fields: Pair<String, Any?>): JsonObject = sessions.getValue(civ).communication(
                obj("op" to "send", "epoch" to runtime.epoch, "requestId" to "group-${++sequence}", *fields),
            ).getValue("message").jsonObject
            fun read(civ: String) = sessions.getValue(civ).communication(obj("op" to "read", "epoch" to runtime.epoch, "limit" to 100))
            original = obj("op" to "send", "epoch" to runtime.epoch, "requestId" to "original", "recipients" to listOf("Greece", "France"), "text" to "Private three player plan", "mentions" to listOf("Greece"))
            response = sender.communication(original)
            val message = response.getValue("message").jsonObject
            check(!message.flag("public"))
            check(message.array("participants").map { it.toString().trim('"') } == listOf("France", "Greece", "Rome"))
            for (civ in players - "China") {
                val event = read(civ).array("events").single().jsonObject
                check(event["body"] == message)
                check(event.flag("wake") == (civ != "Rome")) // Mentions never narrow private wakeups.
            }
            check(read("China").array("events").isEmpty())
            check(signals.getValue("China").value == outsiderSignal)
            check(outsider.turn().revision == outsiderRevision)
            for (op in listOf("state", "receipt")) {
                val view = outsider.communication(obj("op" to op, "epoch" to runtime.epoch, *if (op == "receipt") arrayOf("requestId" to "original") else emptyArray()))
                check(!view.toString().contains("Private three player plan") && !view.toString().contains(checkNotNull(message.text("message"))))
            }
            check(sender.communication(original) == response)
            check(read("Rome").array("events").size == 1)
            val reordered = send("Rome", "recipients" to listOf("France", "Greece"), "text" to "Same conversation")
            val reply = send("Greece", "recipients" to listOf("Rome", "France"), "text" to "Reply to all")
            check(reordered["participants"] == message["participants"] && reply["participants"] == message["participants"])
            val direct = send("Rome", "to" to "Greece", "text" to "Direct only")
            val single = send("Rome", "recipients" to listOf("Greece"), "text" to "Also direct")
            check(direct["participants"] == single["participants"] && !single.flag("public"))
            check(!read("France").toString().contains("Direct only") && !read("France").toString().contains("Also direct"))
            val overlapping = send("Rome", "recipients" to listOf("China", "Greece"), "text" to "Different private plan")
            check(!read("France").toString().contains("Different private plan"))
            check(!read("China").toString().contains("Private three player plan"))
            val public = send("Rome", "text" to "Public greeting", "mentions" to listOf("China"))
            val everyone = send("Rome", "recipients" to listOf("China", "Greece", "France"), "text" to "Private even with everyone")
            check(public.flag("public") && !everyone.flag("public"))
            val conversation = DesktopChatConversation.fromMessage(message)
            check(conversation.matches(reordered) && conversation.matches(reply))
            check(!conversation.matches(direct) && !conversation.matches(overlapping))
            check(!DesktopChatConversation.fromMessage(everyone).matches(public))
            check(DesktopChatConversation.fromMessage(direct).matches(single))

            val badFields = listOf(
                obj("recipients" to emptyList<String>()),
                obj("recipients" to null),
                obj("recipients" to "Greece"),
                obj("recipients" to listOf("Greece", "Greece")),
                obj("recipients" to listOf("Rome", "Greece")),
                obj("recipients" to listOf("Egypt")),
                obj("recipients" to listOf("Greece", "Unknown")),
                obj("recipients" to listOf("")),
                obj("recipients" to listOf("Greece"), "to" to "France"),
                obj("recipients" to listOf("Greece"), "mentions" to listOf("France")),
                obj("recipients" to listOf("Greece", "France"), "nativeAction" to obj("kind" to "DENOUNCE", "target" to "Greece", "revision" to sender.turn().revision)),
            )
            val streamsBefore = executor.communicationState.streams.mapValues { it.value.toList() }
            val receiptsBefore = executor.communicationState.requests.toMap()
            for (fields in badFields) {
                rejected { sender.communication(JsonObject(obj("op" to "send", "epoch" to runtime.epoch, "requestId" to "invalid-${++sequence}", "text" to "Must never deliver") + fields)) }
            }
            check(executor.communicationState.streams == streamsBefore && executor.communicationState.requests == receiptsBefore)
            check(json().toJson(runtime.game) == nativeBefore) { "Text or rejected native actions changed the engine" }

            val control = OperatorControl(executor)
            control.handle(OperatorRequest("acquire", "review"))
            control.handle(OperatorRequest("barrier", "review", pauseReason = "STRATEGIC_REVIEW"))
            beforeQueue = executor.communicationState.streams.mapValues { it.value.size }
            val signalBeforeQueue = signals.mapValues { it.value.value }
            queuedRequest = obj("op" to "send", "epoch" to runtime.epoch, "requestId" to "queued-group", "recipients" to listOf("Greece", "France"), "text" to "Queued first")
            queuedResponse = sender.communication(queuedRequest)
            check(queuedResponse.flag("queued") && sender.communication(queuedRequest) == queuedResponse)
            send("Rome", "recipients" to listOf("China", "Greece"), "text" to "Queued overlap")
            send("Rome", "recipients" to listOf("France", "Greece"), "text" to "Queued last")
            check(executor.communicationState.queuedMessages.size == 3)
            check(executor.communicationState.streams.mapValues { it.value.size } == beforeQueue)
            check(signals.mapValues { it.value.value } == signalBeforeQueue)
            for (civ in players) {
                val state = sessions.getValue(civ).communication(obj("op" to "state", "epoch" to runtime.epoch))
                check(state.array("queuedMessages").size == if (civ == "Rome") 3 else 0)
                if (civ != "Rome") check(!state.toString().contains("Queued"))
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restarted")).use { runtime ->
            val executor = runtime.executor
            val sender = PlayerHost(runtime).bindPlayer("Rome")
            check(sender.communication(original) == response)
            check(sender.communication(queuedRequest) == queuedResponse)
            val control = OperatorControl(executor)
            control.handle(OperatorRequest("acquire", "resumed"))
            control.handle(OperatorRequest("unbarrier", "resumed"))
            for (civ in players) {
                val delivered = executor.communicationState.streams.getValue(civ).drop(beforeQueue.getValue(civ))
                val expected = when (civ) {
                    "France" -> listOf("Queued first", "Queued last")
                    "China" -> listOf("Queued overlap")
                    else -> listOf("Queued first", "Queued overlap", "Queued last")
                }
                check(delivered.map { it.body.text("text") } == expected)
                check(delivered.all { it.wake == (civ != "Rome") })
            }
            val deliveredCounts = executor.communicationState.streams.mapValues { it.value.size }
            check(sender.communication(queuedRequest) == queuedResponse)
            control.handle(OperatorRequest("unbarrier", "resumed"))
            check(executor.communicationState.queuedMessages.isEmpty())
            check(executor.communicationState.streams.mapValues { it.value.size } == deliveredCounts)
            control.handle(OperatorRequest("barrier", "resumed", pauseReason = "STRATEGIC_REVIEW"))
            val saved = executor.turnSnapshot()
            val abandoned = JsonObject(queuedRequest + obj("requestId" to "abandoned", "text" to "Abandoned group"))
            sender.communication(abandoned)
            control.handle(OperatorRequest("unbarrier", "resumed"))
            control.handle(OperatorRequest("barrier", "resumed", pauseReason = "STRATEGIC_REVIEW"))
            sender.communication(JsonObject(abandoned + obj("requestId" to "abandoned-queued")))
            executor.restoreTurn(saved, "group-rewind")
            check(executor.communicationState.streams.mapValues { it.value.size } == deliveredCounts)
            check(executor.communicationState.queuedMessages.isEmpty())
            check(executor.communicationState.requests.keys.none { it.contains("abandoned") })
        }
    }
}
