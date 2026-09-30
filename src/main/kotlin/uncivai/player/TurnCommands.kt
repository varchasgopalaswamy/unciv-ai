package uncivai.player

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.util.Collections

/** Explicit coverage advertisement, independent of a player's private game state. */
@Serializable
data class PlayerCapabilities(
    val protocolVersion: Int = 1,
    val completeGameplay: Boolean = false,
    val queries: List<String> = listOf("player_capabilities", "player_summary", "owned_cities", "free_great_person_choice", "player_turn", "player_result", "research_details", "policy_details", "victory_progress", "movement_preview", "mission_preview"),
    val commands: List<TurnCommandKind> = TurnCommandKind.entries.toList(),
    val durableRecovery: Boolean = false,
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
    DISMISS_NOTIFICATIONS,
    RESPOND_FRIENDSHIP,
    RESPOND_CITY_STATE_ALERT,
    DECLARE_WAR,
    CITY_STATE_ACTION,
    GIFT_UNIT,
    MOVE_UNIT,
    SET_UNIT_DESTINATION,
    CANCEL_UNIT_MOVEMENT,
    CREATE_WATER_IMPROVEMENT,
    REBASE_UNIT,
    PARADROP,
    AIR_SWEEP,
    NUCLEAR_STRIKE,
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
    ACTIVATE_GREAT_PERSON,
    ADD_SPACESHIP_PART,
    CAST_DIPLOMATIC_VOTE,
    ABSTAIN_DIPLOMATIC_VOTE,
    ACKNOWLEDGE_DIPLOMATIC_RESULTS,
    FOUND_CITY,
    START_WORKER_JOB,
    STOP_WORKER_JOB,
    SET_WORKED_TILE,
    SET_TILE_LOCKED,
    SET_CITY_FOCUS,
    SET_AVOID_GROWTH,
    SET_MANUAL_SPECIALISTS,
    ASSIGN_SPECIALIST,
    UNASSIGN_SPECIALIST,
    RESET_CITIZENS,
    SELL_BUILDING,
    REASSIGN_CITY_TILE,
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
    val friendship: FriendshipDecision? = null,
    val nativeDiplomacy: NativeDiplomaticDecision? = null,
)

@Serializable
data class FriendshipDecision(val civilizationId: String, val content: AlertContent, val accept: DiplomaticAction, val canDecline: Boolean, val duration: Int = com.unciv.logic.civilization.PlayerFriendshipOperations.DURATION)

@Serializable
data class TurnCity(
    val handle: String,
    val name: String,
    val puppet: Boolean,
    val constructionQueue: List<String>,
    val constructionOptions: List<String>,
    val health: Int,
    val maxHealth: Int,
    val defensiveStrength: Int,
    val attacksRemaining: Int,
    val development: CityDevelopment? = null,
    val attacks: List<AttackOption> = emptyList(),
    val captureOptions: List<CaptureOption> = emptyList(),
    val purchases: CityPurchases? = null,
    val management: CityManagement? = null,
    val constructionChoice: ConstructionChoice? = null,
    val productionConversions: Map<String, ProductionConversion> = emptyMap(),
    val constructionEstimates: Map<String, ConstructionEstimate> = emptyMap(),
)

@Serializable
data class PlayerTurnSnapshot(
    val epoch: String,
    val revision: String,
    val turn: Int,
    val phase: String,
    val canEndTurn: Boolean,
    val rules: GameRules,
    val civilizationProfile: CivilizationProfile,
    val constructionCatalogue: List<String>,
    val negotiation: kotlinx.serialization.json.JsonObject = uncivai.obj("active" to false),
    val turnReview: TurnReview? = null,
    val previousTurnReview: CompletedTurnReview? = null,
    val scenario: GameplayScenario? = null,
    val result: PlayerResult? = null,
    val combatReports: List<CombatReport> = emptyList(),
    val pending: List<PendingTurnDecision>,
    val alerts: List<PlayerAlert>,
    val notifications: List<PlayerNotification> = emptyList(),
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
    val settlement: SettlementOpportunities? = null,
    val militaryReadiness: MilitaryReadiness? = null,
    val wonderOpportunities: List<WonderOpportunity> = emptyList(),
    val research: ResearchDevelopment? = null,
    val culture: CultureOverview? = null,
    val greatPeople: List<GreatPersonProgress> = emptyList(),
    val diplomacy: DiplomacyObservation = DiplomacyObservation(),
    val diplomacyOpportunities: DiplomacyOpportunities? = null,
    val economy: EconomyObservation? = null,
    val victory: VictoryDecisions = VictoryDecisions(),
    val socialAdvisories: List<uncivai.SocialPromiseAdvisory> = emptyList(),
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
    val reviewId: String? = null,
    val deferReason: String? = null,
    val attackFrom: TilePosition? = null,
    val confirmAction: Boolean = false,
    val enabled: Boolean? = null,
    val targets: List<String> = emptyList(),
)

@Serializable
data class TurnCommandReceipt(
    val requestId: String,
    val epoch: String,
    val kind: TurnCommandKind,
    val revision: String,
    val turn: Int,
    val isYourTurn: Boolean,
    val actionSummary: String,
    val feedback: AlertContent? = null,
    val combat: CombatOutcome? = null,
    val economy: EconomyOutcome? = null,
    val cityManagement: CityManagementOutcome? = null,
    val greatPerson: GreatPersonOutcome? = null,
    val victory: VictoryOutcome? = null,
    val cityState: CityStateOutcome? = null,
    val movement: MovementOutcome? = null,
    val waterImprovement: WaterImprovementOutcome? = null,
    val mission: MissionOutcome? = null,
    val dismissedNotifications: List<PlayerNotification> = emptyList(),
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
    targets = Collections.unmodifiableList(targets.toList()),
)

internal fun TurnCommandReceipt.detached(): TurnCommandReceipt = copy(
    movement = movement?.copy(orderBefore = movement.orderBefore?.detached(), orderAfter = movement.orderAfter?.detached()),
    dismissedNotifications = Collections.unmodifiableList(dismissedNotifications.map { it.detached() }),
    greatPerson = greatPerson?.detached(),
    mission = mission?.detached(),
    victory = victory?.detached(),
    cityState = cityState?.detached(),
    cityManagement = cityManagement?.detached(),
    economy = economy?.copy(units = Collections.unmodifiableList(economy.units.toList())),
    feedback = feedback?.let {
        it.copy(
            paragraphs = Collections.unmodifiableList(it.paragraphs.toList()),
            additionalAcknowledgements = Collections.unmodifiableList(it.additionalAcknowledgements.toList()),
        )
    },
)
