package uncivai.player

import kotlinx.serialization.Serializable

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

@Serializable
internal data class FreeGreatPersonFingerprint(
    val remainingChoices: Int,
    val mayaLimitedChoices: Int,
    val options: List<String>,
    val turn: Int,
    val isYourTurn: Boolean,
)

@Serializable
internal data class SharedFreeGreatPersonDecision(
    val handle: String,
    val fingerprint: FreeGreatPersonFingerprint,
)

/** Reconnect-stable decision identity, invalidated by changes to its permitted choices. */
internal fun TurnSessionState.synchronizeFreeGreatPersonDecision(fingerprint: FreeGreatPersonFingerprint?): SharedFreeGreatPersonDecision? {
    if (fingerprint == null) {
        freeGreatPersonDecision = null
        return null
    }
    freeGreatPersonDecision?.takeIf { it.fingerprint == fingerprint }?.let { return it }
    return SharedFreeGreatPersonDecision(identifiers.next(PlayerIdentifier.DECISION), fingerprint)
        .also { freeGreatPersonDecision = it }
}
