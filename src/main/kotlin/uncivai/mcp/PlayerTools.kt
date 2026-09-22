package uncivai.mcp

import io.modelcontextprotocol.kotlin.sdk.server.Server
import io.modelcontextprotocol.kotlin.sdk.server.ServerOptions
import io.modelcontextprotocol.kotlin.sdk.types.CallToolResult
import io.modelcontextprotocol.kotlin.sdk.types.Implementation
import io.modelcontextprotocol.kotlin.sdk.types.ServerCapabilities
import io.modelcontextprotocol.kotlin.sdk.types.TextContent
import io.modelcontextprotocol.kotlin.sdk.types.ToolAnnotations
import io.modelcontextprotocol.kotlin.sdk.types.ToolSchema
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import uncivai.InputSchema
import uncivai.InputSchemas
import uncivai.PlayerApiAccounting
import uncivai.awaitCommunication
import uncivai.player.ChooseFreeGreatPersonRequest
import uncivai.player.MovementPreview
import uncivai.player.MovementPreviewRequest
import uncivai.player.PlayerCommandJson
import uncivai.player.PlayerFinalState
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSummaryJson
import uncivai.player.PlayerTurnJson
import uncivai.player.PolicyDetails
import uncivai.player.RenameCityRequest
import uncivai.player.ResearchDetails
import uncivai.player.TurnCommandRequest
import uncivai.player.VictoryProgress

