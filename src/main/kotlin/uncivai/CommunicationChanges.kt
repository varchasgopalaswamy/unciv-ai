package uncivai

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerSession
import java.util.concurrent.ConcurrentHashMap

/** Wakeups carry no messages. The checkpointed recipient stream is the source of truth. */
internal class CommunicationChanges {
    private val signals = ConcurrentHashMap<String, MutableStateFlow<Long>>()
    private var streams = emptyMap<String, Int>()
    private var negotiation: String? = null

    fun forPlayer(civilization: String): StateFlow<Long> = signals.computeIfAbsent(civilization) { MutableStateFlow(0L) }.asStateFlow()

    /** Called under the game executor only after a successful commit, also without a disk store. */
    fun committed(state: CommunicationState) {
        val current = state.streams.mapValues { it.value.size }
        val negotiationChanged = negotiation != state.trades.activeProposal
        for ((civilization, signal) in signals) {
            if (negotiationChanged || current[civilization] != streams[civilization]) signal.update { it + 1 }
        }
        streams = current
        negotiation = state.trades.activeProposal
    }

    /** Availability/credential changes and recovery force reauthorization on the next read. */
    fun invalidate() {
        signals.values.forEach { signal -> signal.update { it + 1 } }
    }
}

/** The model never waits here: its service owns this cancellable HTTPS subscription. */
internal suspend fun PlayerSession.awaitCommunication(arguments: JsonObject): JsonObject {
    val request = InputSchemas.communication.decode(CommunicationRequest.serializer(), arguments)
    if (request.waitSeconds == 0) return communication(arguments)
    val changes = communicationChanges()
    // Capture before reading, then compare in first(): a commit between read and subscribe
    // cannot be missed. Every wake reads through the session's authorization guard again.
    val result = withTimeoutOrNull(request.waitSeconds * 1000L) {
        var response: JsonObject
        while (true) {
            val version = changes.value
            response = communication(arguments)
            val waiting = if (request.op == "negotiation") {
                response.getValue("negotiation").jsonObject.getValue("active").jsonPrimitive.boolean
            } else {
                (response.getValue("events") as JsonArray).isEmpty()
            }
            if (!waiting) break
            changes.first { it != version }
        }
        response
    }
    // Recheck credentials, runtime availability and deadlines even on a quiet timeout.
    return result ?: communication(arguments)
}
