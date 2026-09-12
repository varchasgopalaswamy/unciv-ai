package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.mcp.OperatorMcpBinding
import uncivai.mcp.PlayerMcpBinding
import uncivai.mcp.PlayerMcpServer
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Files
import java.nio.file.Path
import java.time.Duration
import java.util.UUID

/** Authenticated HTTPS player and operator client for scripted gameplay fixtures. */
internal class GameplayTestApi(runtime: HeadlessRuntime, tlsRoot: Path) : AutoCloseable {
    private val tls = TlsTestFixture(Files.createDirectories(tlsRoot))
    private val client = HttpClient.newBuilder().sslContext(tls.context).connectTimeout(Duration.ofSeconds(5)).build()
    private val tokens = mapOf("Rome" to "R".repeat(43), "Greece" to "G".repeat(43))
    private val operatorToken = "O".repeat(43)
    private val server = PlayerMcpServer(tokens.map { (civ, token) -> PlayerMcpBinding(token, PlayerHost(runtime).bindPlayer(civ)) }, 0, OperatorMcpBinding(operatorToken, OperatorControl(runtime.executor)), tls.tls)
    private val base = "https://localhost:${server.start()}"
    val lease = UUID.randomUUID().toString()
    fun playerToken(civ: String) = tokens.getValue(civ)
    fun operator(request: OperatorRequest): OperatorResponse = OperatorControl.json.decodeFromString(raw("/operator", OperatorControl.json.encodeToString(request), operatorToken))
    fun status() = operator(OperatorRequest("status")).status
    fun acquire() = operator(OperatorRequest("acquire", lease))
    fun activate(): OperatorResponse {
        val current = status()
        return operator(OperatorRequest("activate", lease, current.epoch, current.serverInstance, current.handoff, current.activeCivilization, UUID.randomUUID().toString()))
    }
    fun turn(token: String): PlayerTurnSnapshot = PlayerTurnJson.json.decodeFromString(tool(token, "player_turn").toString())
    fun execute(token: String, request: TurnCommandRequest): TurnCommandReceipt = PlayerTurnJson.json.decodeFromString(tool(token, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject).toString())
    fun reject(token: String, request: TurnCommandRequest, code: String) {
        val response = call(token, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject)
        check(response["isError"]!!.jsonPrimitive.boolean && response["structuredContent"]!!.jsonObject["error"]!!.jsonObject["code"]!!.jsonPrimitive.content == code) { "Expected $code, got $response" }
    }
    fun readAlerts(token: String): PlayerTurnSnapshot {
        repeat(30) {
            val turn = turn(token)
            val alert = turn.alerts.firstOrNull { it.canAcknowledge } ?: return turn
            check(alert.content!!.paragraphs.isNotEmpty())
            execute(token, command(turn, TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle))
        }
        error("Alert budget exhausted")
    }
    fun finishTurn(token: String) {
        operator(OperatorRequest("heartbeat", lease))
        repeat(40) {
            val turn = readAlerts(token)
            val city = turn.cities.firstOrNull { !it.puppet && it.constructionQueue.isEmpty() }
            val command = when {
                turn.freeTechnologies > 0 -> command(turn, TurnCommandKind.CHOOSE_FREE_TECHNOLOGY, selection = turn.researchOptions.first())
                turn.freeGreatPeople > 0 -> command(turn, TurnCommandKind.CHOOSE_FREE_GREAT_PERSON, selection = turn.greatPersonOptions.first())
                turn.researchQueue.isEmpty() -> command(turn, TurnCommandKind.SET_RESEARCH_QUEUE).copy(queue = listOf(turn.researchOptions.first()))
                city != null -> command(turn, TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, city.constructionOptions.first())
                turn.pending.any { it.kind == "Policy" } -> command(turn, TurnCommandKind.ADOPT_POLICY, selection = turn.policyOptions.first())
                turn.canEndTurn -> command(turn, TurnCommandKind.END_TURN)
                else -> error("Unsupported fixture decision: ${turn.pending}")
            }
            val receipt = execute(token, command)
            if (command.kind == TurnCommandKind.END_TURN) {
                check(execute(token, command) == receipt)
                return
            }
        }
        error("Turn budget exhausted")
    }
    private fun raw(path: String, body: String, token: String): String {
        val response = client.send(
            HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofSeconds(20))
                .header("Authorization", "Bearer $token").header("Content-Type", "application/json").header("Accept", "application/json, text/event-stream")
                .POST(HttpRequest.BodyPublishers.ofString(body)).build(),
            HttpResponse.BodyHandlers.ofString(),
        )
        check(response.statusCode() == 200) { "HTTP ${response.statusCode()}: ${response.body()}" }
        return response.body()
    }
    private fun call(token: String, name: String, args: JsonObject = JsonObject(emptyMap())) = Json.parseToJsonElement(raw("/mcp", """{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"$name","arguments":$args}}""", token)).jsonObject.getValue("result").jsonObject
    fun tool(token: String, name: String, args: JsonObject = JsonObject(emptyMap())): JsonObject {
        val response = call(token, name, args)
        check(!response.getValue("isError").jsonPrimitive.boolean) { response.toString() }
        return response.getValue("structuredContent").jsonObject
    }
    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection).reviewedFor(turn)

    override fun close() {
        server.close()
        client.close()
    }
}
