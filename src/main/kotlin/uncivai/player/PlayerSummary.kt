package uncivai.player

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * A detached projection of the information this player may read about itself.
 *
 * [isYourTurn] identifies the current player only. It does not indicate that the player is ready
 * or that ending the turn is supported.
 */
@Serializable
data class PlayerSummary(
    val civilization: String,
    val ruleset: String,
    val turn: Int,
    val isYourTurn: Boolean,
    val treasury: Int,
    val cityCount: Int,
    val unitCount: Int,
    val currentResearch: String?,
    val freePolicies: Int,
    val freeTechnologies: Int,
    val freeGreatPeople: Int,
)

object PlayerSummaryJson {
    private val json = Json {
        explicitNulls = true
    }

    fun encode(summary: PlayerSummary): String = json.encodeToString(PlayerSummary.serializer(), summary)
}
