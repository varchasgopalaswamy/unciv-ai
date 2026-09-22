package uncivai

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.future.await
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.mcp.OperatorMcpBinding
import uncivai.mcp.PlayerMcpBinding
import uncivai.mcp.PlayerMcpServer
import uncivai.player.PlayerHost
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Path
import java.time.Duration

/** Exercise quiet long polls through the same TLS transport as the live services. */
object CommunicationHttpProbe {
    @JvmStatic
    fun main(args: Array<String>) = runBlocking(Dispatchers.IO) {
        val root = Path.of(args.single())
        val tls = TlsTestFixture(root)
        HeadlessRuntime.createBilateralExercise(root.resolve("game")).use { runtime ->
            runtime.executor.enableCheckpoints(root.resolve("checkpoint.json"))
            val tokens = listOf("R".repeat(43), "G".repeat(43))
            val sessions = listOf("Rome", "Greece").map { PlayerHost(runtime).bindPlayer(it) }
            val control = OperatorControl(runtime.executor) { 0L }
            control.handle(OperatorRequest("acquire", leaseId = "polling-test"))
            val temporaryTokens = (1..68).map {
                checkNotNull(control.handle(OperatorRequest("connect", leaseId = "polling-test", epoch = runtime.executor.epoch, civilization = "Rome")).playerToken)
            }
            PlayerMcpServer(tokens.zip(sessions) { token, session -> PlayerMcpBinding(token, session) }, port = 0, tls = tls.tls, operator = OperatorMcpBinding("O".repeat(43), control)).use { server ->
                val url = URI("https://localhost:${server.start()}/mcp")
                HttpClient.newBuilder().sslContext(tls.context).connectTimeout(Duration.ofSeconds(5)).build().use { client ->
                    suspend fun tool(token: String, request: JsonObject): JsonObject {
                        val body = obj("jsonrpc" to "2.0", "id" to 1, "method" to "tools/call", "params" to obj("name" to "player_communication", "arguments" to obj("request" to request)))
                        val response = client.sendAsync(
                            HttpRequest.newBuilder(url).timeout(Duration.ofSeconds(35))
                                .header("Content-Type", "application/json").header("Accept", "application/json, text/event-stream")
                                .header("Authorization", "Bearer $token")
                                .POST(HttpRequest.BodyPublishers.ofString(body.toString())).build(),
                            HttpResponse.BodyHandlers.ofString(),
                        ).await()
                        check(response.statusCode() == 200) { "Poll returned ${response.statusCode()}: ${response.body()}" }
                        val result = Json.parseToJsonElement(response.body()).jsonObject.getValue("result").jsonObject
                        check(!result.getValue("isError").jsonPrimitive.boolean) { result.toString() }
                        return result.getValue("structuredContent").jsonObject
                    }
                    fun read(wait: Int) = obj("op" to "read", "epoch" to runtime.executor.epoch, "cursor" to 0, "waitSeconds" to wait)
                    repeat(3) {
                        val polls = tokens.map { token -> async { tool(token, read(1)) } }
                        repeat(3) { tool(tokens.first(), read(0)) }
                        for (poll in polls) check(poll.await().getValue("events").jsonArray.isEmpty())
                    }
                    // Long-lived communication credentials and short-lived activation credentials
                    // share the authenticated transport. Exceed the old 64-server cache while
                    // requests are in flight: credential churn must not close someone else's poll.
                    val rotatingPolls = temporaryTokens.map { token -> async { tool(token, read(5)) } }
                    for (poll in rotatingPolls) check(poll.await().getValue("events").jsonArray.isEmpty())
                    val polls = tokens.map { token -> async { tool(token, read(25)) } }
                    repeat(26) {
                        tool(tokens.first(), read(0))
                        delay(1000)
                    }
                    for (poll in polls) check(poll.await().getValue("events").jsonArray.isEmpty())
                    val incoming = async { tool(tokens.last(), read(25)) }
                    tool(tokens.first(), obj("op" to "send", "epoch" to runtime.executor.epoch, "requestId" to "wake", "to" to "Greece", "text" to "Hello"))
                    val page = withTimeout(3000) { incoming.await() }
                    check(page.getValue("events").jsonArray.single().jsonObject.getValue("body").jsonObject.getValue("text").jsonPrimitive.content == "Hello")
                }
            }
        }
    }
}
