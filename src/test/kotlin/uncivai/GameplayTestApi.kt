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

/** Gameplay and receipts must be unchanged by retries; operator API-call accounting still advances. */
internal fun gameplayCheckpoint(path: Path): JsonObject {
    val checkpoint = Json.parseToJsonElement(Files.readString(path)).jsonObject
    val communication = checkpoint.getValue("communication").jsonObject
    return JsonObject(checkpoint + ("communication" to JsonObject(communication - "telemetry")))
}

/** Engine fixtures keep their fixed read credentials; end-to-end recovery tests cover rotation. */
internal fun captureTestTurn(runtime: HeadlessRuntime, root: Path): GameCheckpoint {
    val wasPaused = runtime.executor.isPaused()
    runtime.executor.pause()
    runtime.executor.communicationPaused = true
    val saved = runtime.executor.turnSnapshot()
    Files.writeString(root.resolve("turn-boundary.json"), PlayerTurnJson.json.encodeToString(saved))
    runtime.executor.communicationPaused = false
    if (!wasPaused) runtime.executor.resume()
    return saved
}

internal fun testPlayerTokens(root: Path, defaults: Map<String, String>): Map<String, String> {
    val path = root.resolve("test-player-tokens.json")
    return if (Files.exists(path)) PlayerTurnJson.json.decodeFromString(Files.readString(path)) else defaults
}

internal fun restoreTestTurn(operator: (OperatorRequest) -> OperatorResponse, lease: String, root: Path): Map<String, String> {
    val saved = PlayerTurnJson.json.decodeFromString<GameCheckpoint>(Files.readString(root.resolve("turn-boundary.json")))
    val epoch = "restored-" + UUID.randomUUID()
    operator(OperatorRequest("barrier", lease))
    operator(OperatorRequest("restore", leaseId = lease, epoch = epoch, checkpoint = saved))
    val tokens = operator(OperatorRequest("rotate_players", leaseId = lease, epoch = epoch)).players.associate { it.civilizationId to it.token }
    Files.writeString(root.resolve("test-player-tokens.json"), PlayerTurnJson.json.encodeToString(tokens))
    operator(OperatorRequest("restore_complete", leaseId = lease, epoch = epoch))
    operator(OperatorRequest("unbarrier", lease))
    return tokens
}

/** Authenticated HTTPS player and operator client for scripted gameplay fixtures. */
internal class GameplayTestApi(runtime: HeadlessRuntime, tlsRoot: Path, initialTokens: Map<String, String> = mapOf("Rome" to "R".repeat(43), "Greece" to "G".repeat(43))) : AutoCloseable {
    private var tokens = testPlayerTokens(tlsRoot.parent, initialTokens)
    fun restore(root: Path) {
        tokens = restoreTestTurn(::operator, lease, root)
    }
    private val tls = TlsTestFixture(Files.createDirectories(tlsRoot))
    private val client = HttpClient.newBuilder().sslContext(tls.context).connectTimeout(Duration.ofSeconds(5)).build()
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
    fun communicate(token: String, request: JsonObject): JsonObject = call(token, "player_communication", obj("request" to request)).getValue("structuredContent").jsonObject
    fun execute(token: String, request: TurnCommandRequest): TurnCommandReceipt = PlayerTurnJson.json.decodeFromString(tool(token, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject).toString())
    fun reject(token: String, request: TurnCommandRequest, code: String): JsonObject {
        val response = call(token, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject)
        check(response["isError"]!!.jsonPrimitive.boolean && response["structuredContent"]!!.jsonObject["error"]!!.jsonObject["code"]!!.jsonPrimitive.content == code) { "Expected $code, got $response" }
        return response.getValue("structuredContent").jsonObject
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
    fun finishTurn(token: String, heartbeat: Boolean = true) {
        if (heartbeat) operator(OperatorRequest("heartbeat", lease))
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
    private fun call(token: String, name: String, args: JsonObject = JsonObject(emptyMap()), meta: JsonObject? = null): JsonObject {
        val parameters = if (meta == null) obj("name" to name, "arguments" to args) else obj("name" to name, "arguments" to args, "_meta" to meta)
        return Json.parseToJsonElement(raw("/mcp", obj("jsonrpc" to "2.0", "id" to 1, "method" to "tools/call", "params" to parameters).toString(), token)).jsonObject.getValue("result").jsonObject
    }
    fun tool(token: String, name: String, args: JsonObject = JsonObject(emptyMap()), meta: JsonObject? = null): JsonObject {
        val response = call(token, name, args, meta)
        check(!response.getValue("isError").jsonPrimitive.boolean) { response.toString() }
        return response.getValue("structuredContent").jsonObject
    }
    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection).reviewedFor(turn)

    override fun close() {
        server.close()
        client.close()
    }
}