internal fun createPlayerToolsServer(session: PlayerSession): Server = Server(
    serverInfo = Implementation(name = "unciv-player", version = "0.1.0"),
    options = ServerOptions(
        capabilities = ServerCapabilities(
            tools = ServerCapabilities.Tools(listChanged = false),
        ),
    ),
).apply {
    addTool(
        name = "player_receipt",
        description = "Trusted recovery transport: read an exact previously executed request without executing it.",
        inputSchema = TURN_COMMAND_INPUT_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "player_receipt", request.arguments) {
            val arguments = request.arguments ?: throw InvalidToolArgumentsException()
            val command = InputSchemas.turnCommand.decode(TurnCommandRequest.serializer(), arguments)
            session.completedReceipt(command)?.let(PlayerTurnJson::encode) ?: "{\"error\":\"NOT_ENACTED\"}"
        }
    }
    addTool(
        name = "player_communication",
        description = "Authenticated host communication transport. Civilization services manage requests, cursors, and subscriptions.",
        inputSchema = COMMUNICATION_INPUT_SCHEMA,
        toolAnnotations = COMMAND_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "player_communication", request.arguments) {
            val arguments = request.arguments?.get("request") as? JsonObject ?: throw InvalidToolArgumentsException()
            session.awaitCommunication(arguments).toString()
        }
    }

    addTool(
        name = "player_capabilities",
        description = "Return implemented gameplay operations and explicit coverage and recovery limitations.",
        inputSchema = NO_ARGUMENTS_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "player_capabilities", request.arguments) {
            request.arguments.requireNoArguments()
            PlayerTurnJson.encode(session.capabilities())
        }
    }

    addTool(
        name = "player_turn",
        description = "Return your turn status and turnReview first: confirmed actions and each owned unit's remaining movement and legal destination count. Read this before END_TURN; previousTurnReview records deliberate deferrals. scenario includes exercise instructions and phase. Parse the response and print selected fields to avoid truncation. Controllable units are map.units, with combat.attacks, combat.promotions.choices and combat.orders; map.tiles[].units contains visible summaries only. Also returns decisions, city yields/growth/citizen options and production progress/queues, research progress, policy and Great Person options, explored map and owned units. Informational alerts include content with the full title, paragraphs, quote and acknowledgement text from the desktop popup; technology descriptions are specific to your civilization. Read all content and note useful information before acknowledging. Ordinary notifications include full text, category, icons and event locations; read these and update private assessments as useful before DISMISS_NOTIFICATIONS. They do not block end turn. FirstContact also identifies the met civilization. Diplomacy lists known surviving majors, relationship status, remaining peace treaty turns, legal actions and unavailable reasons, declarationConfirmation warnings, and active agreements. Negotiate binding economic and peace terms through communication trade_options/read/offer/decide, including off turn. Imported UI trade requests remain readable unsupported decisions. Visible tiles include displayed yields; workers include legal job options, prerequisites and work progress. Units have current-turn movement destinations and previews; speculative paths can stop early on discovery. Coordinates are axial x/y. Revision changes only with your observed state or successful commands. Combat previews include attack origins, paths, strength modifiers and damage ranges. Read combatReports and capture alerts in full. Tactical orders and promotions include unavailable reasons. economy shows your finances, maintenance, happiness, resource balances and city connections. cities[].purchases lists gold construction/tile purchases with costs, descriptions and unavailable reasons. map.units[].economy lists upgrades, pillage and disband options. Read their costs and confirmation text before acting. map.units[].greatPerson exposes special abilities, effects, unit consumption and unavailable reasons. Research and policy descriptions are available on demand from research_details and policy_details. Unsupported decisions block end turn.",
        inputSchema = NO_ARGUMENTS_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "player_turn", request.arguments) {
            request.arguments.requireNoArguments()
            PlayerTurnJson.encode(session.turn())
        }
    }

    addTool(
        name = "player_command",
        description = "Execute one command using the revision from player_turn. MOVE_UNIT uses target=unit handle and offered destination; movement may stop early. FOUND_CITY uses settler target and requires confirmBreakPromise=true if settlementPromises is nonempty. START_WORKER_JOB uses unit target and selection=available job name; STOP_WORKER_JOB uses unit target. SET_WORKED_TILE / SET_TILE_LOCKED use city target, an offered development.tiles destination and worked / locked boolean. REMOVE_CONSTRUCTION uses city target and queue index; MOVE_CONSTRUCTION also uses an offered toIndex. Append with QUEUE_CONSTRUCTION then reorder to change production. SET_RESEARCH_QUEUE replaces the entire research queue, including current research. Put technologies in prerequisite order; the first becomes active. To finish Mining before Calendar, send queue=[Mining, Calendar]. Stored science is retained. ACKNOWLEDGE_ALERT uses target=an alert handle with canAcknowledge=true and full content already read from player_turn at this revision, including FirstContact; a revision from a command receipt alone is insufficient (OBSERVATION_REQUIRED). This dismisses only that message, including WarDeclaration. DISMISS_NOTIFICATIONS takes targets=[notice handles] with canDismiss=true from a fresh player_turn; the whole batch is validated before clearing any messages. It clears the player API pending list, preserves native notification history, and returns the exact dismissedNotifications in the receipt. DECLARE_WAR uses target=known civilizationId and requires confirmWar=true after reading declarationConfirmation at this revision. RESPOND_FRIENDSHIP uses an alert target and selection=Accept or Decline after reading its friendship content and choices. Bilateral trades and friendship proposals use the shared communication trade tools; Opening a negotiation pauses gameplay until it closes; services wait through communication. Successful requests replay; restart recovery requires durableRecovery. Re-observe after commands. END_TURN requires canEndTurn=true and reviewId=turnReview.id from a fresh player_turn at this revision. If any review units are idle with legalDestinations>0, act or supply a short deferReason explaining intentional idling. Read the review even if a prior report claimed movement was unavailable. Any mutation invalidates the review. TURN_REVIEW_REQUIRED means re-read the review and provide acknowledgment; this never forces movement. ATTACK uses an owned unit target and the exact offered destination and attackFrom; CITY_ATTACK uses an owned city and offered destination; omit attackFrom (only ATTACK accepts it). Read receipt.combat: discovery can stop movement before an attack. SET_UNIT_ORDER and PROMOTE_UNIT use an owned unit and selection=available option name. RESOLVE_CAPTURED_CITY uses the capture alert handle (including civilian return and traded-city KeepCity/Liberate choices) and selection=available option name after reading the full decision at this revision. MANAGE_CAPTURED_CITY uses an owned city and available captureOptions selection. BUY_CONSTRUCTION uses city target and purchases.constructions selection=name; BUY_TILE uses city target and purchases.tiles destination (no selection). UPGRADE_UNIT uses unit target and economy.upgrades selection=targetName. PILLAGE and DISBAND_UNIT use unit target without selection. Re-read player_turn before these economic commands; read costs and any confirmation text, and set confirmAction=true when confirmation is present. Read receipt.economy for actual gold and unit changes; re-observe afterward. City management commands require a fresh player_turn at this revision. SET_CITY_FOCUS uses city target and management.focusOptions selection=name. SET_AVOID_GROWTH and SET_MANUAL_SPECIALISTS use city target and enabled boolean. ASSIGN_SPECIALIST / UNASSIGN_SPECIALIST use city target and management.specialists selection=name; each changes one citizen. Assignment needs a free citizen: unwork a tile first if necessary. RESET_CITIZENS uses city target and clears tile locks before automatic allocation. SELL_BUILDING uses city target and management.buildingSales selection=name; read the refund, reasons and confirmation, then set confirmAction=true. REASSIGN_CITY_TILE uses receiving city target and an available management.sharedTiles destination; it needs a free citizen and releases the donor city's tile lock. Read receipt.cityManagement for actual allocations, yields, removed buildings and gold changes. ACTIVATE_GREAT_PERSON uses an owned unit target and selection=an available greatPerson.abilities name from a fresh player_turn. Abilities act at the current tile: move first if needed. Read effects and consumesUnit before acting, then read receipt.greatPerson and any new alerts or choices. CITY_STATE_ACTION uses a known diplomacy.cityStates civilizationId target and available actions.name selection; set confirmAction=true when its confirmation is nonempty. DECLARE_WAR also accepts known city-states. GIFT_UNIT uses an owned unit target with cityStateGift.action.available=true. ACTIVATE_GREAT_PERSON also supports native trade missions. Inspect receipt.cityState for actual influence, alliance, quest and unit changes. For distant movement read movement_preview(target,destination), then SET_UNIT_DESTINATION with the same choices. The native order resumes at the start of your turns. Inspect unit.movementOrder; CANCEL_UNIT_MOVEMENT stops it. For Work Boats read unit.waterImprovement, then CREATE_WATER_IMPROVEMENT with the unit target and selection=its offered name; the unit is consumed. Inspect receipt.movement and receipt.waterImprovement for actual effects. ADD_SPACESHIP_PART uses an owned unit target with spaceshipAssembly.available=true; it consumes that part in your capital. victory.ballot supplies world leader candidates: CAST_DIPLOMATIC_VOTE uses selection=civilizationId; ABSTAIN_DIPLOMATIC_VOTE has no target or selection. Read victory.votingResults before ACKNOWLEDGE_DIPLOMATIC_RESULTS (no target or selection). Read receipt.victory and refresh after these actions; terminal results remain available through player_result.",
        inputSchema = TURN_COMMAND_INPUT_SCHEMA,
        toolAnnotations = COMMAND_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "player_command", request.arguments) {
            val arguments = request.arguments ?: throw InvalidToolArgumentsException()
            val command = InputSchemas.turnCommand.decode(TurnCommandRequest.serializer(), arguments)
            PlayerTurnJson.encode(session.execute(command))
        }
    }

    addTool(
        name = "player_result",
        description = "Read your victory or defeat result and pending combat reports, including after scheduling stops. A null result means no result is recorded for you yet.",
        inputSchema = NO_ARGUMENTS_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "player_result", request.arguments) {
            request.arguments.requireNoArguments()
            PlayerTurnJson.json.encodeToString(PlayerFinalState.serializer(), session.result())
        }
    }

    addTool(
        name = "mission_preview",
        description = "Preview REBASE, PARADROP, AIR_SWEEP or NUCLEAR_STRIKE for an owned unit target and destination x/y. Read unit.air for mission ranges, bases/capacity and automatic interception readiness. Read this preview at the current revision before REBASE_UNIT, PARADROP, AIR_SWEEP or NUCLEAR_STRIKE with matching target/destination. Nuclear strikes require confirmAction=true after reading consequences; known victims are not exhaustive. Sweeps never reveal interceptors in advance. Ordinary air/missile strikes use ATTACK and its offered preview; interception can prevent the strike. Read receipt.mission and player_turn combatReports for actual effects.",
        inputSchema = InputSchemas.missionPreview.toolSchema(),
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "mission_preview", request.arguments) {
            val preview = InputSchemas.missionPreview.decode(uncivai.player.MissionPreviewRequest.serializer(), request.arguments ?: throw InvalidToolArgumentsException())
            PlayerTurnJson.json.encodeToString(uncivai.player.MissionPreview.serializer(), session.missionPreview(preview))
        }
    }

    addTool(
        name = "movement_preview",
        description = "Preview a destination using Unciv pathfinding. Supply an owned unit handle and destination x/y. Returns adjacent paths grouped by estimated turns; speculative paths can change as terrain and units become visible. Founding units also get settlement: siteStatus ELIGIBLE/BLOCKED/UNKNOWN, visible native siteBlockers, and separate unitBlockers such as NO_MOVEMENT. UNKNOWN means nearby visibility is incomplete; it does not prohibit movement or founding. canFoundNow applies only while occupying the destination. Movement availability is not settlement eligibility; inspect this before choosing a city site, then recheck unit_details sections=[settlement] on arrival. Read this at the current revision before SET_UNIT_DESTINATION with the same target and destination. That command moves now and saves the native order for later turns. Inspect unit.movementOrder; CANCEL_UNIT_MOVEMENT stops it. Estimates are not promises of arrival.",
        inputSchema = InputSchemas.movementPreview.toolSchema(),
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "movement_preview", request.arguments) {
            val preview = InputSchemas.movementPreview.decode(MovementPreviewRequest.serializer(), request.arguments ?: throw InvalidToolArgumentsException())
            PlayerTurnJson.json.encodeToString(MovementPreview.serializer(), session.movementPreview(preview))
        }
    }

    addTool(
        name = "research_details",
        description = "Inspect your technology tree on demand. Omit names for a catalogue of prerequisites, costs and legal/blocked choices. Supply up to 16 exact names for desktop effect descriptions and prerequisiteQueue, the engine's ordered path to that technology. researchQueue is your existing queue; SET_RESEARCH_QUEUE replaces it, including current research. Include the existing queue first to preserve it, then append missing entries from prerequisiteQueue. researchNow means the technology can be researched immediately; queueAvailable applies to the entire suggested path. A free choice still requires all prerequisites. Use player_command to act and re-observe afterward.",
        inputSchema = DETAIL_INPUT_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "research_details", request.arguments) {
            PlayerTurnJson.json.encodeToString(ResearchDetails.serializer(), session.researchDetails(request.arguments.detailNames()))
        }
    }

    addTool(
        name = "policy_details",
        description = "Inspect your policy tree on demand, including culture, adoption cost, free choices, branches, prerequisites, era requirements and unavailable reasons. Omit names for a catalogue; supply up to 16 exact names for desktop benefits and branch completion effects. Branch completion policies are automatic. Details do not adopt anything; use ADOPT_POLICY with an available exact name.",
        inputSchema = DETAIL_INPUT_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "policy_details", request.arguments) {
            PlayerTurnJson.json.encodeToString(PolicyDetails.serializer(), session.policyDetails(request.arguments.detailNames()))
        }
    }

    addTool(
        name = "victory_progress",
        description = "Read victory-screen milestones, completion and current progress, your score and turn limit. Includes next milestones only for rivals whose progress the UI permits you to see. Omit names for all displayed victory types, or select up to 16 exact names returned by this query. Time victory uses score and turn limit; it has no UI milestone column. This read does not acknowledge popups or confirm actions.",
        inputSchema = DETAIL_INPUT_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "victory_progress", request.arguments) {
            PlayerTurnJson.json.encodeToString(VictoryProgress.serializer(), session.victoryProgress(request.arguments.detailNames()))
        }
    }

    addTool(
        name = "player_summary",
        description = "Return the authenticated player's current summary.",
        inputSchema = NO_ARGUMENTS_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "player_summary", request.arguments) {
            request.arguments.requireNoArguments()
            PlayerSummaryJson.encode(session.summary())
        }
    }

    addTool(
        name = "owned_cities",
        description = "Return the authenticated player's currently owned cities and opaque handles.",
        inputSchema = NO_ARGUMENTS_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "owned_cities", request.arguments) {
            request.arguments.requireNoArguments()
            PlayerCommandJson.encode(session.cities())
        }
    }

    addTool(
        name = "rename_city",
        description = "Rename an owned city. Repeating the same successful request payload is idempotent.",
        inputSchema = RENAME_CITY_INPUT_SCHEMA,
        toolAnnotations = COMMAND_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "rename_city", request.arguments) {
            val renameRequest = InputSchemas.renameCity.decode(RenameCityRequest.serializer(), request.arguments ?: throw InvalidToolArgumentsException())
            PlayerCommandJson.encode(session.renameCity(renameRequest))
        }
    }

    addTool(
        name = "free_great_person_choice",
        description = "Return the authenticated player's current free Great Person decision, if any.",
        inputSchema = NO_ARGUMENTS_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "free_great_person_choice", request.arguments) {
            request.arguments.requireNoArguments()
            PlayerCommandJson.encode(session.freeGreatPersonChoice())
        }
    }

    addTool(
        name = "choose_free_great_person",
        description = "Choose an offered free Great Person. Repeating the same successful request payload is idempotent.",
        inputSchema = CHOOSE_FREE_GREAT_PERSON_INPUT_SCHEMA,
        toolAnnotations = COMMAND_ANNOTATIONS,
    ) { request ->
        measuredPlayerResult(session, "choose_free_great_person", request.arguments) {
            val choiceRequest = InputSchemas.chooseGreatPerson.decode(ChooseFreeGreatPersonRequest.serializer(), request.arguments ?: throw InvalidToolArgumentsException())
            PlayerCommandJson.encode(session.chooseFreeGreatPerson(choiceRequest))
        }
    }
}

