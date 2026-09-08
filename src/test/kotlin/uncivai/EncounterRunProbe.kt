package uncivai

import com.unciv.json.json
import com.unciv.logic.map.HexCoord
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.mcp.OperatorMcpBinding
import uncivai.mcp.PlayerMcpBinding
import uncivai.mcp.PlayerMcpServer
import uncivai.player.PlayerAlert
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TilePosition
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

@Serializable
private data class EncounterRecovery(
    val epoch: String,
    val romeAlert: PlayerAlert,
    val greeceAlert: PlayerAlert,
    val acknowledgement: TurnCommandRequest? = null,
    val receipt: TurnCommandReceipt? = null,
)

/** Fixture setup is trusted; every action after startup uses advertised commands over verified HTTPS. */
object EncounterRunProbe {
    private lateinit var client: HttpClient
    private lateinit var base: String
    private val operatorToken = "O".repeat(43)
    private val playerTokens = mapOf("Rome" to "R".repeat(43), "Greece" to "G".repeat(43))

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val stage = args[1]
        val tls = TlsTestFixture(Files.createDirectories(root.resolve("$stage-tls")))
        client = HttpClient.newBuilder().sslContext(tls.context).connectTimeout(Duration.ofSeconds(5)).build()
        val checkpoint = root.resolve("checkpoint.json")
        val runtime = if (stage == "meet") {
            HeadlessRuntime.createDuel(root.resolve("initial"), 412L)
        } else {
            HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve(stage))
        }
        try {
            runtime.use {
                if (stage == "meet") {
                    arrangeUnmetPlayers(runtime)
                    runtime.executor.enableCheckpoints(checkpoint)
                }
                val host = PlayerHost(runtime)
                val control = OperatorControl(runtime.executor)
                val bindings = playerTokens.map { (civilization, token) -> PlayerMcpBinding(token, host.bindPlayer(civilization)) }
                PlayerMcpServer(bindings, 0, OperatorMcpBinding(operatorToken, control), tls.tls).use { server ->
                    base = "https://localhost:${server.start()}"
                    val before = operator(OperatorRequest("status")).status
                    val saved = if (stage == "meet") null else PlayerTurnJson.json.decodeFromString<EncounterRecovery>(Files.readString(root.resolve("expected.json")))
                    if (saved != null) {
                        check(before.epoch == saved.epoch && before.phase == "PAUSED" && before.handoff == 0L)
                        check(before.activeCivilization == "Rome" && before.leaseId == null)
                        val greek = turn(playerTokens.getValue("Greece"))
                        check(greek.alerts.single { it.kind == "FirstContact" } == saved.greeceAlert)
                        check(greek.pending.single { it.kind == "PopupAlerts" }.supported)
                        if (stage == "acknowledge") {
                            val roman = turn(playerTokens.getValue("Rome"))
                            check(roman.alerts.single { it.kind == "FirstContact" } == saved.romeAlert.copy(canAcknowledge = false))
                            check(roman.pending.single { it.kind == "PopupAlerts" }.supported)
                        }
                    }
                    val lease = UUID.randomUUID().toString()
                    operator(OperatorRequest("acquire", lease))
                    val token = activate(lease)
                    when (stage) {
                        "meet" -> meet(root, token)
                        "acknowledge" -> acknowledge(root, token, checkNotNull(saved))
                        "continue" -> continuePlaying(runtime, lease, token, checkNotNull(saved))
                        else -> error("Unknown fixture stage: $stage")
                    }
                    operator(OperatorRequest("release", lease))
                }
            }
        } finally {
            client.close()
        }
    }

    private fun arrangeUnmetPlayers(runtime: HeadlessRuntime) {
        val game = runtime.game
        val civilizations = playerTokens.keys.map(game::getCivilization)
        for (tile in game.tileMap.tileList) {
            tile.baseTerrain = "Grassland"
            tile.setTerrainFeatures(emptyList())
            tile.naturalWonder = null
            tile.tileResource = null
            tile.improvement = null
            tile.hasBottomRiver = false
            tile.hasBottomLeftRiver = false
            tile.hasBottomRightRiver = false
            tile.setTerrainTransients()
            civilizations.forEach { tile.setExplored(it, false) }
        }
        val units = civilizations.flatMap { it.units.getCivUnits().toList() }
        check(units.size == 4 && civilizations.all { it.cities.isEmpty() && it.diplomacy.isEmpty() })
        units.forEach { it.removeFromTile() }
        for (unit in units) {
            val position = when {
                unit.civ.civName == "Greece" -> HexCoord(3, 0)
                unit.isCivilian() -> HexCoord(-3, 0)
                else -> HexCoord(0, 0)
            }
            val tile = game.tileMap[position]
            unit.currentTile = tile
            if (unit.isCivilian()) tile.civilianUnit = unit else tile.militaryUnit = unit
            unit.updateVisibleTiles(updateCivViewableTiles = false)
        }
        civilizations.forEach { it.cache.updateViewableTiles() }
        check(civilizations.all { it.diplomacy.isEmpty() && it.popupAlerts.none { alert -> alert.type.name == "FirstContact" } }) {
            "The fixture must begin before either player has encountered the other"
        }
    }

    private fun meet(root: Path, token: String) {
        val before = turn(token)
        check(before.diplomacy.knownCivilizations.isEmpty() && before.alerts.none { it.kind == "FirstContact" })
        check(turn(playerTokens.getValue("Greece")).diplomacy.knownCivilizations.isEmpty())
        val warrior = before.map.units.single { it.name == "Warrior" }
        // Only an offered move is submitted; the engine's ordinary visibility update creates contact.
        val move = warrior.moves.single { it.destination == TilePosition(1, 0) }
        execute(token, command(before, TurnCommandKind.MOVE_UNIT, warrior.handle, destination = move.destination))
        val roman = turn(token)
        val greek = turn(playerTokens.getValue("Greece"))
        val romanAlert = contact(roman, "Greece", active = true)
        val greekAlert = contact(greek, "Rome", active = false)
        check(!roman.canEndTurn && roman.pending.single { it.kind == "PopupAlerts" }.supported)
        check(!greek.canEndTurn && greek.pending.single { it.kind == "PopupAlerts" }.supported)
        Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(EncounterRecovery(roman.epoch, romanAlert, greekAlert)))
    }

    private fun acknowledge(root: Path, token: String, saved: EncounterRecovery) {
        val before = turn(token)
        check(contact(before, "Greece", active = true) == saved.romeAlert)
        val request = command(before, TurnCommandKind.ACKNOWLEDGE_ALERT, saved.romeAlert.handle)
        val receipt = execute(token, request)
        check(turn(token).alerts.none { it.kind == "FirstContact" })
        check(turn(playerTokens.getValue("Greece")).alerts.single { it.kind == "FirstContact" } == saved.greeceAlert)
        Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(saved.copy(acknowledgement = request, receipt = receipt)))
    }

    private fun continuePlaying(runtime: HeadlessRuntime, lease: String, firstToken: String, saved: EncounterRecovery) {
        val before = turn(firstToken)
        check(before.alerts.none { it.kind == "FirstContact" })
        val rawBefore = json().toJson(runtime.game)
        val statusBefore = operator(OperatorRequest("status")).status
        check(execute(firstToken, checkNotNull(saved.acknowledgement)) == saved.receipt)
        check(json().toJson(runtime.game) == rawBefore) { "Restart replay repeated first-contact effects" }
        check(turn(firstToken) == before)
        check(operator(OperatorRequest("status")).status.copy(leaseExpiresInMs = 0) == statusBefore.copy(leaseExpiresInMs = 0))

        var token = firstToken
        val completed = mutableMapOf("Rome" to 0, "Greece" to 0)
        var greekAcknowledgements = 0
        repeat(8) { index ->
            val status = operator(OperatorRequest("status")).status
            if (index > 0) token = activate(lease)
            val civilization = status.activeCivilization
            greekAcknowledgements += finishTurn(token, civilization, saved.greeceAlert.handle)
            val after = operator(OperatorRequest("status")).status
            check(after.handoff == status.handoff + 1 && after.activeCivilization != civilization)
            completed[civilization] = completed.getValue(civilization) + 1
        }
        check(completed.values.all { it == 4 } && greekAcknowledgements == 1)
        val final = operator(OperatorRequest("status")).status
        check(final.turn == 4 && final.handoff == 8L)
        for ((civilization, staticToken) in playerTokens) {
            val snapshot = turn(staticToken)
            check(snapshot.alerts.none { it.kind == "FirstContact" })
            check(snapshot.cities.size == 1)
            check(snapshot.research!!.technologies.any { it.scienceStored > 0 }) { "$civilization did not continue research" }
            knownCivilization(snapshot, if (civilization == "Rome") "Greece" else "Rome")
        }
    }

    private fun contact(snapshot: PlayerTurnSnapshot, other: String, active: Boolean): PlayerAlert {
        val alert = snapshot.alerts.single { it.kind == "FirstContact" }
        val details = checkNotNull(alert.firstContact)
        check(alert.canAcknowledge == active && alert.subject == other)
        check(details.civilizationId == other && details.civilizationName == other)
        check(details.leaderName.isNotBlank() && details.message.isNotBlank() && details.acknowledgement.isNotBlank())
        val known = knownCivilization(snapshot, other)
        check(known.leaderName == details.leaderName)
        return alert
    }

    private fun knownCivilization(snapshot: PlayerTurnSnapshot, other: String) = snapshot.diplomacy.knownCivilizations.single().also {
        check(it.civilizationId == other && it.civilizationName == other && !it.atWar)
    }

    private fun finishTurn(token: String, civilization: String, greekAlertHandle: String): Int {
        var contactsAcknowledged = 0
        repeat(40) {
            val snapshot = turn(token)
            check(snapshot.pending.none { !it.supported }) { "Unsupported encounter decision: ${snapshot.pending}" }
            knownCivilization(snapshot, if (civilization == "Rome") "Greece" else "Rome")
            val alert = snapshot.alerts.firstOrNull { it.canAcknowledge }
            val settler = snapshot.map.units.firstOrNull { it.canFoundCity && it.settlementPromises.isEmpty() }
            val city = snapshot.cities.firstOrNull { it.constructionQueue.isEmpty() }
            val request = when {
                alert != null -> {
                    if (alert.kind == "FirstContact") {
                        check(civilization == "Greece" && alert.handle == greekAlertHandle && contactsAcknowledged == 0)
                        contact(snapshot, "Rome", active = true)
                        contactsAcknowledged++
                    }
                    command(snapshot, TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle)
                }

                settler != null && snapshot.cities.isEmpty() -> command(snapshot, TurnCommandKind.FOUND_CITY, settler.handle)

                snapshot.freeTechnologies > 0 -> command(snapshot, TurnCommandKind.CHOOSE_FREE_TECHNOLOGY, selection = snapshot.researchOptions.first())

                snapshot.freeGreatPeople > 0 -> command(snapshot, TurnCommandKind.CHOOSE_FREE_GREAT_PERSON, selection = snapshot.greatPersonOptions.first())

                snapshot.researchQueue.isEmpty() -> command(snapshot, TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf(snapshot.researchOptions.first()))

                city != null -> command(snapshot, TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, selection = "Monument".takeIf { it in city.constructionOptions } ?: city.constructionOptions.first())

                snapshot.pending.any { it.kind == "Policy" } && snapshot.policyOptions.isNotEmpty() -> command(snapshot, TurnCommandKind.ADOPT_POLICY, selection = snapshot.policyOptions.first())

                snapshot.canEndTurn -> command(snapshot, TurnCommandKind.END_TURN)

                else -> error("Encounter fixture blocked: ${snapshot.pending}")
            }
            execute(token, request)
            if (request.kind == TurnCommandKind.END_TURN) return contactsAcknowledged
        }
        error("Encounter fixture command budget exhausted")
    }

    private fun activate(lease: String): String {
        val status = operator(OperatorRequest("status")).status
        return checkNotNull(operator(OperatorRequest("activate", lease, status.epoch, status.serverInstance, status.handoff, status.activeCivilization, UUID.randomUUID().toString())).playerToken)
    }

    private fun command(snapshot: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null, queue: List<String> = emptyList(), destination: TilePosition? = null) = TurnCommandRequest(UUID.randomUUID().toString(), snapshot.epoch, snapshot.revision, kind, target, selection, queue, destination)

    private fun execute(token: String, request: TurnCommandRequest): TurnCommandReceipt = PlayerTurnJson.json.decodeFromString(tool(token, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject).toString())

    private fun turn(token: String): PlayerTurnSnapshot = PlayerTurnJson.json.decodeFromString(tool(token, "player_turn").toString())

    private fun operator(request: OperatorRequest): OperatorResponse = OperatorControl.json.decodeFromString(raw("/operator", OperatorControl.json.encodeToString(request), operatorToken))

    private fun raw(path: String, body: String, token: String): String {
        val response = client.send(
            HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofSeconds(15))
                .header("Authorization", "Bearer $token").header("Content-Type", "application/json")
                .header("Accept", "application/json, text/event-stream")
                .POST(HttpRequest.BodyPublishers.ofString(body)).build(),
            HttpResponse.BodyHandlers.ofString(),
        )
        check(response.statusCode() == 200) { "HTTPS ${response.statusCode()}: ${response.body()}" }
        return response.body()
    }

    private fun tool(token: String, name: String, arguments: JsonObject = JsonObject(emptyMap())): JsonObject {
        val body = """{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"$name","arguments":$arguments}}"""
        val result = Json.parseToJsonElement(raw("/mcp", body, token)).jsonObject.getValue("result").jsonObject
        check(!result.getValue("isError").jsonPrimitive.boolean) { result.toString() }
        return result.getValue("structuredContent").jsonObject
    }
}
