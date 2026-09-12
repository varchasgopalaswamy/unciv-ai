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
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import uncivai.player.ChooseFreeGreatPersonRequest
import uncivai.player.PlayerCommandJson
import uncivai.player.PlayerFinalState
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSummaryJson
import uncivai.player.PlayerTurnJson
import uncivai.player.RenameCityRequest
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest

internal fun createPlayerToolsServer(session: PlayerSession): Server = Server(
    serverInfo = Implementation(name = "unciv-player", version = "0.1.0"),
    options = ServerOptions(
        capabilities = ServerCapabilities(
            tools = ServerCapabilities.Tools(listChanged = false),
        ),
    ),
).apply {
    addTool(
        name = "player_capabilities",
        description = "Return implemented gameplay operations and explicit coverage and recovery limitations.",
        inputSchema = NO_ARGUMENTS_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        playerResult {
            request.arguments.requireNoArguments()
            PlayerTurnJson.encode(session.capabilities())
        }
    }

    addTool(
        name = "player_turn",
        description = "Return your turn status and turnReview first: confirmed actions and each owned unit's remaining movement and legal destination count. Read this before END_TURN; previousTurnReview records deliberate deferrals. scenario includes exercise instructions and phase. Parse the response and print selected fields to avoid truncation. Controllable units are map.units, with combat.attacks, combat.promotions.choices and combat.orders; map.tiles[].units contains visible summaries only. Also returns decisions, city yields/growth/citizen options and production progress/queues, research progress, policy and Great Person options, explored map and owned units. Informational alerts include content with the full title, paragraphs, quote and acknowledgement text from the desktop popup; technology descriptions are specific to your civilization. Read all content and note useful information before acknowledging. FirstContact also identifies the met civilization. Diplomacy lists known surviving majors, relationship status, remaining peace treaty turns, legal actions and unavailable reasons, declarationConfirmation warnings, and permitted peaceOptions maxima. Incoming offers include the leader message, display text, and give/receive terms from your perspective; read them all before responding. Outgoing offers show your pending proposal and retraction option. Visible tiles include displayed yields; workers include legal job options, prerequisites and work progress. Units have current-turn movement destinations and previews; speculative paths can stop early on discovery. Coordinates are axial x/y. Revision changes only with your observed state or successful commands. Combat previews include attack origins, paths, strength modifiers and damage ranges. Read combatReports and capture alerts in full. Tactical orders and promotions include unavailable reasons. economy shows your finances, maintenance, happiness, resource balances and city connections. cities[].purchases lists gold construction/tile purchases with costs, descriptions and unavailable reasons. map.units[].economy lists upgrades, pillage and disband options. Read their costs and confirmation text before acting. Unsupported decisions block end turn.",
        inputSchema = NO_ARGUMENTS_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        playerResult {
            request.arguments.requireNoArguments()
            PlayerTurnJson.encode(session.turn())
        }
    }

    addTool(
        name = "player_command",
        description = "Execute one command using the revision from player_turn. MOVE_UNIT uses target=unit handle and offered destination; movement may stop early. FOUND_CITY uses settler target and requires confirmBreakPromise=true if settlementPromises is nonempty. START_WORKER_JOB uses unit target and selection=available job name; STOP_WORKER_JOB uses unit target. SET_WORKED_TILE / SET_TILE_LOCKED use city target, an offered development.tiles destination and worked / locked boolean. REMOVE_CONSTRUCTION uses city target and queue index; MOVE_CONSTRUCTION also uses an offered toIndex. Append with QUEUE_CONSTRUCTION then reorder to change production. Research queue is only for SET_RESEARCH_QUEUE. ACKNOWLEDGE_ALERT uses target=an alert handle with canAcknowledge=true and full content already read from player_turn at this revision, including FirstContact; a revision from a command receipt alone is insufficient (OBSERVATION_REQUIRED). This dismisses only that message, including WarDeclaration. DECLARE_WAR uses target=known civilizationId and requires confirmWar=true after reading declarationConfirmation at this revision. PROPOSE_PEACE uses target=known civilizationId; COUNTER_PEACE uses target=incoming offer handle. Both require peaceTerms give/receive lists with exact offered type/name/duration and positive amounts at most the option maximum, and one Peace Treaty per side. ACCEPT_PEACE and REJECT_PEACE use target=incoming offer handle after reading the current message and terms; unsupported requests can only be rejected. RETRACT_PEACE uses target=known civilizationId. Pending outgoing offers do not block END_TURN: the opponent decides on their turn. Read receipt feedback after acceptance. Successful requests replay; restart recovery requires durableRecovery. Re-observe after commands. END_TURN requires canEndTurn=true and reviewId=turnReview.id from a fresh player_turn at this revision. If any review units are idle with legalDestinations>0, act or supply a short deferReason explaining intentional idling. Read the review even if a prior report claimed movement was unavailable. Any mutation invalidates the review. TURN_REVIEW_REQUIRED means re-read the review and provide acknowledgment; this never forces movement. ATTACK uses an owned unit target and the exact offered destination and attackFrom; CITY_ATTACK uses an owned city and offered destination; omit attackFrom (only ATTACK accepts it). Read receipt.combat: discovery can stop movement before an attack. SET_UNIT_ORDER and PROMOTE_UNIT use an owned unit and selection=available option name. RESOLVE_CAPTURED_CITY uses the capture alert handle (including civilian return choices) and selection=available option name after reading the full decision at this revision. MANAGE_CAPTURED_CITY uses an owned city and available captureOptions selection. BUY_CONSTRUCTION uses city target and purchases.constructions selection=name; BUY_TILE uses city target and purchases.tiles destination (no selection). UPGRADE_UNIT uses unit target and economy.upgrades selection=targetName. PILLAGE and DISBAND_UNIT use unit target without selection. Re-read player_turn before these economic commands; read costs and any confirmation text, and set confirmAction=true when confirmation is present. Read receipt.economy for actual gold and unit changes; re-observe afterward. City management commands require a fresh player_turn at this revision. SET_CITY_FOCUS uses city target and management.focusOptions selection=name. SET_AVOID_GROWTH and SET_MANUAL_SPECIALISTS use city target and enabled boolean. ASSIGN_SPECIALIST / UNASSIGN_SPECIALIST use city target and management.specialists selection=name; each changes one citizen. Assignment needs a free citizen: unwork a tile first if necessary. RESET_CITIZENS uses city target and clears tile locks before automatic allocation. SELL_BUILDING uses city target and management.buildingSales selection=name; read the refund, reasons and confirmation, then set confirmAction=true. REASSIGN_CITY_TILE uses receiving city target and an available management.sharedTiles destination; it needs a free citizen and releases the donor city's tile lock. Read receipt.cityManagement for actual allocations, yields, removed buildings and gold changes.",
        inputSchema = TURN_COMMAND_INPUT_SCHEMA,
        toolAnnotations = COMMAND_ANNOTATIONS,
    ) { request ->
        playerResult {
            val arguments = request.arguments ?: throw InvalidToolArgumentsException()
            val command = try {
                PlayerTurnJson.json.decodeFromJsonElement(TurnCommandRequest.serializer(), arguments)
            } catch (_: SerializationException) {
                throw InvalidToolArgumentsException()
            } catch (_: IllegalArgumentException) {
                throw InvalidToolArgumentsException()
            }
            PlayerTurnJson.encode(session.execute(command))
        }
    }

    addTool(
        name = "player_result",
        description = "Read your victory or defeat result and pending combat reports, including after scheduling stops. A null result means no result is recorded for you yet.",
        inputSchema = NO_ARGUMENTS_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        playerResult {
            request.arguments.requireNoArguments()
            PlayerTurnJson.json.encodeToString(PlayerFinalState.serializer(), session.result())
        }
    }

    addTool(
        name = "player_summary",
        description = "Return the authenticated player's current summary.",
        inputSchema = NO_ARGUMENTS_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        playerResult {
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
        playerResult {
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
        playerResult {
            val arguments = request.arguments.requireStringArguments(RENAME_CITY_ARGUMENTS)
            val renameRequest = RenameCityRequest(
                requestId = arguments.getValue("requestId"),
                epoch = arguments.getValue("epoch"),
                cityHandle = arguments.getValue("cityHandle"),
                name = arguments.getValue("name"),
            )
            PlayerCommandJson.encode(session.renameCity(renameRequest))
        }
    }

    addTool(
        name = "free_great_person_choice",
        description = "Return the authenticated player's current free Great Person decision, if any.",
        inputSchema = NO_ARGUMENTS_SCHEMA,
        toolAnnotations = QUERY_ANNOTATIONS,
    ) { request ->
        playerResult {
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
        playerResult {
            val arguments = request.arguments.requireStringArguments(CHOOSE_FREE_GREAT_PERSON_ARGUMENTS)
            val choiceRequest = ChooseFreeGreatPersonRequest(
                requestId = arguments.getValue("requestId"),
                epoch = arguments.getValue("epoch"),
                decisionHandle = arguments.getValue("decisionHandle"),
                unitName = arguments.getValue("unitName"),
            )
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

private val NO_ARGUMENTS_SCHEMA = ToolSchema(
    properties = buildJsonObject {},
    required = emptyList(),
)

private val TURN_COMMAND_INPUT_SCHEMA = ToolSchema(
    properties = buildJsonObject {
        put("requestId", requestIdProperty())
        put("epoch", stringProperty("Epoch returned by player_turn.", minLength = 1))
        put("revision", stringProperty("Opaque player revision returned by player_turn.", minLength = 1))
        put("reviewId", stringProperty("Only END_TURN: required id from the current turnReview after reading player_turn.", minLength = 1, maxLength = 128))
        put("deferReason", stringProperty("Only END_TURN: why you intentionally leave idle units with legal destinations; required if any exist in turnReview.", minLength = 1, maxLength = 500))
        putJsonObject("kind") {
            put("type", "string")
            putJsonArray("enum") { TurnCommandKind.entries.forEach { add(it.name) } }
        }
        put("target", stringProperty("City, alert, incoming offer or owned unit handle; diplomacy partner commands use a known civilizationId from player_turn.", minLength = 1))
        putJsonObject("destination") {
            put("type", "object")
            put("description", "Offered movement or attack destination, city development tile for SET_WORKED_TILE / SET_TILE_LOCKED, or purchases.tiles position for BUY_TILE.")
            putJsonObject("properties") {
                putJsonObject("x") { put("type", "integer") }
                putJsonObject("y") { put("type", "integer") }
            }
            putJsonArray("required") {
                add("x")
                add("y")
            }
            put("additionalProperties", false)
        }
        putJsonObject("attackFrom") {
            put("type", "object")
            put("description", "Only ATTACK: required origin from the exact offered attack preview.")
            putJsonObject("properties") {
                putJsonObject("x") { put("type", "integer") }
                putJsonObject("y") { put("type", "integer") }
            }
            putJsonArray("required") {
                add("x")
                add("y")
            }
            put("additionalProperties", false)
        }
        putJsonObject("confirmBreakPromise") {
            put("type", "boolean")
            put("description", "Explicitly accept listed settlement promises; only for FOUND_CITY.")
        }
        putJsonObject("confirmAction") {
            put("type", "boolean")
            put("description", "For BUY_CONSTRUCTION, BUY_TILE, UPGRADE_UNIT, PILLAGE, DISBAND_UNIT or SELL_BUILDING: confirm the offered confirmation text after reading player_turn; required true when confirmation is non-null.")
        }
        putJsonObject("enabled") {
            put("type", "boolean")
            put("description", "Required only for SET_AVOID_GROWTH and SET_MANUAL_SPECIALISTS: the desired setting.")
        }
        putJsonObject("confirmWar") {
            put("type", "boolean")
            put("description", "Required true for DECLARE_WAR after reading declarationConfirmation; omit otherwise.")
        }
        putJsonObject("peaceTerms") {
            put("type", "object")
            put("description", "Only PROPOSE_PEACE / COUNTER_PEACE. Give and receive are from your perspective, each with exactly one offered Peace Treaty.")
            putJsonObject("properties") {
                for (side in listOf("give", "receive")) {
                    putJsonObject(side) {
                        put("type", "array")
                        put("minItems", 1)
                        put("maxItems", 256)
                        putJsonObject("items") {
                            put("type", "object")
                            putJsonObject("properties") {
                                put("type", stringProperty("Exact offered term type."))
                                put("name", stringProperty("Exact offered term name.", minLength = 1, maxLength = 256))
                                putJsonObject("amount") {
                                    put("type", "integer")
                                    put("minimum", 1)
                                }
                                putJsonObject("duration") { put("type", "integer") }
                            }
                            putJsonArray("required") {
                                add("type")
                                add("name")
                                add("amount")
                                add("duration")
                            }
                            put("additionalProperties", false)
                        }
                    }
                }
            }
            putJsonArray("required") {
                add("give")
                add("receive")
            }
            put("additionalProperties", false)
        }
        put("selection", stringProperty("Chosen option or new city name; omit for commands without a named choice.", minLength = 1))
        putJsonObject("worked") {
            put("type", "boolean")
            put("description", "Assign or unassign an offered city tile; only SET_WORKED_TILE.")
        }
        putJsonObject("locked") {
            put("type", "boolean")
            put("description", "Lock or unlock an offered city tile; only SET_TILE_LOCKED.")
        }
        putJsonObject("index") {
            put("type", "integer")
            put("minimum", 0)
            put("description", "Current construction queue index; only REMOVE_CONSTRUCTION / MOVE_CONSTRUCTION.")
        }
        putJsonObject("toIndex") {
            put("type", "integer")
            put("minimum", 0)
            put("description", "Offered destination queue index; only MOVE_CONSTRUCTION.")
        }
        putJsonObject("queue") {
            put("type", "array")
            put("description", "Research queue in prerequisite order, only for SET_RESEARCH_QUEUE.")
            put("maxItems", 1024)
            putJsonObject("items") { put("type", "string") }
        }
    },
    required = listOf("requestId", "epoch", "revision", "kind"),
)

private val RENAME_CITY_ARGUMENTS = listOf("requestId", "epoch", "cityHandle", "name")
private val RENAME_CITY_INPUT_SCHEMA = stringInputSchema(
    "requestId" to requestIdProperty(),
    "epoch" to stringProperty("Game epoch returned by a player query.", minLength = 1),
    "cityHandle" to stringProperty("Opaque city handle returned by owned_cities.", minLength = 1),
    "name" to stringProperty(
        description = "New city name.",
        minLength = 1,
        maxLength = 32,
        pattern = "^[^\\[\\]{}\"\\\\<>\\r\\n]+$",
    ),
)

private val CHOOSE_FREE_GREAT_PERSON_ARGUMENTS =
    listOf("requestId", "epoch", "decisionHandle", "unitName")
private val CHOOSE_FREE_GREAT_PERSON_INPUT_SCHEMA = stringInputSchema(
    "requestId" to requestIdProperty(),
    "epoch" to stringProperty("Game epoch returned by a player query.", minLength = 1),
    "decisionHandle" to stringProperty(
        "Opaque decision handle returned by free_great_person_choice.",
        minLength = 1,
    ),
    "unitName" to stringProperty("Exact offered unit name.", minLength = 1),
)

private fun requestIdProperty(): JsonObject = stringProperty(
    description = "Printable ASCII request ID used for successful-command replay.",
    minLength = 1,
    maxLength = 128,
    pattern = "^[ -~]+$",
)

private fun stringInputSchema(vararg properties: Pair<String, JsonObject>): ToolSchema = ToolSchema(
    properties = buildJsonObject {
        for ((name, schema) in properties) put(name, schema)
    },
    required = properties.map { it.first },
)

private fun stringProperty(
    description: String,
    minLength: Int? = null,
    maxLength: Int? = null,
    pattern: String? = null,
): JsonObject = buildJsonObject {
    put("type", "string")
    put("description", description)
    if (minLength != null) put("minLength", minLength)
    if (maxLength != null) put("maxLength", maxLength)
    if (pattern != null) put("pattern", pattern)
}

private fun JsonObject?.requireNoArguments() {
    if (this != null && isNotEmpty()) throw InvalidToolArgumentsException()
}

private fun JsonObject?.requireStringArguments(names: List<String>): Map<String, String> {
    if (this == null || keys != names.toSet()) throw InvalidToolArgumentsException()
    return names.associateWith { name ->
        val value = get(name)
        if (value !is JsonPrimitive || !value.isString) throw InvalidToolArgumentsException()
        value.content
    }
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