private val QUERY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint = true,
    destructiveHint = false,
    openWorldHint = false,
)

private val COMMAND_ANNOTATIONS = ToolAnnotations(
    readOnlyHint = false,
    idempotentHint = true,
    openWorldHint = false,
)

private val NO_ARGUMENTS_SCHEMA = InputSchemas.empty.toolSchema()
private val DETAIL_INPUT_SCHEMA = InputSchemas.details.toolSchema()
private val TURN_COMMAND_INPUT_SCHEMA = InputSchemas.turnCommand.toolSchema()
private val RENAME_CITY_INPUT_SCHEMA = InputSchemas.renameCity.toolSchema()
private val CHOOSE_FREE_GREAT_PERSON_INPUT_SCHEMA = InputSchemas.chooseGreatPerson.toolSchema()

/** ToolSchema supports properties/required/defs; communication's variant schema is nested
 * so operation-specific required fields and additionalProperties survive SDK serialization. */
internal val COMMUNICATION_INPUT_SCHEMA = ToolSchema(
    properties = buildJsonObject { put("request", JsonObject(InputSchemas.communication.document - "\$schema" - "\$defs")) },
    required = listOf("request"),
    defs = InputSchemas.communication.document["\$defs"]?.jsonObject,
)

private fun InputSchema.toolSchema() = ToolSchema(
    properties = document.getValue("properties").jsonObject,
    required = document.getValue("required").jsonArray.map { it.jsonPrimitive.content },
)

