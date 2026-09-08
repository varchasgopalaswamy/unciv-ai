package uncivai

import com.badlogic.gdx.Gdx
import com.unciv.json.json
import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.Civilization
import io.ktor.client.engine.cio.CIO
import io.ktor.client.plugins.sse.SSE
import io.ktor.client.request.header
import io.ktor.http.HttpHeaders
import io.modelcontextprotocol.kotlin.sdk.ExperimentalMcpApi
import io.modelcontextprotocol.kotlin.sdk.client.Client
import io.modelcontextprotocol.kotlin.sdk.client.ClientOptions
import io.modelcontextprotocol.kotlin.sdk.client.StreamableHttpClientTransport
import io.modelcontextprotocol.kotlin.sdk.types.CallToolResult
import io.modelcontextprotocol.kotlin.sdk.types.ClientCapabilities
import io.modelcontextprotocol.kotlin.sdk.types.Implementation
import io.modelcontextprotocol.kotlin.sdk.types.TextContent
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.mcp.PlayerMcpBinding
import uncivai.mcp.PlayerMcpServer
import uncivai.player.ChooseFreeGreatPersonRequest
import uncivai.player.FreeGreatPersonSnapshot
import uncivai.player.OwnedCitiesSnapshot
import uncivai.player.PlayerCommandJson
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSummary
import uncivai.player.PlayerSummaryJson
import uncivai.player.RenameCityRequest
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.Socket
import java.net.URI
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Path
import java.time.Duration
import io.ktor.client.HttpClient as KtorHttpClient

/** Verifies the player binding through the real stateless Streamable HTTP transport. */
@OptIn(ExperimentalMcpApi::class)
object S06McpProbe {
    private const val ROME_TOKEN = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    private const val GREECE_TOKEN = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    private const val FAILURE_TOKEN = "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    private const val HALT_TOKEN = "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"

    @JvmStatic
    fun main(args: Array<String>) = runBlocking(Dispatchers.IO) {
        require(args.size == 2) { "Usage: S06McpProbe <absolute-save> <absolute-data-directory>" }
        val save = Path.of(args[0])
        val data = Path.of(args[1])
        require(save.isAbsolute && data.isAbsolute) { "Probe paths must be absolute" }
        check(Gdx.app == null && Gdx.graphics == null && Gdx.gl == null)

        verifyBoundTransport(save, data.resolve("bound-transport"))
        verifyTurnTransport(save, data.resolve("turn-transport"))
        verifyHaltedRuntime(save, data.resolve("halted-runtime"))

        check(Gdx.app == null && Gdx.graphics == null && Gdx.gl == null)
        println("S06-VERIFIED:${save.fileName}")
    }

