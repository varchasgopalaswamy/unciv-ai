package uncivai

import uncivai.player.PlayerTurnSnapshot
import uncivai.player.PlayerTurnJson
import kotlinx.serialization.json.jsonObject

/** Loading rebuilds observations and invalidates old uncommitted commands. */
internal fun assertRebuiltObservation(previous: PlayerTurnSnapshot, current: PlayerTurnSnapshot) {
    check(current.revision != previous.revision) { "Restart retained a stale observation revision" }
    val normalized = current.copy(
        revision = previous.revision,
        turnReview = current.turnReview?.copy(id = checkNotNull(previous.turnReview).id),
    )
    check(normalized == previous) {
        val expected = PlayerTurnJson.json.encodeToJsonElement(PlayerTurnSnapshot.serializer(), previous).jsonObject
        val actual = PlayerTurnJson.json.encodeToJsonElement(PlayerTurnSnapshot.serializer(), normalized).jsonObject
        "Restart changed player observations or handles: " + expected.keys.filter { expected[it] != actual[it] }
    }
}