@kotlinx.serialization.Serializable
private data class DetailRequest(val names: List<String> = emptyList())

private fun JsonObject?.detailNames(): List<String> = InputSchemas.details.decode(DetailRequest.serializer(), this ?: buildJsonObject {}).names

private fun JsonObject?.requireNoArguments() {
    InputSchemas.empty.validate(this ?: buildJsonObject {})
}

private inline fun playerResult(operation: () -> String): CallToolResult = try {
    successResult(operation())
} catch (failure: CancellationException) {
    throw failure
} catch (_: InvalidToolArgumentsException) {
    errorResult(code = "INVALID_ARGUMENT", message = "Invalid argument")
} catch (failure: PlayerSessionException) {
    errorResult(
        code = failure.reason.name,
        message = failure.message ?: "Player operation failed",
    )
} catch (_: Throwable) {
    errorResult(code = "INTERNAL_ERROR", message = "Player operation failed")
}

private fun successResult(encoded: String): CallToolResult = CallToolResult(
    structuredContent = Json.parseToJsonElement(encoded).jsonObject,
    content = listOf(TextContent(encoded)),
    isError = false,
)

private fun errorResult(code: String, message: String): CallToolResult {
    val structured = buildJsonObject {
        putJsonObject("error") {
            put("code", code)
            put("message", message)
        }
    }
    return CallToolResult(
        structuredContent = structured,
        content = listOf(TextContent(structured.toString())),
        isError = true,
    )
}

private class InvalidToolArgumentsException : IllegalArgumentException()

/** Count at the authenticated MCP boundary, including rejected calls and uncertain cancellations. */
private suspend inline fun measuredPlayerResult(session: PlayerSession, name: String, arguments: JsonObject?, operation: () -> String): CallToolResult {
    var id: String? = null
    var outcome = "incomplete"
    try {
        return playerResult {
            id = (session as? PlayerApiAccounting)?.beginApiCall(name, arguments)
            val body = operation()
            outcome = if ((runCatching { Json.parseToJsonElement(body) }.getOrNull() as? JsonObject)?.containsKey("error") == true) "error" else "success"
            body
        }.also { if (it.isError == true) outcome = "error" }
    } finally {
        id?.let { (session as? PlayerApiAccounting)?.finishApiCall(it, outcome) }
    }
}
