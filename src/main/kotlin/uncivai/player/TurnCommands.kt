package uncivai.player

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.util.Collections

/** Explicit coverage advertisement, independent of a player's private game state. */
@Serializable
data class PlayerCapabilities(
    val protocolVersion: Int = 1,
    val completeGameplay: Boolean = false,
    val queries: List<String> = listOf("player_capabilities", "player_summary", "owned_cities", "free_great_person_choice", "player_turn", "player_result"),
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
    DECLARE_WAR,
    PROPOSE_PEACE,
    ACCEPT_PEACE,
    REJECT_PEACE,
    COUNTER_PEACE,
    RETRACT_PEACE,
    MOVE_UNIT,
    ATTACK,
    CITY_ATTACK,
    SET_UNIT_ORDER,
    PROMOTE_UNIT,
    RESOLVE_CAPTURED_CITY,
    MANAGE_CAPTURED_CITY,
    BUY_CONSTRUCTION,
    BUY_TILE,
    UPGRADE_UNIT,
    PILLAGE,
    DISBAND_UNIT,
    FOUND_CITY,
    START_WORKER_JOB,
    STOP_WORKER_JOB,
    SET_WORKED_TILE,
    SET_TILE_LOCKED,
    REMOVE_CONSTRUCTION,
    MOVE_CONSTRUCTION,
    END_TURN,
}

@Serializable
data class PendingTurnDecision(val kind: String, val supported: Boolean)

@Serializable
data class PlayerAlert(
    val handle: String,
    val kind: String,
    val subject: String?,
    val canAcknowledge: Boolean,
    val firstContact: FirstContactDetails? = null,
    val content: AlertContent? = null,
    val capture: CaptureDecision? = null,
)

@Serializable
data class TurnCity(
    val handle: String,
    val name: String,
    val puppet: Boolean,
    val constructionQueue: List<String>,
    val constructionOptions: List<String>,
    val development: CityDevelopment? = null,
    val attacks: List<AttackOption> = emptyList(),
    val captureOptions: List<CaptureOption> = emptyList(),
    val purchases: CityPurchases? = null,
)

@Serializable
data class PlayerTurnSnapshot(
    val epoch: String,
    val revision: String,
    val turn: Int,
    val phase: String,
    val canEndTurn: Boolean,
    val turnReview: TurnReview? = null,
    val previousTurnReview: CompletedTurnReview? = null,
    val scenario: GameplayScenario? = null,
    val result: PlayerResult? = null,
    val combatReports: List<CombatReport> = emptyList(),
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
    val research: ResearchDevelopment? = null,
    val diplomacy: DiplomacyObservation = DiplomacyObservation(),
    val economy: EconomyObservation? = null,
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
    val worked: Boolean? = null,
    val locked: Boolean? = null,
    val index: Int? = null,
    val toIndex: Int? = null,
    val confirmWar: Boolean = false,
    val peaceTerms: PeaceTerms? = null,
    val reviewId: String? = null,
    val deferReason: String? = null,
    val attackFrom: TilePosition? = null,
    val confirmAction: Boolean = false,
)

@Serializable
data class TurnCommandReceipt(
    val requestId: String,
    val epoch: String,
    val kind: TurnCommandKind,
    val revision: String,
    val turn: Int,
    val isYourTurn: Boolean,
    val feedback: AlertContent? = null,
    val combat: CombatOutcome? = null,
    val economy: EconomyOutcome? = null,
)

object PlayerTurnJson {
    val json = Json { encodeDefaults = true }

    fun encode(value: PlayerCapabilities): String = json.encodeToString(value)
    fun encode(value: PlayerTurnSnapshot): String = json.encodeToString(value)
    fun encode(value: TurnCommandRequest): String = json.encodeToString(value)
    fun encode(value: TurnCommandReceipt): String = json.encodeToString(value)
}

/** Deserialized checkpoint collections and caller-owned lists must never enter the receipt cache. */
internal fun TurnCommandRequest.detached(): TurnCommandRequest = copy(
    queue = Collections.unmodifiableList(queue.toList()),
    peaceTerms = peaceTerms?.let {
        PeaceTerms(Collections.unmodifiableList(it.give.toList()), Collections.unmodifiableList(it.receive.toList()))
    },
)

internal fun TurnCommandReceipt.detached(): TurnCommandReceipt = copy(
    economy = economy?.copy(units = Collections.unmodifiableList(economy.units.toList())),
    feedback = feedback?.let {
        it.copy(
            paragraphs = Collections.unmodifiableList(it.paragraphs.toList()),
            additionalAcknowledgements = Collections.unmodifiableList(it.additionalAcknowledgements.toList()),
        )
    },
)
