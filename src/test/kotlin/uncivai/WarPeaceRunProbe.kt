package uncivai

import com.unciv.json.json
import com.unciv.logic.map.HexCoord
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
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
private data class WarPeaceRecovery(
    val epoch: String,
    val handoff: Long,
    val snapshot: PlayerTurnSnapshot,
    val offer: JsonObject,
    val acceptance: JsonObject? = null,
    val receipt: JsonObject? = null,
    val romanTreasury: Int? = null,
    val greekTreasury: Int? = null,
)

/** Trusted fixture setup only; all play and scheduling thereafter use verified HTTPS observations. */
object WarPeaceRunProbe {
    private lateinit var client: HttpClient
    private lateinit var base: String
    private lateinit var lease: String
    private lateinit var epoch: String
    private val operatorToken = "O".repeat(43)
    private val playerTokens = mapOf("Rome" to "R".repeat(43), "Greece" to "G".repeat(43))
    private var warMessagesRead = 0

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val stage = args[1]
        val tls = TlsTestFixture(Files.createDirectories(root.resolve("$stage-tls")))
        client = HttpClient.newBuilder().sslContext(tls.context).connectTimeout(Duration.ofSeconds(5)).build()
        val checkpoint = root.resolve("checkpoint.json")
        val runtime = if (stage == "offer") {
            HeadlessRuntime.createDuel(root.resolve("initial"), 412L)
        } else {
            HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve(stage))
        }
        try {
            runtime.use {
                if (stage == "offer") {
                    arrangeUnmetPlayers(runtime)
                    runtime.executor.enableCheckpoints(checkpoint)
                }
                val host = PlayerHost(runtime)
                val control = OperatorControl(runtime.executor)
                val bindings = playerTokens.map { (civ, token) -> PlayerMcpBinding(token, host.bindPlayer(civ)) }
                PlayerMcpServer(bindings, 0, OperatorMcpBinding(operatorToken, control), tls.tls).use { server ->
                    base = "https://localhost:${server.start()}"
                    val saved = if (stage == "offer") null else PlayerTurnJson.json.decodeFromString<WarPeaceRecovery>(Files.readString(root.resolve("expected.json")))
                    val status = operator(OperatorRequest("status")).status
                    if (saved != null) {
                        check(status.epoch == saved.epoch && status.handoff == saved.handoff)
                        check(status.phase == "PAUSED" && status.activeCivilization == "Rome" && status.leaseId == null)
                    }
                    lease = UUID.randomUUID().toString()
                    operator(OperatorRequest("acquire", lease))
                    epoch = status.epoch
                    val token = activate()
                    when (stage) {
                        "offer" -> negotiate(root, token)
                        "accept" -> accept(root, token, checkNotNull(saved))
                        "continue" -> continuePlaying(runtime, token, checkNotNull(saved))
                        else -> error("Unknown war and peace fixture stage: $stage")
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
        check(PlayerHost(runtime).bindPlayer("Rome").turn().scenario == null)
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
        civilizations.forEach {
            it.addGold(500)
            it.cache.updateViewableTiles()
        }
        check(civilizations.all { it.diplomacy.isEmpty() })
        game.tileMap.mapParameters.name = "Close-start duel"
    }

    private fun negotiate(root: Path, firstToken: String) {
        var token = firstToken
        val before = turn(token)
        check(before.diplomacy.knownCivilizations.isEmpty())
        check(before.scenario?.name == "CLOSE_START_DIPLOMACY" && before.scenario.phase == "AWAITING_WAR")
        reject(token, command(before, TurnCommandKind.DECLARE_WAR, "Greece", confirmWar = true))
        val warrior = before.map.units.single { it.name == "Warrior" }
        val move = warrior.moves.single { it.destination == TilePosition(1, 0) }
        execute(token, command(before, TurnCommandKind.MOVE_UNIT, warrior.handle, destination = move.destination))
        var snapshot = readAndAcknowledgeAlerts(token)
        val target = snapshot.diplomacy.knownCivilizations.single()
        check(target.civilizationId == "Greece" && target.declareWar.available && !target.atWar)
        check(target.declarationConfirmation.any { it.contains("Greece") })
        reject(token, command(snapshot, TurnCommandKind.DECLARE_WAR, "Missing civilization", confirmWar = true))
        reject(token, command(snapshot, TurnCommandKind.DECLARE_WAR, "Greece"))
        reject(token, command(snapshot, TurnCommandKind.DECLARE_WAR, "Greece", confirmWar = true).copy(selection = "Unexpected"))
        val declaration = command(snapshot, TurnCommandKind.DECLARE_WAR, "Greece", confirmWar = true)
        val declarationReceipt = execute(token, declaration)
        val response = checkNotNull(declarationReceipt.feedback)
        check(response.title.isNotBlank() && response.paragraphs.any { it.isNotBlank() } && response.acknowledgement.isNotBlank())
        check(execute(token, declaration) == declarationReceipt)
        snapshot = turn(token)
        check(snapshot.diplomacy.knownCivilizations.single().atWar)
        check(snapshot.scenario?.phase == "AT_WAR")
        check(!snapshot.diplomacy.knownCivilizations.single().negotiatePeace.available)
        check(!tradeOptions("Rome", "Greece")["available"]!!.jsonPrimitive.boolean)
        val earlyTreaty = obj("type" to "Treaty", "name" to "Peace Treaty", "amount" to 1, "duration" to 10)
        val blocked = communication("Rome", "trade_offer", "partner" to "Greece", "give" to listOf(earlyTreaty), "receive" to listOf(earlyTreaty))
        check(blocked["reason"]!!.jsonPrimitive.content.contains("more turns"))
        val waiting = turn(playerTokens.getValue("Greece"))
        check(waiting.scenario?.phase == "AT_WAR")
        val war = waiting.alerts.single { it.kind == "WarDeclaration" }
        val warContent = checkNotNull(war.content)
        check(!war.canAcknowledge && warContent.paragraphs.any { it.contains("DECLARATION OF WAR") })
        check(warContent.paragraphs.size >= 2 && warContent.additionalAcknowledgements.isNotEmpty())
        reject(playerTokens.getValue("Greece"), command(waiting, TurnCommandKind.ACKNOWLEDGE_ALERT, war.handle))

        // The actual minimum-war timer elapses through ordinary scheduled turns.
        var cooldownTurns = 0
        while (true) {
            snapshot = turn(token)
            val current = operator(OperatorRequest("status")).status
            if (current.activeCivilization == "Rome" && snapshot.diplomacy.knownCivilizations.single().negotiatePeace.available) break
            if (!snapshot.diplomacy.knownCivilizations.single().negotiatePeace.available) {
                check(snapshot.diplomacy.knownCivilizations.single().negotiatePeace.unavailableReasons.isNotEmpty())
                cooldownTurns++
            }
            finishTurn(token)
            token = activate()
            check(cooldownTurns < 40) { "Peace cooldown did not expire" }
        }
        check(cooldownTurns >= 2 && warMessagesRead == 1)

        // All proposal decisions below run while Rome owns the gameplay turn.
        val terms = tradeOptions("Rome", "Greece")
        val treaty = selection(terms, "give", "Treaty")
        val gold = selection(terms, "give", "Gold", 25)
        fun offer() = communication("Rome", "trade_offer", "partner" to "Greece", "give" to listOf(treaty), "receive" to listOf(treaty)).getValue("trade").jsonObject
        val withdrawn = offer()
        decide("Rome", withdrawn, "withdraw")
        check(decide("Greece", withdrawn, "accept")["error"] != null)
        val declined = offer()
        decide("Greece", declined, "decline")
        check(decide("Greece", declined, "accept")["error"] != null)
        val initial = communication("Greece", "trade_offer", "partner" to "Rome", "give" to listOf(treaty), "receive" to listOf(treaty)).getValue("trade").jsonObject
        val counter = communication("Rome", "trade_offer", "partner" to "Greece", "proposal" to initial["proposal"], "version" to initial["version"], "give" to listOf(treaty, gold), "receive" to listOf(treaty)).getValue("trade").jsonObject
        check(counter.getValue("version").jsonPrimitive.int == 2)
        check(decide("Rome", initial, "accept")["error"] != null)
        check(communication("Greece", "trade_read", "proposal" to counter["proposal"])["trade"]!!.jsonObject["clauses"] == counter["clauses"])
        check(turn(token).pending.none { it.kind == "TradeRequests" })
        Files.writeString(
            root.resolve("expected.json"),
            PlayerTurnJson.json.encodeToString(
                WarPeaceRecovery(epoch, operator(OperatorRequest("status")).status.handoff, turn(token), counter),
            ),
        )
    }

    private fun accept(root: Path, token: String, saved: WarPeaceRecovery) {
        val offer = communication("Greece", "trade_read", "proposal" to saved.offer["proposal"])["trade"]!!.jsonObject
        check(offer["clauses"] == saved.offer["clauses"] && offer["version"] == saved.offer["version"])
        check(offer["acceptAvailable"]!!.jsonPrimitive.boolean)
        val romanBefore = treasury(token)
        val greekBefore = treasury(playerTokens.getValue("Greece"))
        val request = obj("op" to "trade_decide", "epoch" to epoch, "requestId" to "peace-accept", "proposal" to offer["proposal"], "version" to offer["version"], "decision" to "accept")
        val receipt = tool(playerTokens.getValue("Greece"), "player_communication", request)
        check(receipt["trade"]!!.jsonObject["status"]!!.jsonPrimitive.content == "ENACTED")
        check(receipt["outcome"]!!.jsonObject["feedback"]!!.jsonObject["paragraphs"]!!.jsonArray.isNotEmpty())
        check(operator(OperatorRequest("status")).status.let { it.activeCivilization == "Rome" && it.handoff == saved.handoff })
        val romanAfter = treasury(token)
        val greekAfter = treasury(playerTokens.getValue("Greece"))
        check(romanAfter == romanBefore - 25 && greekAfter == greekBefore + 25)
        for (playerToken in playerTokens.values) {
            val after = turn(playerToken)
            val known = after.diplomacy.knownCivilizations.single()
            check(!known.atWar && known.peaceTreatyTurns > 0 && !known.declareWar.available)
            check(known.declareWar.unavailableReasons.isNotEmpty())
            check(after.scenario?.phase == "COMPLETE")
            check(after.pending.none { it.kind == "TradeRequests" })
        }
        Files.writeString(
            root.resolve("expected.json"),
            PlayerTurnJson.json.encodeToString(
                saved.copy(snapshot = turn(token), acceptance = request, receipt = receipt, romanTreasury = romanAfter, greekTreasury = greekAfter),
            ),
        )
    }

    private fun continuePlaying(runtime: HeadlessRuntime, firstToken: String, saved: WarPeaceRecovery) {
        val beforeReplay = json().toJson(runtime.game)
        val greekToken = playerTokens.getValue("Greece")
        check(tool(greekToken, "player_communication", checkNotNull(saved.acceptance)) == saved.receipt)
        check(decide("Greece", saved.offer, "accept")["outcome"] == saved.receipt!!["outcome"])
        check(json().toJson(runtime.game) == beforeReplay) { "Acceptance retry repeated peace effects" }
        val rome = runtime.game.getCivilization("Rome")
        val greece = runtime.game.getCivilization("Greece")
        check(rome.gold == saved.romanTreasury && greece.gold == saved.greekTreasury)
        val native = rome.getDiplomacyManager(greece)!!.trades.single()
        check(native.reverse().equalTrade(greece.getDiplomacyManager(rome)!!.trades.single()))
        check(native.ourOffers.single { it.type.name == "Gold" }.amount == 25)
        check(rome.tradeRequests.isEmpty() && greece.tradeRequests.isEmpty())
        reject(firstToken, command(turn(firstToken), TurnCommandKind.DECLARE_WAR, "Greece", confirmWar = true))
        var token = firstToken
        repeat(8) { index ->
            if (index > 0) token = activate()
            val snapshot = turn(token)
            check(snapshot.scenario?.phase == "COMPLETE")
            check(snapshot.diplomacy.knownCivilizations.single().let { !it.atWar && it.peaceTreatyTurns > 0 && !it.declareWar.available })
            check(snapshot.pending.none { it.kind == "TradeRequests" })
            finishTurn(token)
        }
        check(operator(OperatorRequest("status")).status.handoff == saved.handoff + 8)
        check(operator(OperatorRequest("status")).status.turn == saved.snapshot.turn + 4)
    }

    private fun communication(civ: String, op: String, vararg fields: Pair<String, Any?>): JsonObject {
        val request = obj("op" to op, "epoch" to epoch, *fields)
        val mutation = op in setOf("trade_offer", "trade_decide")
        return tool(playerTokens.getValue(civ), "player_communication", if (mutation) JsonObject(request + obj("requestId" to UUID.randomUUID().toString())) else request)
    }
    private fun tradeOptions(civ: String, partner: String) = communication(civ, "trade_options", "partner" to partner)
    private fun selection(options: JsonObject, side: String, type: String, amount: Int = 1): JsonObject {
        val term = options.getValue(side).jsonArray.map { it.jsonObject }.single { it["type"]!!.jsonPrimitive.content == type }
        return obj("type" to term["type"], "name" to term["name"], "amount" to amount, "duration" to term["duration"])
    }
    private fun decide(civ: String, offer: JsonObject, decision: String) = communication(civ, "trade_decide", "proposal" to offer["proposal"], "version" to offer["version"], "decision" to decision)

    private fun readAndAcknowledgeAlerts(token: String): PlayerTurnSnapshot {
        repeat(12) {
            val snapshot = turn(token)
            val alert = snapshot.alerts.firstOrNull { it.canAcknowledge } ?: return snapshot
            val content = checkNotNull(alert.content)
            check(content.title.isNotBlank() && content.paragraphs.isNotEmpty() && content.acknowledgement.isNotBlank())
            if (alert.kind == "WarDeclaration") {
                check(content.paragraphs.size >= 2 && content.additionalAcknowledgements.isNotEmpty())
                warMessagesRead++
            }
            execute(token, command(snapshot, TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle))
        }
        error("Alert fixture budget exhausted")
    }

    private fun finishTurn(token: String) {
        operator(OperatorRequest("heartbeat", lease))
        val before = operator(OperatorRequest("status")).status
        repeat(40) {
            val snapshot = readAndAcknowledgeAlerts(token)
            check(snapshot.pending.none { !it.supported }) { "Unsupported war and peace decision: ${snapshot.pending}" }
            val request = when {
                snapshot.freeTechnologies > 0 -> command(snapshot, TurnCommandKind.CHOOSE_FREE_TECHNOLOGY).copy(selection = snapshot.researchOptions.first())
                snapshot.freeGreatPeople > 0 -> command(snapshot, TurnCommandKind.CHOOSE_FREE_GREAT_PERSON).copy(selection = snapshot.greatPersonOptions.first())
                snapshot.researchQueue.isEmpty() -> command(snapshot, TurnCommandKind.SET_RESEARCH_QUEUE).copy(queue = listOf(snapshot.researchOptions.first()))
                snapshot.pending.any { it.kind == "Policy" } && snapshot.policyOptions.isNotEmpty() -> command(snapshot, TurnCommandKind.ADOPT_POLICY).copy(selection = snapshot.policyOptions.first())
                snapshot.canEndTurn -> command(snapshot, TurnCommandKind.END_TURN)
                else -> error("War and peace fixture blocked: ${snapshot.pending}")
            }
            val receipt = execute(token, request)
            if (request.kind == TurnCommandKind.END_TURN) {
                check(execute(token, request) == receipt)
                val after = operator(OperatorRequest("status")).status
                check(after.handoff == before.handoff + 1 && after.activeCivilization != before.activeCivilization)
                return
            }
        }
        error("War and peace fixture command budget exhausted")
    }

    private fun activate(): String {
        operator(OperatorRequest("heartbeat", lease))
        val status = operator(OperatorRequest("status")).status
        return checkNotNull(
            operator(
                OperatorRequest(
                    "activate",
                    lease,
                    status.epoch,
                    status.serverInstance,
                    status.handoff,
                    status.activeCivilization,
                    UUID.randomUUID().toString(),
                ),
            ).playerToken,
        )
    }

    private fun command(
        snapshot: PlayerTurnSnapshot,
        kind: TurnCommandKind,
        target: String? = null,
        destination: TilePosition? = null,
        confirmWar: Boolean = false,
    ) = TurnCommandRequest(
        UUID.randomUUID().toString(),
        snapshot.epoch,
        snapshot.revision,
        kind,
        target = target,
        destination = destination,
        confirmWar = confirmWar,
    ).reviewedFor(snapshot)

    private fun execute(token: String, request: TurnCommandRequest): TurnCommandReceipt = PlayerTurnJson.json.decodeFromString(
        tool(token, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject).toString(),
    )

    private fun reject(token: String, request: TurnCommandRequest) {
        tool(token, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject, expectError = true)
    }

    private fun turn(token: String): PlayerTurnSnapshot = PlayerTurnJson.json.decodeFromString(tool(token, "player_turn").toString())
    private fun treasury(token: String) = tool(token, "player_summary").getValue("treasury").jsonPrimitive.int
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

    private fun tool(token: String, name: String, arguments: JsonObject = JsonObject(emptyMap()), expectError: Boolean = false): JsonObject {
        val body = """{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"$name","arguments":$arguments}}"""
        val result = Json.parseToJsonElement(raw("/mcp", body, token)).jsonObject.getValue("result").jsonObject
        check(name == "player_communication" || result.getValue("isError").jsonPrimitive.boolean == expectError) { result.toString() }
        return if (expectError) result else result.getValue("structuredContent").jsonObject
    }
}
