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
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import uncivai.player.ChooseFreeGreatPersonRequest
import uncivai.player.PlayerCommandJson
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSummaryJson
import uncivai.player.RenameCityRequest

internal fun createPlayerToolsServer(session: PlayerSession): Server = Server(
    serverInfo = Implementation(name = "unciv-player", version = "0.1.0"),
    options = ServerOptions(
        capabilities = ServerCapabilities(
            tools = ServerCapabilities.Tools(listChanged = false),
        ),
    ),
).apply {
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
