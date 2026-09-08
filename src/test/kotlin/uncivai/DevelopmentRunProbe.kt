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
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Files
import java.nio.file.Path
import java.time.Duration
import java.util.UUID

/** Scripted players act exclusively on their HTTPS observations; engine reads only verify outcomes. */
object DevelopmentRunProbe {
    private lateinit var client: HttpClient
    private lateinit var base: String
    private val operatorToken = "O".repeat(43)

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val starting = args[1] == "start"
        val certRoot = Files.createDirectories(root.resolve(args[1] + "-tls"))
        val tls = TlsTestFixture(certRoot)
        client = HttpClient.newBuilder().sslContext(tls.context).connectTimeout(Duration.ofSeconds(5)).build()
        val checkpoint = root.resolve("checkpoint.json")
        val loaded = if (starting) HeadlessRuntime.createDuel(root.resolve("initial"), 412L) else HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored"))
        loaded.use { runtime ->
            if (starting) runtime.executor.enableCheckpoints(checkpoint)
            val host = PlayerHost(runtime)
            val control = OperatorControl(runtime.executor)
            val bindings = listOf("Rome", "Greece").mapIndexed { index, civ -> PlayerMcpBinding(('A' + index).toString().repeat(43), host.bindPlayer(civ)) }
            PlayerMcpServer(bindings, 0, OperatorMcpBinding(operatorToken, control), tls.tls).use { server ->
                base = "https://localhost:${server.start()}"
                val before = operator(OperatorRequest("status")).status
                if (starting) {
                    Files.writeString(root.resolve("epoch"), before.epoch)
                } else {
                    check(before.epoch == Files.readString(root.resolve("epoch")))
                    check(before.phase == "PAUSED" && before.turn == 10 && before.handoff == 20L)
                }
                val lease = UUID.randomUUID().toString()
                operator(OperatorRequest("acquire", lease))
                repeat(20) {
                    val current = operator(OperatorRequest("status")).status
                    val activation = operator(OperatorRequest("activate", lease, current.epoch, current.serverInstance, current.handoff, current.activeCivilization, UUID.randomUUID().toString()))
                    val token = checkNotNull(activation.playerToken)
                    develop(token, current.activeCivilization)
                    val after = operator(OperatorRequest("status")).status
                    check(after.handoff == current.handoff + 1 && after.activeCivilization != current.activeCivilization)
                }
                operator(OperatorRequest("release", lease))
                check(operator(OperatorRequest("status")).status.turn == if (starting) 10 else 20)
            }
            for (civ in listOf("Rome", "Greece")) {
                val snapshot = host.bindPlayer(civ).turn()
                val city = snapshot.cities.single().development!!
                check(city.population > 1) { "$civ did not grow" }
                if (!starting) {
                    check(snapshot.map.units.any { it.worker != null }) { "$civ did not build a worker" }
                    check(snapshot.map.tiles.any { it.owner == civ && it.cityName == null && it.improvement in setOf("Farm", "Mine", "Pasture", "Camp", "Plantation", "Quarry") }) { "$civ did not finish an improvement" }
                    check(city.builtBuildings.any { it in setOf("Granary", "Monument") } || city.constructions.any { (it.productionStored ?: 0) > 0 }) { "$civ did not continue production after building a worker" }
                    check(snapshot.research!!.technologies.count { it.researched } > 2)
                }
            }
        }
        client.close()
    }

    private fun develop(token: String, civ: String) {
        val workerActed = mutableSetOf<String>()
        repeat(60) {
            val snapshot = PlayerTurnJson.json.decodeFromString<PlayerTurnSnapshot>(tool(token, "player_turn").toString())
            check(snapshot.pending.none { !it.supported }) { "Unsupported fixture decision: ${snapshot.pending}" }
            fun command(kind: TurnCommandKind, target: String? = null, selection: String? = null, queue: List<String> = emptyList(), destination: TilePosition? = null) = TurnCommandRequest(UUID.randomUUID().toString(), snapshot.epoch, snapshot.revision, kind, target, selection, queue, destination).reviewedFor(snapshot)
            val settler = snapshot.map.units.firstOrNull { it.canFoundCity && it.settlementPromises.isEmpty() }
            val city = snapshot.cities.firstOrNull { it.constructionQueue.isEmpty() }
            val alert = snapshot.alerts.firstOrNull { it.canAcknowledge }
            val worker = snapshot.map.units.firstOrNull { it.worker != null && it.worker.currentJob == null && it.handle !in workerActed }
            val request = when {
                settler != null && snapshot.cities.isEmpty() -> command(TurnCommandKind.FOUND_CITY, settler.handle)

                alert != null -> command(TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle)

                snapshot.freeTechnologies > 0 -> command(TurnCommandKind.CHOOSE_FREE_TECHNOLOGY, selection = snapshot.researchOptions.first())

                snapshot.freeGreatPeople > 0 -> command(TurnCommandKind.CHOOSE_FREE_GREAT_PERSON, selection = snapshot.greatPersonOptions.first())

                snapshot.researchQueue.isEmpty() -> {
                    val choice = listOf("Pottery", "Mining", "Animal Husbandry", "Calendar", "Trapping", "Masonry", "The Wheel", "Writing").firstOrNull { it in snapshot.researchOptions } ?: snapshot.researchOptions.first()
                    command(TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf(choice))
                }

                city != null -> {
                    val choices = if (snapshot.map.units.none { it.worker != null }) listOf("Worker") else listOf("Granary", "Monument", "Library", "Archer")
                    command(TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, choices.firstOrNull { it in city.constructionOptions } ?: city.constructionOptions.first())
                }

                snapshot.policyOptions.isNotEmpty() && snapshot.pending.any { it.kind == "Policy" } -> command(TurnCommandKind.ADOPT_POLICY, selection = listOf("Tradition", "Legalism", "Landed Elite").firstOrNull { it in snapshot.policyOptions } ?: snapshot.policyOptions.first())

                worker != null -> {
                    workerActed.add(worker.handle)
                    val option = worker.worker!!.options.firstOrNull { it.available && it.name in setOf("Farm", "Mine", "Pasture", "Camp", "Plantation", "Quarry") }
                    if (option != null) {
                        command(TurnCommandKind.START_WORKER_JOB, worker.handle, option.name)
                    } else {
                        val move = worker.moves.firstOrNull { move -> snapshot.map.tiles.any { it.position == move.destination && it.owner == civ && it.cityName == null && it.improvement == null && it.terrain in setOf("Grassland", "Plains", "Desert", "Tundra") } }
                        if (move == null) return@repeat
                        command(TurnCommandKind.MOVE_UNIT, worker.handle, destination = move.destination)
                    }
                }

                snapshot.canEndTurn -> command(TurnCommandKind.END_TURN)

                else -> error("Fixture blocked: ${snapshot.pending}")
            }
            val receipt = tool(token, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject)
            if (request.kind == TurnCommandKind.END_TURN) {
                // Lost acknowledgement: an exact retry remains a read after this activation's handoff.
                check(tool(token, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject) == receipt)
                return
            }
        }
        error("Fixture command budget exhausted")
    }

    private fun raw(path: String, body: String, token: String): String {
        val response = client.send(
            HttpRequest.newBuilder(URI.create(base + path))
                .timeout(Duration.ofSeconds(15)).header("Authorization", "Bearer $token")
                .header("Content-Type", "application/json").header("Accept", "application/json, text/event-stream")
                .POST(HttpRequest.BodyPublishers.ofString(body)).build(),
            HttpResponse.BodyHandlers.ofString(),
        )
        check(response.statusCode() == 200) { "HTTPS ${response.statusCode()}: ${response.body()}" }
        return response.body()
    }

    private fun operator(request: OperatorRequest): OperatorResponse = OperatorControl.json.decodeFromString(raw("/operator", OperatorControl.json.encodeToString(request), operatorToken))

    private fun tool(token: String, name: String, args: JsonObject = JsonObject(emptyMap())): JsonObject {
        val body = """{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"$name","arguments":$args}}"""
        val result = Json.parseToJsonElement(raw("/mcp", body, token)).jsonObject.getValue("result").jsonObject
        check(!result.getValue("isError").jsonPrimitive.boolean) { result.toString() }
        return result.getValue("structuredContent").jsonObject
    }
}
