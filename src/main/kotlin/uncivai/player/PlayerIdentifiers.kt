package uncivai.player

import kotlinx.serialization.Serializable

/** Names are local to one authenticated player and game epoch, never engine IDs. */
@Serializable
internal enum class PlayerIdentifier(val prefix: String) {
    CITY("city"),
    UNIT("unit"),
    ALERT("alert"),
    NOTIFICATION("notice"),
    DECISION("decision"),
    REPORT("report"),
    REVISION("rev"),
}

/** Access under the game executor. Persist counters even after their objects disappear. */
internal class PlayerIdentifiers(saved: Map<PlayerIdentifier, Long> = emptyMap()) {
    private val counters = saved.toMutableMap()

    init {
        require(saved.values.all { it >= 0 }) { "Invalid player identifier counter" }
    }

    fun next(kind: PlayerIdentifier): String {
        val number = Math.incrementExact(counters.getOrDefault(kind, 0L))
        counters[kind] = number
        return kind.prefix + number
    }

    fun snapshot(): Map<PlayerIdentifier, Long> = counters.toMap()

    /** A rollback restores objects, but must not reissue any reference from the discarded attempt. */
    fun retainIssued(issued: Map<PlayerIdentifier, Long>) {
        for ((kind, number) in issued) counters[kind] = maxOf(counters.getOrDefault(kind, 0L), number)
    }
}
