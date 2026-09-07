package uncivai.player

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/** Explicit coverage advertisement, independent of a player's private game state. */
@Serializable
data class PlayerCapabilities(
    val protocolVersion: Int = 1,
    val completeGameplay: Boolean = false,
    val queries: List<String> = listOf("player_capabilities", "player_summary", "owned_cities", "free_great_person_choice", "player_turn"),
    val commands: List<TurnCommandKind> = TurnCommandKind.entries.toList(),
    val durableRecovery: Boolean = false,
    val legacyMutationsSupported: Boolean = true,
)

@Serializable
enum class TurnCommandKind {
    SET_RESEARCH_QUEUE,
    CHOOSE_FREE_TECHNOLOGY,
    ADOPT_POLICY,
    DISMISS_POLICY_PICKER,
    QUEUE_CONSTRUCTION,
    CHOOSE_FREE_GREAT_PERSON,
    RENAME_CITY,
    ACKNOWLEDGE_ALERT,
    MOVE_UNIT,
    FOUND_CITY,
    END_TURN,
}

@Serializable
data class PendingTurnDecision(val kind: String, val supported: Boolean)

@Serializable
data class PlayerAlert(val handle: String, val kind: String, val subject: String?, val canAcknowledge: Boolean)

@Serializable
data class TurnCity(
    val handle: String,
    val name: String,
    val puppet: Boolean,
    val constructionQueue: List<String>,
    val constructionOptions: List<String>,
)

@Serializable
data class PlayerTurnSnapshot(
    val epoch: String,
    val revision: String,
    val turn: Int,
    val phase: String,
    val canEndTurn: Boolean,
    val pending: List<PendingTurnDecision>,
    val alerts: List<PlayerAlert>,
    val cities: List<TurnCity>,
    val researchQueue: List<String>,
    val researchOptions: List<String>,
    val freeTechnologies: Int,
    val policyOptions: List<String>,
    val freePolicies: Int,
    val freeGreatPeople: Int,
    val mayaLimited: Boolean,
    val greatPersonOptions: List<String>,
    val idleUnits: Int,
    val map: MapObservation = MapObservation(),
)

/** Fields unused by the selected command must be absent or have their default value. */
@Serializable
data class TurnCommandRequest(
    val requestId: String,
    val epoch: String,
    val revision: String,
    val kind: TurnCommandKind,
    val target: String? = null,
    val selection: String? = null,
    val queue: List<String> = emptyList(),
    val destination: TilePosition? = null,
    val confirmBreakPromise: Boolean = false,
)

@Serializable
data class TurnCommandReceipt(
    val requestId: String,
    val epoch: String,
    val kind: TurnCommandKind,
    val revision: String,
    val turn: Int,
    val isYourTurn: Boolean,
)

object PlayerTurnJson {
    val json = Json { encodeDefaults = true }

    fun encode(value: PlayerCapabilities): String = json.encodeToString(value)
    fun encode(value: PlayerTurnSnapshot): String = json.encodeToString(value)
    fun encode(value: TurnCommandRequest): String = json.encodeToString(value)
    fun encode(value: TurnCommandReceipt): String = json.encodeToString(value)
}
