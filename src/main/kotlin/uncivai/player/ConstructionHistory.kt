package uncivai.player

import com.unciv.logic.city.City
import kotlinx.serialization.Serializable
import java.util.Collections

/** Player-visible availability at selection time, preserved through queue moves and restart. */
@Serializable
data class ConstructionSelection(
    val name: String,
    val chosenTurn: Int?,
    val observedSinceTurn: Int,
    val options: List<String>,
)

@Serializable
data class ConstructionChoice(
    val name: String,
    val chosenTurn: Int?,
    val observedSinceTurn: Int,
    val turnsObserved: Int,
    val availableSinceChoice: List<String>,
)

internal fun observeConstructionChoice(city: City, options: List<String>, state: TurnSessionState): ConstructionChoice? {
    val queue = city.cityConstructions.constructionQueue
    val previous = state.constructionSelections[city.id].orEmpty()
    // Native completion removes the front of the queue. Match the suffix first,
    // including repeated unit names with distinct selection times.
    val remaining = (
        if (previous.size >= queue.size && previous.takeLast(queue.size).map { it.name } == queue) {
            previous.takeLast(queue.size)
        } else {
            previous
        }
        ).toMutableList()
    val selections = queue.map { name ->
        val index = remaining.indexOfFirst { it.name == name }
        if (index >= 0) {
            remaining.removeAt(index)
        } else {
            ConstructionSelection(
                name,
                null,
                city.civ.gameInfo.turns,
                (options + queue).distinct().sorted(),
            )
        }
    }
    state.constructionSelections[city.id] = selections
    val current = selections.firstOrNull() ?: return null
    return ConstructionChoice(
        current.name,
        current.chosenTurn,
        current.observedSinceTurn,
        (city.civ.gameInfo.turns - current.observedSinceTurn).coerceAtLeast(0),
        Collections.unmodifiableList(options.filter { it !in current.options }.sorted()),
    )
}

internal fun recordConstructionSelection(city: City, before: TurnCity, request: TurnCommandRequest, state: TurnSessionState) {
    state.constructionQueues.remove(city.id)
    val selections = state.constructionSelections[city.id].orEmpty().toMutableList()
    when (request.kind) {
        TurnCommandKind.QUEUE_CONSTRUCTION, TurnCommandKind.START_CONSTRUCTION -> selections.add(
            if (request.kind == TurnCommandKind.START_CONSTRUCTION) 0 else selections.size,
            ConstructionSelection(
                checkNotNull(request.selection),
                city.civ.gameInfo.turns,
                city.civ.gameInfo.turns,
                (before.constructionOptions + before.constructionQueue).distinct().sorted(),
            ),
        )

        TurnCommandKind.REMOVE_CONSTRUCTION -> selections.removeAt(checkNotNull(request.index))

        TurnCommandKind.MOVE_CONSTRUCTION -> selections.add(checkNotNull(request.toIndex), selections.removeAt(checkNotNull(request.index)))

        else -> return
    }
    state.constructionSelections[city.id] = selections.toList()
}

@Serializable
internal data class ConstructionQueueState(val queue: List<String>, val revision: String)

/** Durable queue identity, independent of unrelated native revisions.
 * Every explicit
 * queue edit invalidates the identity, including permutations of equal units.
 */
internal fun observeConstructionQueueRevision(city: City, state: TurnSessionState): String {
    val queue = city.cityConstructions.constructionQueue.toList()
    val prior = state.constructionQueues[city.id]
    if (prior != null && prior.queue == queue) return prior.revision
    val revision = java.util.UUID.randomUUID().toString()
    state.constructionQueues[city.id] = ConstructionQueueState(queue, revision)
    return revision
}
