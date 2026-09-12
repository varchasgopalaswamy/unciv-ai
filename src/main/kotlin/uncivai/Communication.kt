package uncivai

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import java.security.SecureRandom

/** Shared records and recipient streams are part of the same commit as game receipts. */
@Serializable
internal data class CommunicationState(
    val streams: MutableMap<String, MutableList<JsonObject>> = mutableMapOf(),
    val offers: MutableMap<String, MutableList<JsonObject>> = mutableMapOf(),
    val consents: MutableList<JsonObject> = mutableListOf(),
    val outcomes: MutableList<JsonObject> = mutableListOf(),
    val requests: MutableMap<String, JsonObject> = mutableMapOf(),
    val peaceVersions: MutableMap<String, Long> = mutableMapOf(),
    val acceptedVersions: MutableMap<String, Int> = mutableMapOf(),
    val jobs: MutableMap<String, JsonObject> = mutableMapOf(),
    var invocations: Long = 0,
    var maxInvocations: Long = 1000,
    var maxConcurrent: Int = 4,
    var maxPerCivilization: Int = 2,
    var coordinatedRecovery: Boolean = false,
    var restorePending: Boolean = false,
    val revokedCredentials: MutableSet<String> = mutableSetOf(),
    val playerCredentials: MutableMap<String, String> = mutableMapOf(),
)

internal fun obj(vararg pairs: Pair<String, Any?>): JsonObject = buildJsonObject {
    for ((key, value) in pairs) {
        put(
            key,
            when (value) {
                null -> JsonNull
                is JsonElement -> value
                is String -> JsonPrimitive(value)
                is Number -> JsonPrimitive(value)
                is Boolean -> JsonPrimitive(value)
                is Collection<*> -> JsonArray(value.map { if (it is JsonElement) it else JsonPrimitive(it.toString()) })
                else -> error("Unsupported JSON value")
            },
        )
    }
}

