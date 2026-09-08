package uncivai

import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest

/** Non-movement fixtures deliberately leave available units in place after reading the review. */
internal fun TurnCommandRequest.reviewedFor(snapshot: PlayerTurnSnapshot): TurnCommandRequest = if (kind == TurnCommandKind.END_TURN) {
    copy(
        reviewId = snapshot.turnReview?.id,
        deferReason = if (snapshot.turnReview?.units?.any { it.idle && it.legalDestinations > 0 } == true) {
            "Keep these units in position while this fixture tests other player actions."
        } else {
            null
        },
    )
} else {
    this
}
