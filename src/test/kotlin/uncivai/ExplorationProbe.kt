package uncivai

import com.unciv.json.json
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.view.GameView
import io.ktor.client.HttpClient
import io.ktor.client.engine.cio.CIO
import io.ktor.client.plugins.sse.SSE
import io.ktor.client.request.header
import io.ktor.http.HttpHeaders
import io.modelcontextprotocol.kotlin.sdk.ExperimentalMcpApi
import io.modelcontextprotocol.kotlin.sdk.client.Client
import io.modelcontextprotocol.kotlin.sdk.client.StreamableHttpClientTransport
import io.modelcontextprotocol.kotlin.sdk.types.CallToolResult
import io.modelcontextprotocol.kotlin.sdk.types.Implementation
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.mcp.PlayerMcpBinding
import uncivai.mcp.PlayerMcpServer
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

@Serializable
internal data class ExplorationRecovery(val request: TurnCommandRequest, val receipt: TurnCommandReceipt, val snapshot: PlayerTurnSnapshot)

/** The scripted players use the real transport. Raw state is used only by the trusted test assertions. */
@OptIn(ExperimentalMcpApi::class)
object ExplorationProbe {
    @JvmStatic
    fun main(args: Array<String>) = runBlocking {
        val root = Path.of(args.single())
        HeadlessRuntime.loadCheckpoint(root.resolve("game.json"), root.resolve("data")).use { runtime ->
            val game = runtime.game
            check(game.turns == 0 && game.currentPlayer == "Rome")
            check(game.gameParameters.speed == "Quick" && game.gameParameters.startingEra == "Ancient era")
            check(game.gameParameters.noBarbarians && game.gameParameters.numberOfCityStates == 0)
            check(game.tileMap.mapParameters.noRuins && game.gameParameters.victoryTypes == listOf("Domination", "Time"))
            check(game.civilizations.filter { it.isMajorCiv() }.all { it.isHuman() && it.cities.isEmpty() })
            val host = PlayerHost(runtime)
            val romeSession = host.bindPlayer("Rome")
            val greeceSession = host.bindPlayer("Greece")
            val initialRaw = json().toJson(game)
            val initial = romeSession.turn()
            check(initialRaw == json().toJson(game)) { "Observation changed engine state" }
            check(initial.map.tiles.isNotEmpty() && initial.map.tiles.size < game.tileMap.tileList.size)
            check(initial.map.units.any { it.canFoundCity })
            check(initial == host.bindPlayer("Rome").turn())

            // Unexplored terrain, unrevealed resources, and unseen opposing units cannot change the response or revision.
            val player = game.getCivilization("Rome")
            val hidden = game.tileMap.tileList.first { tile ->
                !tile.isExplored(player) && tile.getUnits().none() && tile.getOwner() == null &&
                    player.units.getCivUnits().all { it.currentTile.aerialDistanceTo(tile) > 5 }
            }
            hidden.baseTerrain = "Mountain"
            hidden.setTerrainTransients()
            hidden.tileResource = game.ruleset.tileResources.getValue("Iron")
            val foreign = game.getCivilization("Greece").units.getCivUnits().first()
            check(!foreign.isVisibleTo(player))
            foreign.instanceName = "UNSEEN-UNIT-SECRET"
            foreign.health = 61
            check(initial == romeSession.turn()) { "Hidden state affected player output" }

            // Match the UI's remembered improvement on explored but unseen tiles.
            hidden.setExplored(player, true)
            player.setLastSeenImprovement(hidden.position, "Farm")
            hidden.improvement = "Mine"
            val fog = romeSession.turn()
            val remembered = fog.map.tiles.single { it.position == TilePosition(hidden.position.x, hidden.position.y) }
            check(!remembered.visible && remembered.improvement == "Farm" && remembered.resource == null)
            hidden.improvement = null
            hidden.tileResource = game.ruleset.tileResources.getValue("Uranium")
            check(fog == romeSession.turn()) { "Fog or resource reveal leaked live state" }
            val view = GameView(game, player).tileMapView.getTile(hidden)
            check(view.getShownImprovement() == remembered.improvement)
            check(view.getViewableResource(GameView(game, player).civView) == null)
            check("UNSEEN-UNIT-SECRET" !in PlayerTurnJson.encode(romeSession.turn()))

            PlayerMcpServer(listOf(PlayerMcpBinding("A".repeat(43), romeSession), PlayerMcpBinding("B".repeat(43), greeceSession)), port = 0).use { server ->
                val endpoint = "http://127.0.0.1:${server.start()}/mcp"
                val rome = connect(endpoint, "A".repeat(43))
                val greece = connect(endpoint, "B".repeat(43))
                try {
                    val before = rome.turn()
                    check(before == romeSession.turn())
                    val settler = before.map.units.single { it.name == "Settler" }
                    val greekSettler = greece.turn().map.units.single { it.name == "Settler" }
                    rome.error(command(before, TurnCommandKind.FOUND_CITY, target = greekSettler.handle), "UNIT_UNAVAILABLE")
                    greece.error(command(greece.turn(), TurnCommandKind.FOUND_CITY, target = greekSettler.handle), "NOT_YOUR_TURN")
                    rome.error(command(before, TurnCommandKind.MOVE_UNIT, settler.handle, destination = TilePosition(Int.MAX_VALUE, Int.MIN_VALUE)), "OPTION_UNAVAILABLE")
                    rome.error(command(before, TurnCommandKind.MOVE_UNIT, settler.handle), "INVALID_ARGUMENT")
                    rome.error(command(before, TurnCommandKind.FOUND_CITY, settler.handle, destination = settler.position), "INVALID_ARGUMENT")

                    // Move a starting military unit using only the returned options, retaining the settler's movement.
                    val scout = before.map.units.first { it.name != "Settler" && it.moves.isNotEmpty() }
                    val move = scout.moves.maxBy { it.path.size }
                    val moveRequest = command(before, TurnCommandKind.MOVE_UNIT, scout.handle, destination = move.destination)
                    val moveReceipt = rome.execute(moveRequest)
                    val afterMove = rome.turn()
                    check(afterMove.map.units.single { it.handle == scout.handle }.position != scout.position)
                    check(afterMove.revision != before.revision)
                    check(rome.execute(moveRequest) == moveReceipt)
                    rome.error(command(before, TurnCommandKind.FOUND_CITY, settler.handle), "STALE_REVISION")

                    val settle = command(afterMove, TurnCommandKind.FOUND_CITY, settler.handle)
                    val settled = rome.execute(settle)
                    val afterSettle = rome.turn()
                    check(afterSettle.cities.size == 1 && afterSettle.map.units.none { it.handle == settler.handle })
                    check(rome.execute(settle) == settled)
                    rome.error(command(afterSettle, TurnCommandKind.MOVE_UNIT, settler.handle, destination = move.destination), "UNIT_UNAVAILABLE")
                    check(player.cities.single().location == com.unciv.logic.map.HexCoord(settler.position.x, settler.position.y))
                    resolve(rome)
                    rome.execute(command(rome.turn(), TurnCommandKind.END_TURN))
                    check(greece.turn().phase == "ACTIVE" && rome.turn().phase == "WAITING")
                    check(game.currentPlayer == "Greece")
                    // No engine AI acted for the second human slot.
                    check(greece.turn().cities.isEmpty() && greece.turn().map.units.any { it.name == "Settler" })

                    val saved = ExplorationRecovery(settle, settled, rome.turn())
                    Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(saved))
                } finally {
                    rome.close()
                    greece.close()
                }
            }
            // Keep the unit operations exercised against the same authoritative game after transport shutdown.
            check(player.units.getCivUnits().all { PlayerUnitOperations(player).owns(it) })
        }
    }

    private suspend fun resolve(client: Connection) {
        repeat(30) {
            val turn = client.turn()
            if (turn.canEndTurn) return
            val decision = turn.pending.first()
            check(decision.supported) { "Unsupported initial decision: $decision" }
            val request = when (decision.kind) {
                "Construction" -> command(turn, TurnCommandKind.QUEUE_CONSTRUCTION, turn.cities.first { it.constructionQueue.isEmpty() }.handle, selection = "Scout")
                "Research" -> command(turn, TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf("Pottery"))
                "PopupAlerts" -> command(turn, TurnCommandKind.ACKNOWLEDGE_ALERT, turn.alerts.first { it.canAcknowledge }.handle)
                "Policy" -> command(turn, TurnCommandKind.ADOPT_POLICY, selection = turn.policyOptions.first())
                else -> error("Unexpected initial decision: $decision")
            }
            client.execute(request)
        }
        error("Turn decisions did not converge")
    }

    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null, queue: List<String> = emptyList(), destination: TilePosition? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection, queue, destination)

    private suspend fun connect(endpoint: String, token: String): Connection {
        val http = HttpClient(CIO) { install(SSE) }
        val client = Client(Implementation("exploration-test", "1.0"))
        try {
            client.connect(StreamableHttpClientTransport(http, endpoint, requestBuilder = { header(HttpHeaders.Authorization, "Bearer $token") }))
            return Connection(client, http)
        } catch (failure: Throwable) {
            http.close()
            throw failure
        }
    }

    private class Connection(val client: Client, val http: HttpClient) {
        suspend fun turn(): PlayerTurnSnapshot = PlayerTurnJson.json.decodeFromString(success(client.callTool("player_turn", emptyMap())).toString())

        suspend fun execute(request: TurnCommandRequest): TurnCommandReceipt = PlayerTurnJson.json.decodeFromString(success(call(request)).toString())

        suspend fun error(request: TurnCommandRequest, expected: String) {
            val result = call(request)
            check(result.isError == true && result.structuredContent?.get("error")?.jsonObject?.get("code")?.jsonPrimitive?.content == expected) { "Expected $expected, got $result" }
        }

        private suspend fun call(request: TurnCommandRequest) = client.callTool(
            "player_command",
            buildMap<String, Any?> {
                put("requestId", request.requestId)
                put("epoch", request.epoch)
                put("revision", request.revision)
                put("kind", request.kind.name)
                request.target?.let { put("target", it) }
                request.selection?.let { put("selection", it) }
                if (request.queue.isNotEmpty()) put("queue", request.queue)
                request.destination?.let { put("destination", mapOf("x" to it.x, "y" to it.y)) }
            },
        )

        suspend fun close() {
            try {
                client.close()
            } finally {
                http.close()
            }
        }

        private fun success(result: CallToolResult) = checkNotNull(result.structuredContent).also { check(result.isError == false) { "Tool failed: $result" } }
    }
}

object ExplorationRecoveryProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        val saved = PlayerTurnJson.json.decodeFromString<ExplorationRecovery>(Files.readString(root.resolve("expected.json")))
        HeadlessRuntime.loadCheckpoint(root.resolve("game.json"), root.resolve("recovered")).use { runtime ->
            val player = PlayerHost(runtime).bindPlayer("Rome")
            val raw = json().toJson(runtime.game)
            check(player.turn() == saved.snapshot) { "Recovery changed observed state or unit handles" }
            check(player.execute(saved.request) == saved.receipt) { "Settlement receipt was not preserved" }
            check(json().toJson(runtime.game) == raw) { "Recovery replay repeated settlement" }
            check(runtime.game.getCivilization("Rome").cities.size == 1)
        }
    }
}