internal class Communication(private val runtime: GameExecutor) {
    private val random = SecureRandom()
    private val state get() = runtime.communicationState
    private fun fail(): Nothing = throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
    private fun string(request: JsonObject, key: String, max: Int = 128): String = (request[key] as? JsonPrimitive)?.takeIf {
        it.isString
    }?.content?.takeIf { it.isNotBlank() && it.length <= max }
        ?: fail()
    private fun members(value: JsonElement?): List<String> = (value as? JsonArray)?.map { (it as? JsonPrimitive)?.takeIf { p -> p.isString }?.content ?: fail() }?.distinct()
        ?: fail()
    private fun shortId(prefix: String) = prefix + java.lang.Long.toUnsignedString(random.nextLong(), 36).take(9)
    private fun visible(record: JsonObject, civilization: String) = civilization in members(record["participants"])
    private fun emit(audience: List<String>, kind: String, body: JsonObject, wakes: Set<String> = emptySet()) {
        for (recipient in audience) {
            val stream = state.streams.getOrPut(recipient) { mutableListOf() }
            stream.add(obj("id" to stream.size + 1, "kind" to kind, "body" to body, "wake" to (recipient in wakes)))
        }
    }
    fun handle(civilization: String, request: JsonObject): JsonObject = runtime.accessGame { game ->
        val participants = game.civilizations.filter { it.isHuman() && it.isMajorCiv() }.map { it.civID }
        if (civilization !in participants) fail()
        if (string(request, "epoch") != runtime.epoch) throw PlayerSessionException(PlayerSessionFailure.STALE_EPOCH)
        val op = string(request, "op")
        val own = state.streams[civilization].orEmpty()
        if (op == "receipt") {
            if (request.keys != setOf("op", "epoch", "requestId")) fail()
            return@accessGame state.requests[
                "$civilization:${string(
                    request,
                    "requestId",
                )}",
            ]?.get("response")?.jsonObject
                ?: obj("error" to "NOT_ENACTED")
        }
        if (op == "read") {
            if (request.keys.any { it !in setOf("op", "epoch", "cursor", "limit") }) fail()
            val cursor = request["cursor"]?.jsonPrimitive?.intOrNull ?: 0
            val limit = request["limit"]?.jsonPrimitive?.intOrNull ?: 50
            if (cursor !in 0..own.size || limit !in 1..100) fail()
            val page = own.drop(cursor).take(limit)
            return@accessGame obj(
                "epoch" to runtime.epoch,
                "events" to page,
                "nextCursor" to cursor + page.size,
                "hasMore" to (cursor + page.size < own.size),
            )
        }
        if (op == "state") {
            if (request.keys != setOf("op", "epoch")) fail()
            return@accessGame view(civilization, participants)
        }
        if (op in setOf("job_claim", "job_heartbeat", "job_release")) {
            if (request.keys.any { it !in setOf("op", "epoch", "job", "purpose", "usage") }) fail()
            val job = string(request, "job")
            val now = System.currentTimeMillis()
            val prior = state.jobs[job]
            if (prior != null && prior["civilization"]?.jsonPrimitive?.content != civilization) fail()
            val active = state.jobs.values.filter { (it["expires"]?.jsonPrimitive?.long ?: 0) > now }
            if (op == "job_claim" && prior == null) {
                val purpose = string(request, "purpose")
                if (purpose !in setOf("gameplay", "communication")) fail()
                if (runtime.communicationPaused || state.invocations >= state.maxInvocations ||
                    active.size >= state.maxConcurrent ||
                    active.count { it["civilization"]?.jsonPrimitive?.content == civilization } >=
                    state.maxPerCivilization ||
                    active.any {
                        it["civilization"]?.jsonPrimitive?.content == civilization &&
                            it["purpose"]?.jsonPrimitive?.content == purpose
                    }
                ) {
                    return@accessGame obj("granted" to false)
                }
                state.jobs[job] = obj("civilization" to civilization, "purpose" to purpose, "expires" to now + 90000)
                state.invocations++
            } else if (prior != null) {
                if (op != "job_release" &&
                    (prior["expires"]?.jsonPrimitive?.long ?: 0) <= now
                ) {
                    return@accessGame obj("granted" to false)
                }
                state.jobs[job] =
                    JsonObject(
                        prior +
                            obj(
                                "expires" to (
                                    if (op ==
                                        "job_release"
                                    ) {
                                        0
                                    } else {
                                        now + 90000
                                    }
                                    ),
                                "usage" to (request["usage"] ?: prior["usage"]),
                            ),
                    )
            } else {
                fail()
            }
            runtime.commitCheckpoint()
            return@accessGame obj("granted" to true)
        }
        if (runtime.communicationPaused) throw PlayerSessionException(PlayerSessionFailure.COMMAND_UNAVAILABLE)
        val id = string(request, "requestId")
        val requestKey = "$civilization:$id"
        state.requests[requestKey]?.let {
            if (it["request"] != request) throw PlayerSessionException(PlayerSessionFailure.REQUEST_ID_CONFLICT)
            return@accessGame it.getValue("response").jsonObject
        }
        val common = setOf("op", "epoch", "requestId")
        val allowed = common + when (op) {
            "send" -> setOf("to", "text", "mentions")

            "offer", "revise" -> setOf(
                "proposal",
                "participants",
                "terms",
                "conditions",
                "expiresTurn",
                "kind",
                "sources",
            )

            "accept", "withdraw", "reconsider" -> setOf("proposal", "version", "reason")

            else -> fail()
        }
        if (request.keys.any { it !in allowed }) fail()
        val response = when (op) {
            "send" -> {
                val target = request["to"]?.let { string(request, "to") }
                if (target != null && (target !in participants || target == civilization)) fail()
                val audience = if (target == null) participants else listOf(civilization, target)
                val mentions = request["mentions"]?.let(::members).orEmpty()
                if (mentions.any { it !in audience }) fail()
                val message = obj(
                    "message" to shortId("m"),
                    "author" to civilization,
                    "participants" to audience,
                    "public" to (target == null),
                    "text" to string(request, "text", 4000),
                    "mentions" to mentions,
                    "turn" to game.turns,
                )
                emit(
                    audience,
                    "message",
                    message,
                    (if (target != null) setOf(target) else mentions.toSet()) - civilization,
                )
                obj("message" to message)
            }

            "offer", "revise" -> {
                val audience = members(request["participants"])
                if (audience.size !in 2..participants.size || civilization !in audience ||
                    audience.any { it !in participants }
                ) {
                    fail()
                }
                val proposalId = if (op == "revise") string(request, "proposal") else shortId("p")
                val prior = state.offers[proposalId]?.lastOrNull()
                if (op == "revise" &&
                    (
                        prior == null || prior["author"]?.jsonPrimitive?.content != civilization ||
                            members(prior["participants"]) != audience
                        )
                ) {
                    fail()
                }
                val kind = string(request, "kind")
                if (kind !in setOf("peace", "other")) fail()
                val expires = request["expiresTurn"]?.jsonPrimitive?.intOrNull
                if (request.containsKey("expiresTurn") && (expires == null || expires <= game.turns)) fail()
                val sources = request["sources"]?.let(::members).orEmpty()
                if (sources.any { source ->
                        own.none { event ->
                            event["body"]?.jsonObject?.let { body ->
                                body["message"]?.jsonPrimitive?.content ==
                                    source &&
                                    members(body["participants"]).containsAll(audience)
                            } ==
                                true
                        }
                    }
                ) {
                    fail()
                }
                val version = (prior?.get("version")?.jsonPrimitive?.int ?: 0) + 1
                val offer = obj(
                    "proposal" to proposalId, "version" to version, "author" to civilization,
                    "participants" to audience, "kind" to kind, "terms" to string(request, "terms", 4000),
                    "conditions" to string(request, "conditions", 2000), "expiresTurn" to expires,
                    "sources" to sources, "turn" to game.turns,
                )
                state.offers.getOrPut(proposalId) { mutableListOf() }.add(offer)
                emit(audience, "offer", offer, audience.toSet() - civilization)
                // Publishing exact terms is the author's explicit consent to them.
                consent(civilization, offer, "accept", "Published these terms", game.turns)
                bumpPeace(audience)
                obj("offer" to offer)
            }

            else -> {
                val proposal = string(request, "proposal")
                val version = request["version"]?.jsonPrimitive?.intOrNull ?: fail()
                val versions = state.offers[proposal] ?: fail()
                val offer =
                    versions.singleOrNull { it["version"]?.jsonPrimitive?.int == version && visible(it, civilization) }
                        ?: fail()
                if (op == "accept" &&
                    offer != versions.last()
                ) {
                    throw PlayerSessionException(PlayerSessionFailure.STALE_REVISION)
                }
                if (op == "accept" &&
                    (expired(offer, game.turns) || withdrawn(offer))
                ) {
                    throw PlayerSessionException(PlayerSessionFailure.COMMAND_UNAVAILABLE)
                }
                val reason = if (op == "accept") "Explicit consent" else string(request, "reason", 1000)
                val existing = state.consents.lastOrNull {
                    it["proposal"] == offer["proposal"] &&
                        it["version"] == offer["version"] &&
                        it["author"]?.jsonPrimitive?.content == civilization
                }
                if (existing?.get("decision")?.jsonPrimitive?.content != op) {
                    consent(civilization, offer, op, reason, game.turns)
                    bumpPeace(members(offer["participants"]))
                }
                if (accepted(offer, game.turns)) state.acceptedVersions[proposal] = version
                obj(
                    "decision" to op,
                    "proposal" to proposal,
                    "version" to version,
                    "accepted" to accepted(offer, game.turns),
                )
            }
        }
        state.requests[requestKey] = obj(
            "request" to request,
            "response" to response,
            "audit" to
                obj(
                    "handoff" to runtime.handoff,
                    "activeCivilization" to game.currentPlayer,
                    "timeMillis" to System.currentTimeMillis(),
                ),
        )
        runtime.commitCheckpoint()
        response
    }
    private fun consent(author: String, offer: JsonObject, decision: String, reason: String, turn: Int) {
        val record = obj(
            "proposal" to offer["proposal"],
            "version" to offer["version"],
            "author" to author,
            "participants" to offer["participants"],
            "decision" to decision,
            "reason" to reason,
            "turn" to turn,
        )
        state.consents.add(record)
        emit(members(offer["participants"]), "consent", record, members(offer["participants"]).toSet() - author)
    }
    private fun expired(offer: JsonObject, turn: Int) = offer["expiresTurn"]?.jsonPrimitive?.intOrNull?.let { it <= turn } == true
    private fun decisions(offer: JsonObject) = state.consents.filter {
        it["proposal"] == offer["proposal"] &&
            it["version"] == offer["version"]
    }.associateBy { it.getValue("author").jsonPrimitive.content }
    private fun withdrawn(offer: JsonObject) = decisions(offer).values.any {
        it["decision"]?.jsonPrimitive?.content in
            setOf("withdraw", "reconsider")
    }
    private fun accepted(offer: JsonObject, turn: Int): Boolean = !expired(offer, turn) && !withdrawn(offer) &&
        members(offer["participants"]).all {
            decisions(offer)[it]?.get("decision")?.jsonPrimitive?.content ==
                "accept"
        }
    private fun bumpPeace(participants: List<String>) {
        participants.forEach { state.peaceVersions[it] = (state.peaceVersions[it] ?: 0) + 1 }
    }
    fun view(civilization: String, participants: List<String>): JsonObject = obj(
        "epoch" to runtime.epoch,
        "civilization" to civilization,
        "channels" to participants,
        "peaceVersion" to (state.peaceVersions[civilization] ?: 0),
        "offers" to state.offers.values.flatMap {
            it.filter { offer -> visible(offer, civilization) }.map { offer ->
                JsonObject(
                    offer +
                        obj(
                            "effective" to
                                (
                                    state.acceptedVersions[offer["proposal"]?.jsonPrimitive?.content] ==
                                        offer["version"]?.jsonPrimitive?.int &&
                                        accepted(offer, runtime.accessGame { game -> game.turns })
                                    ),
                        ),
                )
            }
        },
        "consents" to state.consents.filter { visible(it, civilization) },
        "outcomes" to state.outcomes.filter { visible(it, civilization) },
    )
    fun validate(civilization: String, request: TurnCommandRequest, opponent: String?, turn: Int) {
        if (request.socialVersion != null &&
            request.socialVersion != (state.peaceVersions[civilization] ?: 0)
        ) {
            throw PlayerSessionException(PlayerSessionFailure.STALE_REVISION)
        }
        if (request.kind !in
            setOf(TurnCommandKind.ATTACK, TurnCommandKind.CITY_ATTACK, TurnCommandKind.DECLARE_WAR)
        ) {
            return
        }
        if (opponent == null) return // Attack target is resolved only after validating its offered destination.
        if (state.offers.values.flatten().any {
                state.acceptedVersions[it["proposal"]?.jsonPrimitive?.content] ==
                    it["version"]?.jsonPrimitive?.int &&
                    it["kind"]?.jsonPrimitive?.content == "peace" &&
                    visible(it, civilization) &&
                    visible(it, opponent) &&
                    accepted(it, turn)
            }
        ) {
            throw PlayerSessionException(PlayerSessionFailure.COMMAND_UNAVAILABLE)
        }
    }
    fun enacted(civilization: String, opponent: String?, receipt: TurnCommandReceipt) {
        if (opponent == null ||
            receipt.kind !in
            setOf(
                TurnCommandKind.DECLARE_WAR,
                TurnCommandKind.PROPOSE_PEACE,
                TurnCommandKind.ACCEPT_PEACE,
                TurnCommandKind.REJECT_PEACE,
                TurnCommandKind.COUNTER_PEACE,
                TurnCommandKind.RETRACT_PEACE,
            )
        ) {
            return
        }
        val audience = listOf(civilization, opponent)
        val player = runtime.accessGame { it.getCivilization(civilization) }
        val other = player.gameInfo.getCivilization(opponent)
        val record = obj(
            "outcome" to shortId("o"),
            "action" to receipt.kind.name,
            "author" to civilization,
            "participants" to audience,
            "turn" to receipt.turn,
            "confirmation" to obj("source" to "engine", "atWar" to player.isAtWarWith(other)),
            "relatedProposals" to
                state.offers.values.map {
                    it.last()
                }.filter {
                    members(it["participants"]).toSet() == audience.toSet()
                }.map { obj("proposal" to it["proposal"], "version" to it["version"]) },
        )
        state.outcomes.add(record)
        emit(audience, "engine", record, audience.toSet() - civilization)
        bumpPeace(audience)
    }
}