    private suspend fun verifyTurnTransport(save: Path, data: Path) {
        HeadlessRuntime.load(save, data).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("Rome")
            PlayerMcpServer(
                listOf(
                    PlayerMcpBinding(ROME_TOKEN, session),
                    PlayerMcpBinding(GREECE_TOKEN, PlayerHost(runtime).bindPlayer("Greece")),
                ),
                port = 0,
            ).use { server ->
                val url = "http://127.0.0.1:${server.start()}/mcp"
                var connection = connect(url, ROME_TOKEN, "turn-client")
                val greek = connect(url, GREECE_TOKEN, "greek-turn-client")
                try {
                    val capabilities = success(connection.client.callTool("player_capabilities", emptyMap()))
                    check(!capabilities.boolean("completeGameplay"))
                    val snapshot = success(connection.client.callTool("player_turn", emptyMap()))
                    check(snapshot == parse(uncivai.player.PlayerTurnJson.encode(session.turn())))
                    toolError(
                        connection.client.callTool("player_turn", mapOf("viewer" to "Greece")),
                        "INVALID_ARGUMENT",
                        "Invalid argument",
                    )
                    val arguments = mapOf(
                        "requestId" to "mcp-turn-rename",
                        "epoch" to snapshot.string("epoch"),
                        "revision" to snapshot.string("revision"),
                        "kind" to "RENAME_CITY",
                        "target" to snapshot.array("cities").single().jsonObject.string("handle"),
                        "selection" to "MCP Roma",
                    )
                    toolError(
                        connection.client.callTool("player_command", arguments + ("viewer" to "Greece")),
                        "INVALID_ARGUMENT",
                        "Invalid argument",
                    )
                    toolError(
                        connection.client.callTool("player_command", arguments + ("kind" to "RAW_ENGINE_CALL")),
                        "INVALID_ARGUMENT",
                        "Invalid argument",
                    )
                    toolError(
                        greek.client.callTool("player_command", arguments),
                        "NOT_YOUR_TURN",
                        "It is not your turn",
                    )
                    val receipt = success(connection.client.callTool("player_command", arguments))
                    check(runtime.game.getCivilization("Rome").cities.single().name == "MCP Roma")
                    connection.close()
                    connection = connect(url, ROME_TOKEN, "reconnected-turn-client")
                    check(success(connection.client.callTool("player_command", arguments)) == receipt)
                    toolError(
                        connection.client.callTool("player_command", arguments + ("selection" to "Conflict")),
                        "REQUEST_ID_CONFLICT",
                        "Request ID conflicts with an earlier request",
                    )
                    toolError(
                        connection.client.callTool("player_command", arguments + ("requestId" to "stale-turn-rename")),
                        "STALE_REVISION",
                        "Player observation is stale",
                    )
                } finally {
                    closeConnection(connection)
                    closeConnection(greek)
                }
            }
        }
    }

    private suspend fun verifyBoundTransport(save: Path, data: Path) {
        val runtime = HeadlessRuntime.load(save, data)
        val game = runtime.game
        S01ScenarioState.verify(game)
        val rome = game.getCivilization("Rome")
        val greece = game.getCivilization("Greece")
        check(!rome.knows(greece))
        val romeSession = PlayerHost(runtime).bindPlayer("Rome")
        val greeceSession = PlayerHost(runtime).bindPlayer("Greece")
        val server = PlayerMcpServer(
            listOf(
                PlayerMcpBinding(ROME_TOKEN, romeSession),
                PlayerMcpBinding(GREECE_TOKEN, greeceSession),
                PlayerMcpBinding(FAILURE_TOKEN, ThrowingPlayerSession),
            ),
            port = 0,
        )
        val port = server.start()
        check(port > 0 && server.start() == port)
        val url = "http://127.0.0.1:$port/mcp"
        var romeClient: McpConnection? = null
        var greeceClient: McpConnection? = null
        var failureClient: McpConnection? = null
        try {
            var rc = connect(url, ROME_TOKEN, "rome-client")
            romeClient = rc
            val gc = connect(url, GREECE_TOKEN, "greece-client")
            greeceClient = gc
            check(rc.transport.sessionId == null && gc.transport.sessionId == null)
            verifyToolSchemas(rc.client)

            val romeSummary = success(rc.client.callTool("player_summary", emptyMap()))
            val greeceSummary = success(gc.client.callTool("player_summary", emptyMap()))
            check(romeSummary.string("civilization") == "Rome")
            check(romeSummary.boolean("isYourTurn"))
            check(greeceSummary.string("civilization") == "Greece")
            check(!greeceSummary.boolean("isYourTurn"))
            check(romeSummary == parse(PlayerSummaryJson.encode(romeSession.summary())))
            check(greeceSummary == parse(PlayerSummaryJson.encode(greeceSession.summary())))

            val romeCities = success(rc.client.callTool("owned_cities", emptyMap()))
            val greeceCities = success(gc.client.callTool("owned_cities", emptyMap()))
            val romeCityHandle = romeCities.array("cities").single().jsonObject.string("handle")
            val greeceCityHandle = greeceCities.array("cities").single().jsonObject.string("handle")
            check(romeCityHandle != greeceCityHandle)
            check(romeCities == parse(PlayerCommandJson.encode(romeSession.cities())))
            check(greeceCities == parse(PlayerCommandJson.encode(greeceSession.cities())))

            val romeChoice = success(rc.client.callTool("free_great_person_choice", emptyMap()))
            val greeceChoice = success(gc.client.callTool("free_great_person_choice", emptyMap()))
            val romeDecisionHandle = romeChoice.decision().string("handle")
            val greeceDecisionHandle = greeceChoice.decision().string("handle")
            check(romeDecisionHandle != greeceDecisionHandle)
            check("Great Scientist" in romeChoice.decision().strings("options"))
            check("Great Scientist" in greeceChoice.decision().strings("options"))
            check(romeChoice == parse(PlayerCommandJson.encode(romeSession.freeGreatPersonChoice())))
            check(greeceChoice == parse(PlayerCommandJson.encode(greeceSession.freeGreatPersonChoice())))

            verifyHiddenStateDoesNotCrossBinding(game, greece, rc.client, romeSummary, romeCities, romeChoice)

            toolError(
                rc.client.callTool(
                    "rename_city",
                    renameArguments("cross-city", romeCities.string("epoch"), greeceCityHandle, "No Access"),
                ),
                "CITY_UNAVAILABLE",
                "City is unavailable",
            )
            toolError(
                rc.client.callTool(
                    "choose_free_great_person",
                    choiceArguments(
                        "cross-decision",
                        romeChoice.string("epoch"),
                        greeceDecisionHandle,
                        "Great Scientist",
                    ),
                ),
                "DECISION_UNAVAILABLE",
                "Decision is unavailable",
            )

            val greeceOutOfTurnArguments = choiceArguments(
                "same-id-across-bindings",
                greeceChoice.string("epoch"),
                greeceDecisionHandle,
                "Great Scientist",
            )
            toolError(
                gc.client.callTool("choose_free_great_person", greeceOutOfTurnArguments),
                "NOT_YOUR_TURN",
                "It is not your turn",
            )

            val renameA = renameArguments(
                "same-id-across-bindings",
                romeCities.string("epoch"),
                romeCityHandle,
                "Nova Roma",
            )
            val receiptA = success(rc.client.callTool("rename_city", renameA))
            check(receiptA.keys == setOf("requestId", "epoch", "cityHandle", "previousName", "name"))
            check(rome.cities.single().name == "Nova Roma")
            rc.close()
            rc = connect(url, ROME_TOKEN, "rome-reconnected")
            romeClient = rc
            check(rc.transport.sessionId == null)
            val reconnectedCities = success(rc.client.callTool("owned_cities", emptyMap()))
            check(reconnectedCities.array("cities").single().jsonObject.string("handle") == romeCityHandle)

            val renameB = renameArguments("rename-b", romeCities.string("epoch"), romeCityHandle, "New Rome")
            success(rc.client.callTool("rename_city", renameB))
            check(rome.cities.single().name == "New Rome")
            check(success(rc.client.callTool("rename_city", renameA)) == receiptA)
            check(rome.cities.single().name == "New Rome")
            toolError(
                rc.client.callTool(
                    "choose_free_great_person",
                    choiceArguments(
                        "same-id-across-bindings",
                        romeChoice.string("epoch"),
                        romeDecisionHandle,
                        "Great Scientist",
                    ),
                ),
                "REQUEST_ID_CONFLICT",
                "Request ID conflicts with an earlier request",
            )

            val romeUnitIds = rome.units.getCivUnits().map { it.id }.toSet()
            val duplicateArguments = choiceArguments(
                "concurrent-choice",
                romeChoice.string("epoch"),
                romeDecisionHandle,
                "Great Scientist",
            )
            val duplicateResults = coroutineScope {
                listOf(
                    async { rc.client.callTool("choose_free_great_person", duplicateArguments) },
                    async { rc.client.callTool("choose_free_great_person", duplicateArguments) },
                ).map { it.await() }
            }
            val duplicateReceipts = duplicateResults.map(::success)
            check(duplicateReceipts.distinct().size == 1)
            check(
                duplicateReceipts.first().keys ==
                    setOf("requestId", "epoch", "decisionHandle", "unitName", "remainingChoices"),
            )
            check(rome.greatPeople.freeGreatPeople == 0)
            checkAddedUnit(rome, romeUnitIds, "Great Scientist")

            rc.close()
            rc = connect(url, ROME_TOKEN, "rome-second-reconnect")
            romeClient = rc
            check(success(rc.client.callTool("choose_free_great_person", duplicateArguments)) == duplicateReceipts.first())
            check(rome.greatPeople.freeGreatPeople == 0)
            toolError(
                rc.client.callTool(
                    "rename_city",
                    renameArguments(
                        "concurrent-choice",
                        romeCities.string("epoch"),
                        romeCityHandle,
                        "Cross Kind Conflict",
                    ),
                ),
                "REQUEST_ID_CONFLICT",
                "Request ID conflicts with an earlier request",
            )
            toolError(
                rc.client.callTool(
                    "choose_free_great_person",
                    duplicateArguments + ("unitName" to "Great Engineer"),
                ),
                "REQUEST_ID_CONFLICT",
                "Request ID conflicts with an earlier request",
            )

            setCurrentPlayer(game, greece)
            check(
                success(
                    rc.client.callTool("choose_free_great_person", duplicateArguments),
                ) == duplicateReceipts.first(),
            ) { "A historical receipt was lost after the turn changed" }
            toolError(
                gc.client.callTool(
                    "rename_city",
                    renameArguments("foreign-city", greeceCities.string("epoch"), romeCityHandle, "No Access"),
                ),
                "CITY_UNAVAILABLE",
                "City is unavailable",
            )
            toolError(
                gc.client.callTool(
                    "choose_free_great_person",
                    choiceArguments(
                        "foreign-decision",
                        greeceChoice.string("epoch"),
                        romeDecisionHandle,
                        "Great Scientist",
                    ),
                ),
                "DECISION_UNAVAILABLE",
                "Decision is unavailable",
            )
            val refreshedGreeceChoice = success(
                gc.client.callTool("free_great_person_choice", emptyMap()),
            )
            val greeceRetry = greeceOutOfTurnArguments + (
                "decisionHandle" to refreshedGreeceChoice.decision().string("handle")
                )
            val greeceUnitIds = greece.units.getCivUnits().map { it.id }.toSet()
            val greeceReceipt = success(
                gc.client.callTool("choose_free_great_person", greeceRetry),
            )
            check(greeceReceipt.string("requestId") == "same-id-across-bindings")
            check(greece.greatPeople.freeGreatPeople == 1)
            checkAddedUnit(greece, greeceUnitIds, "Great Scientist")

            verifyRawHttpBoundary(url, port)

            val fc = connect(url, FAILURE_TOKEN, "throwing-client")
            failureClient = fc
            val internalError = toolError(
                fc.client.callTool("player_summary", emptyMap()),
                "INTERNAL_ERROR",
                "Player operation failed",
            )
            check("private adapter failure" !in internalError.toString())
            val assertionError = toolError(
                fc.client.callTool("owned_cities", emptyMap()),
                "INTERNAL_ERROR",
                "Player operation failed",
            )
            check("private adapter assertion" !in assertionError.toString())

            runtime.close()
            toolError(
                rc.client.callTool("player_summary", emptyMap()),
                "SESSION_CLOSED",
                "Player session is closed",
            )
        } finally {
            closeConnection(failureClient)
            closeConnection(greeceClient)
            closeConnection(romeClient)
            server.close()
            server.close()
            runtime.close()
        }
    }

    private suspend fun verifyToolSchemas(client: Client) {
        val listed = client.listTools()
        val tools = listed.tools.associateBy { it.name }
        val expectedInputs = linkedMapOf(
            "player_capabilities" to emptySet(),
            "player_turn" to emptySet(),
            "player_result" to emptySet(),
            "player_command" to setOf("requestId", "epoch", "revision", "reviewId", "deferReason", "kind", "target", "selection", "queue", "destination", "attackFrom", "confirmBreakPromise", "confirmWar", "peaceTerms", "worked", "locked", "index", "toIndex"),
            "player_summary" to emptySet(),
            "owned_cities" to emptySet(),
            "rename_city" to setOf("requestId", "epoch", "cityHandle", "name"),
            "free_great_person_choice" to emptySet(),
            "choose_free_great_person" to setOf("requestId", "epoch", "decisionHandle", "unitName"),
        )
        check(tools.keys == expectedInputs.keys) { "Unexpected MCP tools: ${tools.keys}" }
        check(listed.nextCursor == null)
        for ((name, expectedProperties) in expectedInputs) {
            val tool = tools.getValue(name)
            val properties = tool.inputSchema.properties.orEmpty()
            check(properties.keys == expectedProperties) { "$name input properties were ${properties.keys}" }
            if (name == "player_command") {
                check(tool.inputSchema.required.orEmpty().toSet() == setOf("requestId", "epoch", "revision", "kind"))
                check(properties.getValue("queue").jsonObject.string("type") == "array")
                for ((field, maximum) in mapOf("reviewId" to "128", "deferReason" to "500")) {
                    val schema = properties.getValue(field).jsonObject
                    check(schema.string("type") == "string")
                    check(schema.getValue("minLength").jsonPrimitive.content == "1")
                    check(schema.getValue("maxLength").jsonPrimitive.content == maximum)
                }
            } else {
                check(tool.inputSchema.required.orEmpty().toSet() == expectedProperties)
                check(properties.values.all { schema -> schema.jsonObject.string("type") == "string" })
            }
            check(tool.outputSchema == null) {
                "$name advertised an output schema that cannot also describe tool errors"
            }
        }
        val capabilities = checkNotNull(client.serverCapabilities)
        check(capabilities.tools != null)
        check(capabilities.resources == null)
        check(capabilities.prompts == null)
        check(capabilities.logging == null)
        check(capabilities.completions == null)
    }

    private suspend fun verifyHiddenStateDoesNotCrossBinding(
        game: GameInfo,
        hiddenCivilization: Civilization,
        client: Client,
        expectedSummary: JsonObject,
        expectedCities: JsonObject,
        expectedChoice: JsonObject,
    ) {
        val hiddenName = "Unseen ${java.util.UUID.randomUUID()}"
        hiddenCivilization.cities.single().name = hiddenName
        hiddenCivilization.addGold(734)
        hiddenCivilization.greatPeople.longCountGPPool.add("hidden-${java.util.UUID.randomUUID()}")
        val changedWorld = json().toJson(game)

        check(success(client.callTool("player_summary", emptyMap())) == expectedSummary)
        check(success(client.callTool("owned_cities", emptyMap())) == expectedCities)
        check(success(client.callTool("free_great_person_choice", emptyMap())) == expectedChoice)
        check(json().toJson(game) == changedWorld)
        check(hiddenName !in expectedSummary.toString())
        check(hiddenName !in expectedCities.toString())
        check(hiddenName !in expectedChoice.toString())
    }

    private suspend fun verifyRawHttpBoundary(url: String, port: Int) {
        val queryBody = rpcToolCall("player_summary", "null")
        val missingAuth = rawRequest("POST", url, body = queryBody)
        val wrongAuth = rawRequest("POST", url, token = "wrong-token", body = queryBody)
        check(missingAuth.status == 401 && wrongAuth.status == 401)
        check(missingAuth.body == wrongAuth.body)
        check(missingAuth.body == "Unauthorized")
        check(ROME_TOKEN !in missingAuth.body && GREECE_TOKEN !in missingAuth.body)

        check(rawRequest("GET", url).status == 401) { "Authentication did not precede method handling" }
        check(rawRequest("GET", url, token = ROME_TOKEN).status == 405)
        check(rawRequest("DELETE", url, token = ROME_TOKEN).status == 405)
        check(
            rawRequest(
                "POST",
                url,
                token = ROME_TOKEN,
                body = queryBody,
                headers = mapOf("Origin" to "http://127.0.0.1:$port"),
            ).status == 403,
        ) { "An Origin header was accepted" }
        check(
            rawRequest(
                "POST",
                url,
                token = ROME_TOKEN,
                body = queryBody,
                headers = mapOf("Mcp-Session-Id" to "stateful-client"),
            ).status == 400,
        ) { "A session ID was accepted by the stateless endpoint" }
        check(rawHostRequest(port, "attacker.example", ROME_TOKEN, queryBody) == 403)
        check(rawHostRequest(port, "localhost:$port", ROME_TOKEN, queryBody) == 200)

        val nullArguments = rawRequest("POST", url, ROME_TOKEN, queryBody)
        check(nullArguments.status == 200)
        check(!rawToolResult(nullArguments).boolean("isError"))

        val malformedCalls = listOf(
            rpcToolCall("player_summary", "{\"viewer\":\"Greece\"}"),
            rpcToolCall("rename_city", "{}"),
            rpcToolCall(
                "rename_city",
                "{\"requestId\":\"raw\",\"epoch\":null,\"cityHandle\":\"h\",\"name\":\"n\"}",
            ),
            rpcToolCall(
                "rename_city",
                "{\"requestId\":\"raw\",\"epoch\":\"e\",\"cityHandle\":3,\"name\":\"n\"}",
            ),
            rpcToolCall(
                "choose_free_great_person",
                "{\"requestId\":\"raw\",\"epoch\":\"e\",\"decisionHandle\":\"h\",\"unitName\":{}}",
            ),
            rpcToolCall(
                "choose_free_great_person",
                "{\"requestId\":\"raw\",\"epoch\":\"e\",\"decisionHandle\":\"h\",\"unitName\":\"Great Scientist\",\"extra\":true}",
            ),
        )
        for (body in malformedCalls) {
            val response = rawRequest("POST", url, ROME_TOKEN, body)
            check(response.status == 200) { "Malformed tool arguments returned HTTP ${response.status}" }
            rawToolError(response, "INVALID_ARGUMENT", "Invalid argument")
        }
        for (body in listOf(rpcToolCall("owned_cities", "[]"), rpcToolCall("free_great_person_choice", "7"))) {
            val response = rawRequest("POST", url, ROME_TOKEN, body)
            check(response.status == 200)
            val root = Json.parseToJsonElement(response.body).jsonObject
            check("error" in root && "result" !in root) {
                "A non-object MCP arguments value reached a tool handler"
            }
        }

        val oversized = rawRequest(
            "POST",
            url,
            token = ROME_TOKEN,
            body = "x".repeat(4 * 1024 * 1024 + 1),
        )
        check(oversized.status == 413) { "Oversized body returned ${oversized.status}" }
    }

    private suspend fun verifyHaltedRuntime(save: Path, data: Path) {
        val runtime = HeadlessRuntime.load(save, data)
        val game = runtime.game
        val rome = game.getCivilization("Rome")
        val injected = IllegalStateException("private failure after placement")
        val session = PlayerHost(
            runtime,
            { city, name -> city.name = name },
            { civilization, unitName ->
                checkNotNull(civilization.greatPeople.chooseFreeGreatPerson(unitName))
                throw injected
            },
        ).bindPlayer("Rome")
        val server = PlayerMcpServer(listOf(PlayerMcpBinding(HALT_TOKEN, session)), port = 0)
        val port = server.start()
        val connection = connect("http://127.0.0.1:$port/mcp", HALT_TOKEN, "halt-client")
        try {
            val choice = success(connection.client.callTool("free_great_person_choice", emptyMap()))
            val unitIds = rome.units.getCivUnits().map { it.id }.toSet()
            val failure = toolError(
                connection.client.callTool(
                    "choose_free_great_person",
                    choiceArguments(
                        "halt-after-placement",
                        choice.string("epoch"),
                        choice.decision().string("handle"),
                        "Great Scientist",
                    ),
                ),
                "GAME_HALTED",
                "Game is halted",
            )
            check(runtime.mutationFailure === injected)
            check(rome.units.getCivUnits().any { it.id !in unitIds })
            check("private failure" !in failure.toString())
            toolError(
                connection.client.callTool("owned_cities", emptyMap()),
                "GAME_HALTED",
                "Game is halted",
            )
        } finally {
            connection.close()
            server.close()
            runtime.close()
        }
    }

    private suspend fun connect(url: String, token: String, name: String): McpConnection {
        val http = KtorHttpClient(CIO) { install(SSE) }
        val transport = StreamableHttpClientTransport(
            client = http,
            url = url,
            requestBuilder = {
                header(HttpHeaders.Authorization, "Bearer $token")
            },
        )
        val client = Client(
            clientInfo = Implementation(name = name, version = "1.0"),
            options = ClientOptions(capabilities = ClientCapabilities()),
        )
        try {
            client.connect(transport)
        } catch (failure: Throwable) {
            http.close()
            throw failure
        }
        return McpConnection(client, transport, http)
    }

    private suspend fun closeConnection(connection: McpConnection?) {
        if (connection == null) return
        runCatching { connection.close() }
    }

    private fun success(result: CallToolResult): JsonObject {
        check(result.isError == false) { "Expected tool success: $result" }
        val structured = checkNotNull(result.structuredContent)
        val text = result.content.filterIsInstance<TextContent>().single().text
        check(Json.parseToJsonElement(text) == structured) {
            "Text and structured tool results differed"
        }
        return structured
    }

    private fun toolError(
        result: CallToolResult,
        code: String,
        message: String,
    ): JsonObject {
        check(result.isError == true) { "Expected tool error: $result" }
        val structured = checkNotNull(result.structuredContent)
        check(structured.keys == setOf("error"))
        val error = structured.getValue("error").jsonObject
        check(error.keys == setOf("code", "message"))
        check(error.string("code") == code) { "Expected $code, got $error" }
        check(error.string("message") == message) { "Expected $message, got $error" }
        val text = result.content.filterIsInstance<TextContent>().single().text
        check(Json.parseToJsonElement(text) == structured)
        return structured
    }

    private fun renameArguments(
        requestId: String,
        epoch: String,
        cityHandle: String,
        name: String,
    ): Map<String, Any?> = mapOf(
        "requestId" to requestId,
        "epoch" to epoch,
        "cityHandle" to cityHandle,
        "name" to name,
    )

    private fun choiceArguments(
        requestId: String,
        epoch: String,
        decisionHandle: String,
        unitName: String,
    ): Map<String, Any?> = mapOf(
        "requestId" to requestId,
        "epoch" to epoch,
        "decisionHandle" to decisionHandle,
        "unitName" to unitName,
    )

    private fun checkAddedUnit(
        civilization: Civilization,
        previousIds: Set<Int>,
        expectedName: String,
    ) {
        val added = civilization.units.getCivUnits().filter { it.id !in previousIds }.toList()
        check(added.size == 1)
        check(added.single().name == expectedName)
        check(added.single().civ === civilization)
        check(added.single().getTile().getUnits().any { it === added.single() })
    }

    private fun setCurrentPlayer(game: GameInfo, civilization: Civilization) {
        game.currentPlayer = civilization.civID
        game.currentPlayerCiv = civilization
    }

    private fun rpcToolCall(name: String, argumentsJson: String): String = """{"jsonrpc":"2.0","id":71,"method":"tools/call","params":{"name":"$name","arguments":$argumentsJson}}"""

    private fun rawRequest(
        method: String,
        url: String,
        token: String? = null,
        body: String = "",
        headers: Map<String, String> = emptyMap(),
    ): RawResponse {
        val builder = HttpRequest.newBuilder(URI(url))
            .timeout(Duration.ofSeconds(15))
            .header("Accept", "application/json, text/event-stream")
            .header("Content-Type", "application/json")
            .header("MCP-Protocol-Version", "2025-06-18")
        if (token != null) builder.header("Authorization", "Bearer $token")
        for ((name, value) in headers) builder.header(name, value)
        val publisher = if (method == "GET" || method == "DELETE") {
            HttpRequest.BodyPublishers.noBody()
        } else {
            HttpRequest.BodyPublishers.ofString(body)
        }
        val response = rawHttpClient.send(
            builder.method(method, publisher).build(),
            HttpResponse.BodyHandlers.ofString(),
        )
        return RawResponse(response.statusCode(), response.body())
    }

    private fun rawHostRequest(port: Int, host: String, token: String, body: String): Int {
        Socket("127.0.0.1", port).use { socket ->
            socket.soTimeout = 15_000
            val writer = OutputStreamWriter(socket.getOutputStream(), Charsets.US_ASCII)
            writer.write("POST /mcp HTTP/1.1\r\n")
            writer.write("Host: $host\r\n")
            writer.write("Authorization: Bearer $token\r\n")
            writer.write("Content-Type: application/json\r\n")
            writer.write("Accept: application/json, text/event-stream\r\n")
            writer.write("MCP-Protocol-Version: 2025-06-18\r\n")
            writer.write("Content-Length: ${body.toByteArray(Charsets.US_ASCII).size}\r\n")
            writer.write("Connection: close\r\n\r\n")
            writer.write(body)
            writer.flush()
            val statusLine = BufferedReader(
                InputStreamReader(socket.getInputStream(), Charsets.US_ASCII),
            ).readLine()
            return statusLine.split(' ')[1].toInt()
        }
    }

    private fun rawToolResult(response: RawResponse): JsonObject {
        val root = Json.parseToJsonElement(response.body).jsonObject
        return root.getValue("result").jsonObject
    }

    private fun rawToolError(response: RawResponse, code: String, message: String) {
        val result = rawToolResult(response)
        check(result.boolean("isError"))
        val structured = result.getValue("structuredContent").jsonObject
        val error = structured.getValue("error").jsonObject
        check(error.string("code") == code && error.string("message") == message) {
            "Unexpected raw tool error: $error"
        }
        val text = result.getValue("content").jsonArray.single().jsonObject.string("text")
        check(Json.parseToJsonElement(text) == structured)
    }

    private fun parse(encoded: String): JsonObject = Json.parseToJsonElement(encoded).jsonObject

    private fun JsonObject.string(name: String): String = getValue(name).jsonPrimitive.content

    private fun JsonObject.boolean(name: String): Boolean = getValue(name).jsonPrimitive.boolean

    private fun JsonObject.array(name: String) = getValue(name).jsonArray

    private fun JsonObject.strings(name: String): List<String> = array(name).map { it.jsonPrimitive.content }

    private fun JsonObject.decision(): JsonObject {
        val value = getValue("decision")
        check(value !== JsonNull)
        return value.jsonObject
    }

    private data class RawResponse(val status: Int, val body: String)

    private data class McpConnection(
        val client: Client,
        val transport: StreamableHttpClientTransport,
        val http: KtorHttpClient,
    ) {
        suspend fun close() {
            try {
                client.close()
            } finally {
                http.close()
            }
        }
    }

    private object ThrowingPlayerSession : PlayerSession {
        private fun fail(): Nothing = error("private adapter failure")

        override fun capabilities(): uncivai.player.PlayerCapabilities = fail()

        override fun turn(): uncivai.player.PlayerTurnSnapshot = fail()
        override fun result(): uncivai.player.PlayerFinalState = fail()

        override fun completedReceipt(request: uncivai.player.TurnCommandRequest): uncivai.player.TurnCommandReceipt? = fail()

        override fun execute(request: uncivai.player.TurnCommandRequest): uncivai.player.TurnCommandReceipt = fail()

        override fun summary(): PlayerSummary = fail()

        override fun cities(): OwnedCitiesSnapshot = throw AssertionError("private adapter assertion")

        override fun renameCity(request: RenameCityRequest) = fail()

        override fun freeGreatPersonChoice(): FreeGreatPersonSnapshot = fail()

        override fun chooseFreeGreatPerson(request: ChooseFreeGreatPersonRequest) = fail()
    }

    private val rawHttpClient = java.net.http.HttpClient.newBuilder()
        .connectTimeout(Duration.ofSeconds(10))
        .build()
}
