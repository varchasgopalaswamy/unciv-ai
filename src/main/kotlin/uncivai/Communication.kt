package uncivai

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.communicationContext
import uncivai.player.gameFinished
import java.security.SecureRandom

/** Shared records and recipient streams are part of the same commit as game receipts. */
@Serializable
internal data class CommunicationState(
    val telemetry: TelemetryState = TelemetryState(),
    val trades: EngineTradeState = EngineTradeState(),
    val agreements: AgreementState = AgreementState(),
    val streams: MutableMap<String, MutableList<CommunicationEvent>> = mutableMapOf(),
    val queuedMessages: MutableList<ChatMessage> = mutableListOf(),
    val offers: MutableMap<String, MutableList<SocialOffer>> = mutableMapOf(),
    val consents: MutableList<SocialConsent> = mutableListOf(),
    val outcomes: MutableList<EngineOutcome> = mutableListOf(),
    val requests: MutableMap<String, CommunicationReceipt> = mutableMapOf(),
    val acceptedVersions: MutableMap<String, Int> = mutableMapOf(),
    val jobs: MutableMap<String, CommunicationJob> = mutableMapOf(),
    var invocations: Long = 0,
    var maxInvocations: Long = 1000,
    var maxConcurrent: Int = 4,
    var maxPerCivilization: Int = 3,
    var restorePending: Boolean = false,
    val revokedCredentials: MutableSet<String> = mutableSetOf(),
    val playerCredentials: MutableMap<String, String> = mutableMapOf(),
    // Trusted controller configuration. Existing matches use agent controllers by default.
    val humanPlayers: MutableSet<String> = mutableSetOf(),
    // Trusted scheduler metadata. This is never exposed through a player session.
    val observedMajorWars: MutableSet<String> = mutableSetOf(),
    var observedWarTurn: Int? = null,
    var warDeclarationTurn: Int? = null,
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

internal fun shortCommunicationId(prefix: String, random: SecureRandom, occupied: (String) -> Boolean): String = generateSequence {
    prefix + random.nextInt(60_466_176).toString(36).padStart(5, '0')
}.first { !occupied(it) }

internal class Communication(private val runtime: GameExecutor) {
    internal val trades = EngineTrades(runtime)
    internal val agreements = AgreementLifecycle(runtime)
    private val random = SecureRandom()
    private val state get() = runtime.communicationState
    private fun fail(): Nothing = throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
    private fun shortId(prefix: String): String = shortCommunicationId(prefix, random) { candidate ->
        when (prefix) {
            "p" -> candidate in state.offers

            "m" -> state.queuedMessages.any { it.message == candidate } ||
                state.streams.values.any { stream -> stream.any { it.kind == "message" && it.body["message"] == JsonPrimitive(candidate) } }

            "o" -> state.outcomes.any { it.outcome == candidate }

            else -> error("Unknown reference kind")
        }
    }

    internal fun emit(audience: List<String>, kind: String, body: JsonObject, wakes: Set<String> = emptySet()) {
        for (recipient in audience) {
            val stream = state.streams.getOrPut(recipient) { mutableListOf() }
            stream.add(CommunicationEvent(stream.size + 1, kind, body, recipient in wakes))
        }
        runtime.telemetry.communicationChanged()
    }

    internal fun canQueueMessage(civilization: String): Boolean = runtime.communicationPaused &&
        runtime.pauseReason == "STRATEGIC_REVIEW" && !state.restorePending && civilization in state.humanPlayers

    private fun deliver(message: ChatMessage) {
        val recipients = if (message.public) message.mentions.toSet() else message.participants.toSet()
        emit(message.participants, "message", message.recordJson(), recipients - message.author)
    }

    /** Publish queued human chat in the same commit that opens communication after review. */
    internal fun deliverQueuedMessages() {
        check(!runtime.communicationPaused && !state.restorePending)
        state.queuedMessages.forEach(::deliver)
        state.queuedMessages.clear()
    }

    fun handle(civilization: String, arguments: JsonObject): JsonObject {
        val request = InputSchemas.communication.decode(CommunicationRequest.serializer(), arguments)
        return runtime.accessGame { game ->
            val participants = game.civilizations.filter { it.isHuman() && it.isMajorCiv() }.map { it.civID }
            if (civilization !in participants) fail()
            if (request.epoch != runtime.epoch) throw PlayerSessionException(PlayerSessionFailure.STALE_EPOCH)
            val own = state.streams[civilization].orEmpty()
            when (request.op) {
                "negotiation" -> return@accessGame obj("negotiation" to trades.negotiation(civilization))

                "receipt" -> return@accessGame state.requests["$civilization:${request.requestId}"]?.response ?: obj("error" to "NOT_ENACTED")

                "read" -> {
                    if (request.cursor !in 0..own.size) fail()
                    val page = own.drop(request.cursor).take(request.limit)
                    return@accessGame obj(
                        "epoch" to runtime.epoch,
                        "events" to page.map { it.recordJson() },
                        "nextCursor" to request.cursor + page.size,
                        "hasMore" to (request.cursor + page.size < own.size),
                    )
                }

                "state" -> return@accessGame view(civilization, participants)

                "trade_options", "trade_read", "trade_prepare" -> return@accessGame trades.read(civilization, request)

                "agreement_list" -> return@accessGame obj("agreements" to agreements.summaries(civilization))

                "agreement_read" -> return@accessGame agreements.read(civilization, checkNotNull(request.agreement))

                "job_claim", "job_heartbeat", "job_release", "job_report" -> return@accessGame job(civilization, request)
            }
            val requestKey = "$civilization:${checkNotNull(request.requestId)}"
            state.requests[requestKey]?.let {
                if (it.request != arguments) throw PlayerSessionException(PlayerSessionFailure.REQUEST_ID_CONFLICT)
                return@accessGame it.response
            }
            val queueMessage = request.op == "send" && request.nativeAction == null && canQueueMessage(civilization)
            if (runtime.communicationPaused && !queueMessage) throw PlayerSessionException(PlayerSessionFailure.COMMAND_UNAVAILABLE)
            fun settlementState(): TradeSettlementState {
                val majors = game.civilizations.filter { it.isMajorCiv() }.sortedBy { it.civID }
                return TradeSettlementState(
                    majors.associate { it.civID to it.gold },
                    majors.flatMap { a -> majors.filter { it !== a }.map { b -> ConfirmedTradeWar(a.civID, b.civID, a.isAtWarWith(b)) } },
                )
            }
            val beforeSettlement = if (request.op == "trade_decide" && request.decision == "accept") settlementState() else null
            val response = when (request.op) {
                "trade_offer", "trade_decide", "friendship_offer" -> trades.mutate(civilization, request)

                "send" -> {
                    val target = request.to
                    val nativeRecipient = game.civilizations.firstOrNull { it.civID == target && it.isAI() && it.isMajorCiv() && game.getCivilization(civilization).knows(it) }
                    if (target != null && (target == civilization || (target !in participants && (nativeRecipient == null || request.nativeAction == null)))) fail()
                    val audience = if (target == null) participants else listOf(civilization, target)
                    if (request.mentions.any { it !in audience }) fail()
                    val nativeResult = request.nativeAction?.let { uncivai.player.TurnSession(runtime, civilization).executeDiplomaticAction(it, target) }
                    val message = ChatMessage(shortId("m"), civilization, audience, target == null, checkNotNull(request.text), request.mentions, game.turns, request.nativeAction)
                    if (queueMessage) {
                        state.queuedMessages.add(message)
                        obj("message" to message.recordJson(), "queued" to true)
                    } else {
                        deliver(message)
                        obj("message" to message.recordJson(), "nativeOutcome" to nativeResult)
                    }
                }

                "offer", "revise" -> {
                    val audience = request.participants
                    if (audience.size !in 2..participants.size || civilization !in audience || audience.any { it !in participants }) fail()
                    val proposalId = if (request.op == "revise") checkNotNull(request.proposal) else shortId("p")
                    val prior = state.offers[proposalId]?.lastOrNull()
                    if (request.op == "revise" && (prior == null || prior.author != civilization || prior.participants != audience)) fail()
                    if (request.expiresTurn != null && request.expiresTurn <= game.turns) fail()
                    val messages = own.filter { it.kind == "message" }.map {
                        communicationJson.decodeFromJsonElement(ChatMessage.serializer(), it.body)
                    }
                    if (request.sources.any { source -> messages.none { it.message == source && it.participants.containsAll(audience) } }) {
                        return@accessGame obj(
                            "error" to "INVALID_TOOL_ARGUMENTS",
                            "applied" to false,
                            "recoverable" to true,
                            "invalidFields" to listOf(obj("fields" to listOf("sources"))),
                            "instruction" to "Copy message IDs visible to every participant, or omit sources. Text citations are not message IDs.",
                        )
                    }
                    val offer = SocialOffer(proposalId, (prior?.version ?: 0) + 1, civilization, audience, checkNotNull(request.kind), checkNotNull(request.terms), checkNotNull(request.conditions), request.expiresTurn, request.sources, game.turns)
                    state.offers.getOrPut(proposalId) { mutableListOf() }.add(offer)
                    emit(audience, "offer", offer.recordJson(), audience.toSet() - civilization)
                    consent(civilization, offer, "accept", "Published these terms", game.turns)
                    obj("offer" to offer.recordJson())
                }

                else -> {
                    val proposal = checkNotNull(request.proposal)
                    val version = checkNotNull(request.version)
                    val versions = state.offers[proposal] ?: fail()
                    val offer = versions.singleOrNull { it.version == version && civilization in it.participants } ?: fail()
                    if (request.op == "accept" && offer != versions.last()) throw PlayerSessionException(PlayerSessionFailure.STALE_REVISION)
                    if (request.op == "accept" && (expired(offer, game.turns) || withdrawn(offer))) throw PlayerSessionException(PlayerSessionFailure.COMMAND_UNAVAILABLE)
                    val reason = if (request.op == "accept") "Explicit consent" else checkNotNull(request.reason)
                    val existing = decisions(offer)[civilization]
                    if (existing?.decision != request.op) {
                        consent(civilization, offer, request.op, reason, game.turns)
                    }
                    if (accepted(offer, game.turns)) state.acceptedVersions[proposal] = version
                    obj("decision" to request.op, "proposal" to proposal, "version" to version, "accepted" to accepted(offer, game.turns))
                }
            }
            state.requests[requestKey] = CommunicationReceipt(
                arguments,
                response,
                CommunicationAudit(
                    runtime.handoff,
                    game.currentPlayer,
                    System.currentTimeMillis(),
                    if (beforeSettlement != null && response["outcome"] is JsonObject) TradeSettlementAudit(beforeSettlement, settlementState()) else null,
                ),
            )
            runtime.commitCheckpoint()
            response
        }
    }

    private fun job(civilization: String, request: CommunicationRequest): JsonObject {
        val id = checkNotNull(request.job)
        val now = System.currentTimeMillis()
        val prior = state.jobs[id]
        if (prior != null && prior.civilization != civilization) fail()
        val active = state.jobs.values.filter { it.expires > now }
        if (request.op == "job_claim" && prior == null) {
            val purpose = checkNotNull(request.purpose)
            val parent = request.parentJob?.let { state.jobs[it] ?: fail() }
            val openingReview = purpose == "review" && parent == null
            if (parent != null && (purpose != "review" || parent.civilization != civilization || parent.purpose != "gameplay")) fail()
            if (openingReview && runtime.accessGame { it.turns != 0 }) fail()
            val phaseAllowsJob = if (purpose == "review") runtime.communicationPaused && runtime.isPaused() else !runtime.communicationPaused
            if (!phaseAllowsJob || state.invocations >= state.maxInvocations || active.size >= state.maxConcurrent ||
                (purpose == "review" && runtime.accessGame(::gameFinished)) ||
                active.count { it.civilization == civilization } >= state.maxPerCivilization || active.any { it.civilization == civilization && it.purpose == purpose }
            ) {
                return obj("granted" to false)
            }
            state.jobs[id] = runtime.accessGame { game -> CommunicationJob(civilization, purpose, now + 90000, epoch = parent?.epoch ?: runtime.epoch, handoff = if (openingReview) null else parent?.handoff ?: runtime.handoff, gameTurn = parent?.gameTurn ?: game.turns, claimedAtMs = now) }
            state.invocations++
        } else if (prior != null) {
            if (request.op !in setOf("job_release", "job_report") && prior.expires <= now) return obj("granted" to false)
            if (request.op == "job_claim" && prior.purpose != request.purpose) fail()
            val report = request.telemetry
            val oldReport = prior.telemetry
            if (report != null && oldReport != null && report.sequence < oldReport.sequence) return obj("granted" to true)
            if (report != null && oldReport != null && report.sequence == oldReport.sequence && (report != oldReport || request.usage != prior.reportedUsage)) fail()
            if (report != null && request.usage != null && prior.reportedUsage != null && listOf("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens").any { request.usage.tokens(it) < prior.reportedUsage.tokens(it) }) fail()
            if (report != null) {
                if ((report.gameplayElapsedMs ?: 0) < 0 || (report.finalizationElapsedMs ?: 0) < 0 || report.modelRequests < 0) fail()
                if (report.memoryEdits?.valid() == false) fail()
                if (oldReport?.memoryEdits?.let { report.memoryEdits?.follows(it) != true } == true) fail()
                if (report.usageComplete && (!report.usageReported || request.usage == null || report.endedAtMs == null)) fail()
                if (oldReport != null && (report.elapsedMs < oldReport.elapsedMs || report.modelRequests < oldReport.modelRequests || (oldReport.usageComplete && !report.usageComplete) || oldReport.apiCalls.any { (name, count) -> (report.apiCalls[name]?.calls ?: 0) < count.calls })) fail()
            }
            state.jobs[id] = prior.copy(
                expires = if (request.op == "job_release") {
                    0
                } else if (request.op == "job_report") {
                    prior.expires
                } else {
                    now + 90000
                },
                usage = mergeUsage(prior.usage, request.usage),
                telemetry = report ?: prior.telemetry,
                reportedUsage = if (report != null) request.usage else prior.reportedUsage,
            )
        } else {
            fail()
        }
        runtime.telemetry.changed(id)
        runtime.commitCheckpoint()
        return obj("granted" to true)
    }

    private fun consent(author: String, offer: SocialOffer, decision: String, reason: String, turn: Int) {
        val record = SocialConsent(offer.proposal, offer.version, author, offer.participants, decision, reason, turn)
        state.consents.add(record)
        emit(offer.participants, "consent", record.recordJson(), offer.participants.toSet() - author)
    }
    private fun expired(offer: SocialOffer, turn: Int) = offer.expiresTurn?.let { it <= turn } == true
    private fun decisions(offer: SocialOffer) = state.consents.filter { it.proposal == offer.proposal && it.version == offer.version }.associateBy { it.author }
    private fun withdrawn(offer: SocialOffer) = decisions(offer).values.any { it.decision in setOf("withdraw", "reconsider") }
    private fun accepted(offer: SocialOffer, turn: Int): Boolean = !expired(offer, turn) && !withdrawn(offer) && offer.participants.all { decisions(offer)[it]?.decision == "accept" }

    fun view(civilization: String, participants: List<String>): JsonObject = runtime.accessGame { game ->
        obj(
            "epoch" to runtime.epoch, "civilization" to civilization,
            "context" to communicationContext(runtime, game, game.getCivilization(civilization)), "channels" to participants,
            "queuedMessages" to state.queuedMessages.filter { it.author == civilization }.map { it.recordJson() },
            "offers" to state.offers.values.flatten().filter { civilization in it.participants }.map { offer ->
                JsonObject(offer.recordJson() + obj("effective" to (state.acceptedVersions[offer.proposal] == offer.version && accepted(offer, game.turns))))
            },
            "consents" to state.consents.filter { civilization in it.participants }.map { it.recordJson() },
            "outcomes" to state.outcomes.filter { civilization in it.participants }.map { it.recordJson() },
            "trades" to trades.summaries(civilization), "negotiation" to trades.negotiation(civilization),
            "agreements" to agreements.summaries(civilization),
        )
    }

    /** Advice uses only the reader's own accepted statements; it never changes game legality. */
    internal fun peaceAdvisories(civilization: String, opponent: String? = null): List<SocialPromiseAdvisory> = runtime.accessGame { game ->
        java.util.Collections.unmodifiableList(
            state.offers.values.flatten().filter {
                state.acceptedVersions[it.proposal] == it.version && it.kind == "peace" &&
                    civilization in it.participants && (opponent == null || opponent in it.participants) && accepted(it, game.turns)
            }.map {
                SocialPromiseAdvisory(it.proposal, it.version, java.util.Collections.unmodifiableList(it.participants.toList()), it.terms, it.conditions, it.expiresTurn)
            },
        )
    }

    fun enacted(civilization: String, opponent: String?, receipt: TurnCommandReceipt) {
        if (opponent == null || receipt.kind !in setOf(TurnCommandKind.DECLARE_WAR, TurnCommandKind.RESPOND_FRIENDSHIP)) return
        diplomacyEnacted(civilization, opponent, if (receipt.kind == TurnCommandKind.RESPOND_FRIENDSHIP) "DECLARE_FRIENDSHIP" else receipt.kind.name, receipt.turn)
    }

    /** The affected pair learns the engine result, without a private trade's terms or sponsor. */
    internal fun diplomacyEnacted(civilization: String, opponent: String, action: String, turn: Int) {
        val audience = listOf(civilization, opponent)
        trades.invalidateDiplomacy(civilization, opponent)
        val player = runtime.accessGame { it.getCivilization(civilization) }
        val other = player.gameInfo.getCivilization(opponent)
        val record = EngineOutcome(
            outcome = shortId("o"),
            action = action,
            author = civilization,
            participants = audience,
            turn = turn,
            confirmation = bilateralConfirmation(player, other),
            relatedProposals = state.offers.values.map { it.last() }.filter { it.participants.toSet() == audience.toSet() }.map { ProposalReference(it.proposal, it.version) },
        )
        state.outcomes.add(record)
        emit(audience, "engine", record.recordJson(), audience.toSet() - civilization)
    }
}

private fun mergeUsage(old: ModelUsage?, current: ModelUsage?): ModelUsage? {
    if (old == null) return current
    if (current == null) return old
    return ModelUsage(maxOf(old.inputTokens, current.inputTokens), maxOf(old.cachedInputTokens, current.cachedInputTokens), maxOf(old.outputTokens, current.outputTokens), maxOf(old.reasoningOutputTokens, current.reasoningOutputTokens))
}
