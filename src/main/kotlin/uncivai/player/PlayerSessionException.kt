package uncivai.player

enum class PlayerSessionFailure {
    SESSION_CLOSED,
    PLAYER_UNAVAILABLE,
    QUERY_FAILED,
    NOT_YOUR_TURN,
    STALE_EPOCH,
    CITY_UNAVAILABLE,
    UNIT_UNAVAILABLE,
    INVALID_ARGUMENT,
    REQUEST_ID_CONFLICT,
    GAME_HALTED,
    DECISION_UNAVAILABLE,
    OPTION_UNAVAILABLE,
    PLACEMENT_FAILED,
    GAME_PAUSED,
    NEGOTIATION_ACTIVE,
    STALE_REVISION,
    OBSERVATION_REQUIRED,
    TURN_BLOCKED,
    TURN_REVIEW_REQUIRED,
    COMMAND_UNAVAILABLE,
    CIVILIAN_CAPTURE_RISK,
}

class PlayerSessionException(
    val reason: PlayerSessionFailure,
    val movementRisk: CivilianMovementRisk? = null,
    val rejectedMove: TurnCommandRequest? = null,
) : RuntimeException(messageFor(reason)) {
    companion object {
        private fun messageFor(reason: PlayerSessionFailure): String = when (reason) {
            PlayerSessionFailure.SESSION_CLOSED -> "Player session is closed"
            PlayerSessionFailure.PLAYER_UNAVAILABLE -> "Player is unavailable"
            PlayerSessionFailure.QUERY_FAILED -> "Player query failed"
            PlayerSessionFailure.NOT_YOUR_TURN -> "It is not your turn"
            PlayerSessionFailure.STALE_EPOCH -> "Game epoch is stale"
            PlayerSessionFailure.CITY_UNAVAILABLE -> "City is unavailable"
            PlayerSessionFailure.UNIT_UNAVAILABLE -> "Unit is unavailable"
            PlayerSessionFailure.INVALID_ARGUMENT -> "Invalid argument"
            PlayerSessionFailure.REQUEST_ID_CONFLICT -> "Request ID conflicts with an earlier request"
            PlayerSessionFailure.GAME_HALTED -> "Game is halted"
            PlayerSessionFailure.DECISION_UNAVAILABLE -> "Decision is unavailable"
            PlayerSessionFailure.OPTION_UNAVAILABLE -> "Option is unavailable"
            PlayerSessionFailure.PLACEMENT_FAILED -> "Unit could not be placed"
            PlayerSessionFailure.GAME_PAUSED -> "Game is paused"
            PlayerSessionFailure.NEGOTIATION_ACTIVE -> "Gameplay is held for a trade negotiation. The service waits until it closes, then requests a fresh observation."
            PlayerSessionFailure.STALE_REVISION -> "Player observation is stale"
            PlayerSessionFailure.OBSERVATION_REQUIRED -> "Read player_turn and the current message, terms, confirmation or turn review before responding"
            PlayerSessionFailure.TURN_BLOCKED -> "Resolve pending choices before ending the turn"
            PlayerSessionFailure.TURN_REVIEW_REQUIRED -> "Read the current turnReview in player_turn and use its reviewId for END_TURN. Idle units do not require action or a deferReason."
            PlayerSessionFailure.COMMAND_UNAVAILABLE -> "Command is unavailable"
            PlayerSessionFailure.CIVILIAN_CAPTURE_RISK -> "The move would expose a civilian to possible capture. Read captureRisk, choose another move or explicitly accept it with confirmAction=true. Nothing moved."
        }
    }
}
