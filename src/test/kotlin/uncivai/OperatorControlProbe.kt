package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
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
import uncivai.player.TurnCommandRequest
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Path
import java.time.Duration
import java.util.UUID

object OperatorControlProbe {
    private val operatorToken = "O".repeat(43)
    private val romeToken = "R".repeat(43)
    private val greeceToken = "G".repeat(43)
    private lateinit var client: HttpClient
    private lateinit var base: String

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val tls = TlsTestFixture(root)
        client = HttpClient.newBuilder().sslContext(tls.context).connectTimeout(Duration.ofSeconds(5)).build()
        val checkpoint = root.resolve("checkpoint.json")
        var clock = 100L
        lateinit var beforeRestart: OperatorStatus
        lateinit var revokedToken: String
        HeadlessRuntime.createDuel(root.resolve("initial"), 412L).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            val control = OperatorControl(runtime.executor) { clock }
            val host = PlayerHost(runtime)
            val bindings = listOf(PlayerMcpBinding(romeToken, host.bindPlayer("Rome")), PlayerMcpBinding(greeceToken, host.bindPlayer("Greece")))
            check(runCatching { PlayerMcpServer(bindings, 0, OperatorMcpBinding(romeToken, control)) }.isFailure)
            PlayerMcpServer(bindings, 0, OperatorMcpBinding(operatorToken, control), tls.tls).use { server ->
                base = "https://localhost:${server.start()}"
                check(raw("/operator", "{\"op\":\"status\"}", romeToken).statusCode() == 401)
                check(raw("/operator", "{\"op\":\"status\"}", null).statusCode() == 401)
                check(raw("/mcp", rpc("player_turn", JsonObject(emptyMap())), operatorToken).statusCode() == 401)
                check(raw("/operator", "{\"op\":\"status\"}", operatorToken, "http://localhost").statusCode() == 403)
                check(raw("/operator", "{\"op\":\"status\",\"unexpected\":true}", operatorToken).statusCode() == 400)
                check(raw("/operator", " ".repeat(16_385), operatorToken).statusCode() == 400)
                val first = operator(OperatorRequest("status")).status
                check(first.phase == "ACTIVE" && first.handoff == 0L && !first.scheduled)
                val lease = "supervisor-1"
                check(operator(OperatorRequest("acquire", lease)).status.phase == "PAUSED")
                expectOperator("LEASE_BUSY", OperatorRequest("acquire", "competitor"))
                check(operator(OperatorRequest("acquire", lease)).leaseId == lease)
                val paused = turn(romeToken)
                check(paused.phase == "PAUSED" && paused.map.units.all { it.moves.isEmpty() && !it.canFoundCity })
                expectOperator("STALE_TURN", activationRequest(lease, first.copy(handoff = 2)))
                val started = operator(activationRequest(lease, first))
                val rome = checkNotNull(started.playerToken)
                check(rome.length == 43 && rome !in listOf(romeToken, greeceToken, operatorToken))
                check(operator(activationRequest(lease, first)).playerToken == rome)
                check(tool(rome, "player_summary")["civilization"]!!.jsonPrimitive.content == "Rome")
                val original = turn(rome)
                expectPlayer("COMMAND_UNAVAILABLE", romeToken, command(original, TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf(original.researchOptions.first())))
                val end = finishFirstTurn(rome)
                val after = operator(OperatorRequest("status")).status
                check(after.handoff == 1L && after.activeCivilization == "Greece" && after.turn == 0)
                val replay = tool(rome, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), end).jsonObject)
                check(replay["requestId"]!!.jsonPrimitive.content == end.requestId)
                check(operator(OperatorRequest("status")).status.handoff == 1L)
                expectPlayer("COMMAND_UNAVAILABLE", rome, end.copy(requestId = "another-end"))
                check(turn(rome).phase == "WAITING")
                val greece = checkNotNull(operator(activationRequest(lease, after)).playerToken)
                check(raw("/mcp", rpc("player_turn", JsonObject(emptyMap())), rome).statusCode() == 401)
                check(tool(greece, "player_summary")["civilization"]!!.jsonPrimitive.content == "Greece")
                finishFirstTurn(greece)
                val next = operator(OperatorRequest("status")).status
                check(next.handoff == 2L && next.activeCivilization == "Rome" && next.turn == 1)
                revokedToken = checkNotNull(operator(activationRequest(lease, next)).playerToken)
                clock += OperatorControl.LEASE_MILLIS + 1
                check(raw("/mcp", rpc("player_turn", JsonObject(emptyMap())), revokedToken).statusCode() == 401)
                val expired = operator(OperatorRequest("status")).status
                check(expired.phase == "PAUSED" && expired.leaseId == null && expired.scheduled)
                expectOperator("LEASE_LOST", OperatorRequest("heartbeat", lease))
                check(operator(OperatorRequest("manual")).status.phase == "ACTIVE")
                val manual = turn(romeToken)
                val city = manual.cities.single()
                tool(romeToken, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), command(manual, TurnCommandKind.RENAME_CITY, target = city.handle, selection = "Manual Roma")).jsonObject)
                operator(OperatorRequest("acquire", "supervisor-2"))
                revokedToken = checkNotNull(operator(activationRequest("supervisor-2", operator(OperatorRequest("status")).status)).playerToken)
                beforeRestart = operator(OperatorRequest("status")).status
                check(beforeRestart.scheduled && beforeRestart.phase == "ACTIVE")
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val control = OperatorControl(runtime.executor) { clock }
            val restored = control.handle(OperatorRequest("status")).status
            check(restored.epoch == beforeRestart.epoch && restored.handoff == beforeRestart.handoff)
            check(restored.serverInstance != beforeRestart.serverInstance && restored.phase == "PAUSED")
            check(control.activationSession(revokedToken) == null)
            control.handle(OperatorRequest("acquire", "after-restart"))
            val after = control.handle(activationRequest("after-restart", restored))
            check(after.status.phase == "ACTIVE")
            control.handle(OperatorRequest("pause", "after-restart"))
            check(control.activationSession(checkNotNull(after.playerToken)) == null)
            control.handle(OperatorRequest("release", "after-restart"))
            check(control.handle(OperatorRequest("status")).status.phase == "PAUSED")
            runCatching { runtime.executor.haltAfterMutation(IllegalStateException("private injected cause")) }
            check(control.handle(OperatorRequest("status")).status.phase == "HALTED")
            val failed = runCatching { control.handle(OperatorRequest("manual")) }.exceptionOrNull()
            check(failed is OperatorFailure && failed.code == "GAME_HALTED")
        }
    }

    private fun activationRequest(lease: String, status: OperatorStatus) = OperatorRequest("activate", lease, status.epoch, status.serverInstance, status.handoff, status.activeCivilization, "activation-${status.handoff}")

    private fun operator(request: OperatorRequest): OperatorResponse {
        val response = raw("/operator", OperatorControl.json.encodeToString(request), operatorToken)
        check(response.statusCode() == 200) { "Operator ${request.op} returned ${response.statusCode()}: ${response.body()}" }
        return OperatorControl.json.decodeFromString(response.body())
    }

    private fun expectOperator(code: String, request: OperatorRequest) {
        val response = raw("/operator", OperatorControl.json.encodeToString(request), operatorToken)
        check(response.statusCode() == 409 && Json.parseToJsonElement(response.body()).jsonObject["error"]!!.jsonPrimitive.content == code)
    }

    private fun raw(path: String, body: String, token: String?, origin: String? = null): HttpResponse<String> {
        val request = HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofSeconds(10))
            .header("Content-Type", "application/json").header("Accept", "application/json, text/event-stream")
        if (token != null) request.header("Authorization", "Bearer $token")
        if (origin != null) request.header("Origin", origin)
        return client.send(request.POST(HttpRequest.BodyPublishers.ofString(body)).build(), HttpResponse.BodyHandlers.ofString())
    }

    private fun rpc(name: String, arguments: JsonObject): String = """{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"$name","arguments":$arguments}}"""

    private fun tool(token: String, name: String, arguments: JsonObject = JsonObject(emptyMap())): JsonObject {
        val response = raw("/mcp", rpc(name, arguments), token)
        check(response.statusCode() == 200) { "MCP returned ${response.statusCode()}" }
        val result = Json.parseToJsonElement(response.body()).jsonObject["result"]!!.jsonObject
        check(result["isError"]!!.jsonPrimitive.boolean.not()) { result.toString() }
        return result["structuredContent"]!!.jsonObject
    }

    private fun expectPlayer(code: String, token: String, command: TurnCommandRequest) {
        val response = raw("/mcp", rpc("player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), command).jsonObject), token)
        val result = Json.parseToJsonElement(response.body()).jsonObject["result"]!!.jsonObject
        check(result["isError"]!!.jsonPrimitive.boolean)
        check(result["structuredContent"]!!.jsonObject["error"]!!.jsonObject["code"] == JsonPrimitive(code))
    }

    private fun turn(token: String): PlayerTurnSnapshot = PlayerTurnJson.json.decodeFromString(tool(token, "player_turn").toString())

    private fun command(snapshot: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null, queue: List<String> = emptyList()) = TurnCommandRequest(UUID.randomUUID().toString(), snapshot.epoch, snapshot.revision, kind, target, selection, queue)

    private fun finishFirstTurn(token: String): TurnCommandRequest {
        repeat(40) {
            val snapshot = turn(token)
            val settler = snapshot.map.units.firstOrNull { it.canFoundCity && it.settlementPromises.isEmpty() }
            val city = snapshot.cities.firstOrNull { it.constructionQueue.isEmpty() }
            val alert = snapshot.alerts.firstOrNull { it.canAcknowledge }
            val request = when {
                settler != null && snapshot.cities.isEmpty() -> command(snapshot, TurnCommandKind.FOUND_CITY, settler.handle)
                alert != null -> command(snapshot, TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle)
                snapshot.researchQueue.isEmpty() -> command(snapshot, TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf(snapshot.researchOptions.first()))
                city != null -> command(snapshot, TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, city.constructionOptions.first())
                snapshot.policyOptions.isNotEmpty() && snapshot.pending.any { it.kind == "Policy" } -> command(snapshot, TurnCommandKind.ADOPT_POLICY, selection = snapshot.policyOptions.first())
                snapshot.canEndTurn -> command(snapshot, TurnCommandKind.END_TURN)
                else -> error("Unsupported fixture decision: ${snapshot.pending}")
            }
            tool(token, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject)
            if (request.kind == TurnCommandKind.END_TURN) return request
        }
        error("Fixture turn did not complete")
    }
}
