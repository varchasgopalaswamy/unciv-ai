package uncivai.player

import kotlinx.serialization.Serializable
import java.util.UUID

@Serializable
data class FreeGreatPersonSnapshot(
    val epoch: String,
    val decision: FreeGreatPersonDecision?,
)

@Serializable
data class FreeGreatPersonDecision(
    val handle: String,
    val remainingChoices: Int,
    val mayaLimited: Boolean,
    val options: List<String>,
)

@Serializable
data class ChooseFreeGreatPersonRequest(
    val requestId: String,
    val epoch: String,
    val decisionHandle: String,
    val unitName: String,
)

@Serializable
data class ChooseFreeGreatPersonReceipt(
    val requestId: String,
    val epoch: String,
    val decisionHandle: String,
    val unitName: String,
    val remainingChoices: Int,
)

internal data class FreeGreatPersonFingerprint(
    val remainingChoices: Int,
    val mayaLimitedChoices: Int,
    val options: List<String>,
    val turn: Int,
    val isYourTurn: Boolean,
)

internal data class SharedFreeGreatPersonDecision(
    val generation: String,
    val fingerprint: FreeGreatPersonFingerprint,
)

/** Runtime-owned decision identity. Callers serialize all access through HeadlessRuntime. */
internal class FreeGreatPersonDecisionTracker {
    private val decisionsByCivilization = HashMap<String, SharedFreeGreatPersonDecision>()

    fun synchronize(
        civilizationId: String,
        fingerprint: FreeGreatPersonFingerprint?,
    ): SharedFreeGreatPersonDecision? {
        if (fingerprint == null) {
            decisionsByCivilization.remove(civilizationId)
            return null
        }

        val current = decisionsByCivilization[civilizationId]
        if (current?.fingerprint == fingerprint) return current

        return SharedFreeGreatPersonDecision(
            generation = UUID.randomUUID().toString(),
            fingerprint = fingerprint,
        ).also { decisionsByCivilization[civilizationId] = it }
    }

    fun invalidate(civilizationId: String) {
        decisionsByCivilization.remove(civilizationId)
    }
}
