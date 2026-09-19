package uncivai

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.json.jsonObject
import uncivai.player.AlertContent

/** Structural constraints, including each operation's required fields, live in communication.json. */
@Serializable
internal data class CommunicationRequest(
    val op: String,
    val epoch: String,
    val requestId: String? = null,
    val cursor: Int = 0,
    val limit: Int = 50,
    val waitSeconds: Int = 0,
    val job: String? = null,
    val purpose: String? = null,
    val usage: ModelUsage? = null,
    val to: String? = null,
    val text: String? = null,
    val mentions: List<String> = emptyList(),
    val proposal: String? = null,
    val agreement: String? = null,
    val version: Int? = null,
    val participants: List<String> = emptyList(),
    val terms: String? = null,
    val conditions: String? = null,
    val expiresTurn: Int? = null,
    val kind: String? = null,
    val sources: List<String> = emptyList(),
    val reason: String? = null,
    val partner: String? = null,
    val give: List<TradeSelection> = emptyList(),
    val receive: List<TradeSelection> = emptyList(),
    val decision: String? = null,
    val nativeAction: NativeDiplomaticAction? = null,
)

@Serializable
internal data class NativeDiplomaticAction(val kind: String, val target: String, val revision: String, val selection: String? = null)

@Serializable
internal data class ModelUsage(
    @SerialName("input_tokens") val inputTokens: Long = 0,
    @SerialName("cached_input_tokens") val cachedInputTokens: Long = 0,
    @SerialName("output_tokens") val outputTokens: Long = 0,
    @SerialName("reasoning_output_tokens") val reasoningOutputTokens: Long = 0,
) {
    fun tokens(key: String): Long = when (key) {
        "input_tokens" -> inputTokens
        "cached_input_tokens" -> cachedInputTokens
        "output_tokens" -> outputTokens
        "reasoning_output_tokens" -> reasoningOutputTokens
        else -> error("Unknown usage counter")
    }
}

@Serializable
internal data class CommunicationJob(val civilization: String, val purpose: String, val expires: Long, val usage: ModelUsage? = null)

@Serializable
internal data class CommunicationEvent(val id: Int, val kind: String, val body: JsonObject, val wake: Boolean)

@Serializable
internal data class ChatMessage(val message: String, val author: String, val participants: List<String>, val public: Boolean, val text: String, val mentions: List<String>, val turn: Int, val nativeAction: NativeDiplomaticAction? = null)

@Serializable
internal data class SocialOffer(
    val proposal: String,
    val version: Int,
    val author: String,
    val participants: List<String>,
    val kind: String,
    val terms: String,
    val conditions: String,
    val expiresTurn: Int?,
    val sources: List<String>,
    val turn: Int,
)

@Serializable
internal data class SocialConsent(val proposal: String, val version: Int, val author: String, val participants: List<String>, val decision: String, val reason: String, val turn: Int)

@Serializable
internal data class ProposalReference(val proposal: String, val version: Int)

/** A private reminder of stated intent, never a native restriction or a verdict on free-text conditions. */
@Serializable
data class SocialPromiseAdvisory(
    val proposal: String,
    val version: Int,
    val participants: List<String>,
    val terms: String,
    val conditions: String,
    val expiresTurn: Int?,
    val relevantActions: List<String> = java.util.Collections.unmodifiableList(listOf("DECLARE_WAR", "ATTACK", "CITY_ATTACK", "WAR_TRADE")),
    val message: String = "Hostile action against another participant may conflict with this accepted social peace promise. Consider its terms and conditions; you may proceed without withdrawing or reconsidering it. Native game rules still apply.",
)

@Serializable
internal data class TradedCityOwner(val city: String, val owner: String)

/** Only the declared relationship; no target's private agreements or military state. */
@Serializable
internal data class ConfirmedTradeWar(val declaringCivilization: String, val targetCivilization: String, val atWar: Boolean)

@Serializable
internal data class EngineOutcome(
    val action: String,
    val author: String,
    val participants: List<String>,
    val turn: Int,
    val confirmation: BilateralConfirmation,
    val outcome: String? = null,
    val proposal: String? = null,
    val version: Int? = null,
    val relatedProposals: List<ProposalReference> = emptyList(),
    val clauses: List<TradeClause> = emptyList(),
    val wars: List<ConfirmedTradeWar> = emptyList(),
    val friendship: FriendshipTerms? = null,
    val cities: List<TradedCityOwner> = emptyList(),
    val feedback: AlertContent? = null,
)

@Serializable
internal data class CommunicationAudit(val handoff: Long, val activeCivilization: String, val timeMillis: Long, val settlement: TradeSettlementAudit? = null)

/** Operator-only evidence in the checkpoint, never included in a player response or event. */
@Serializable
internal data class TradeSettlementState(val gold: Map<String, Int>, val wars: List<ConfirmedTradeWar>)

@Serializable
internal data class TradeSettlementAudit(val before: TradeSettlementState, val after: TradeSettlementState)

@Serializable
internal data class CommunicationReceipt(val request: JsonObject, val response: JsonObject, val audit: CommunicationAudit)

internal val communicationJson = Json { encodeDefaults = true }
internal inline fun <reified T> T.recordJson(): JsonObject = communicationJson.encodeToJsonElement(this).jsonObject
