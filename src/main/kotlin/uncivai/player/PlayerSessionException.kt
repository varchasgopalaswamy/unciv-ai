package uncivai.player

enum class PlayerSessionFailure {
    SESSION_CLOSED,
    PLAYER_UNAVAILABLE,
    QUERY_FAILED,
}

class PlayerSessionException(
    val reason: PlayerSessionFailure,
) : RuntimeException(messageFor(reason)) {
    companion object {
        private fun messageFor(reason: PlayerSessionFailure): String = when (reason) {
            PlayerSessionFailure.SESSION_CLOSED -> "Player session is closed"
            PlayerSessionFailure.PLAYER_UNAVAILABLE -> "Player is unavailable"
            PlayerSessionFailure.QUERY_FAILED -> "Player query failed"
        }
    }
}
