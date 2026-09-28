package uncivai

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull

/** Read-only projection of committed communications. No player-facing state or rules. */
internal class DiplomacyMonitoring(private val runtime: GameExecutor) {
    private var epoch: String? = null
    private var cursors = emptyMap<String, Int>()

    data class Batch(val epoch: String, val cursors: Map<String, Int>, val records: List<MonitoringStore.Record>)

    val epochChanged get() = epoch != runtime.epoch

    fun collect(): Batch {
        val state = runtime.communicationState
        val rows = mutableListOf<MonitoringStore.Record>()
        for ((recipient, stream) in state.streams.toSortedMap()) {
            val offset = if (epoch == runtime.epoch) cursors[recipient] ?: 0 else 0
            for (event in stream.drop(offset)) {
                if (event.kind !in setOf("message", "offer", "consent", "trade", "engine", "agreement")) continue
                val body = event.body
                val participants = (body["participants"] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull }
                // Shared events are emitted to every participant. Use one stable
                // recipient copy, preserving repeated decisions with identical bodies.
                // Agreement lifecycle events have only a single recipient.
                if (participants != null && recipient != participants.minOrNull()) continue
                val audience = (participants ?: listOfNotNull(recipient, (body["partner"] as? JsonPrimitive)?.contentOrNull)).distinct().sorted()
                val id = "$recipient/${event.id}"
                val turn = (body["turn"] as? JsonPrimitive)?.intOrNull
                rows += MonitoringStore.Record(
                    "diplomacy",
                    "${runtime.epoch}/$id",
                    obj("id" to id, "epoch" to runtime.epoch, "kind" to event.kind, "participants" to audience, "turn" to turn, "body" to body),
                    epoch = runtime.epoch,
                    gameTurn = turn,
                )
            }
        }
        val active = state.trades.activeProposal?.let { state.trades.proposals.getValue(it).last().recordJson() }
        return Batch(
            runtime.epoch,
            state.streams.mapValues { it.value.size },
            rows + MonitoringStore.Record("state", "diplomacy", obj("epoch" to runtime.epoch, "activeNegotiation" to active), epoch = runtime.epoch),
        )
    }

    fun committed(batch: Batch) {
        epoch = batch.epoch
        cursors = batch.cursors
    }
}
