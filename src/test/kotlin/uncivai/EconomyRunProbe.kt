package uncivai

import com.unciv.logic.civilization.Civilization
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
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import uncivai.player.observeEconomy
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Files
import java.nio.file.Path
import java.time.Duration
import java.util.UUID

@Serializable
private data class EconomyFacts(val gold: Int, val units: List<String>, val buildings: List<String>, val purchasedTileOwner: String?, val farmPillaged: Boolean, val iron: Int, val maintenance: Int)

@Serializable
private data class EconomyRecovery(val actions: List<SavedReceipt>, val before: EconomyFacts, val after: EconomyFacts, val activationId: String)

/** Scripted gameplay uses authenticated HTTPS; direct engine reads only arrange and verify the fixture. */
object EconomyRunProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val stage = args[1]
        val checkpoint = root.resolve(if (stage == "prepare") "exploration.json" else "checkpoint.json")
        if (stage == "prepare") require(!Files.exists(checkpoint)) { "Checkpoint already exists" }
        val fresh = stage in setOf("start", "prepare")
        val runtime = if (fresh) HeadlessRuntime.createDuel(root.resolve(stage), 412L, combatScenario = true) else HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve(stage))
        runtime.use {
            if (fresh) {
                arrange(runtime)
                runtime.executor.enableCheckpoints(checkpoint)
            }
            if (stage == "prepare") {
                println("Prepared economy exercise: $checkpoint")
                return
            }
            EconomyApi(runtime, root.resolve("$stage-tls")).use { api ->
                when (stage) {
                    "start" -> start(runtime, api, root)
                    "restore" -> restore(runtime, api, root)
                    "verify" -> verify(runtime, api, root)
                    else -> error("Unknown stage $stage")
                }
            }
        }
    }

    private fun arrange(runtime: HeadlessRuntime) {
        val game = runtime.game
        game.tileMap.mapParameters.name = "Economy exercise"
        val players = listOf("Rome", "Greece").map(game::getCivilization)
        players.forEach { player ->
            player.cities.filterNot { it.isOriginalCapital }.toList().forEach { it.destroyCity() }
            player.units.getCivUnits().toList().forEach { it.destroy() }
            player.addGold(5000 - player.gold)
            for (name in listOf("Bronze Working", "Iron Working")) player.tech.addTechnology(name, false)
            player.tech.techsToResearch = arrayListOf("Writing", "The Wheel", "Mathematics")
            player.cities.single().cityConstructions.constructionQueue.apply {
                clear()
                add("The Pyramids")
            }
        }
        val rome = players.first()
        fun place(name: String, position: HexCoord) = checkNotNull(game.tileMap.placeUnitNearTile(position, name, rome)).also {
            check(it.currentTile.position == position)
            it.currentMovement = it.getMaxMovement().toFloat()
        }
        val iron = game.tileMap[HexCoord(-3, 0)]
        check(iron.getOwner() === rome)
        iron.setTileResource("Iron")
        iron.resourceAmount = 2
        iron.improvement = null
        place("Worker", iron.position)
        place("Warrior", HexCoord(-4, -1))
        val farm = game.tileMap[HexCoord(3, 0)]
        check(farm.getOwner() === players.last())
        farm.improvement = "Farm"
        farm.improvementIsPillaged = false
        place("Warrior", farm.position).health = 1
        // Keep upkeep above free allowances, so disbanding has a measurable economic effect.
        repeat(9) { checkNotNull(game.tileMap.placeUnitNearTile(HexCoord(0, -4), "Warrior", rome)) }
        for (player in players) {
            player.cache.updateCivResources()
            player.cities.forEach { it.cityStats.update() }
            player.units.getCivUnits().forEach { it.updateVisibleTiles(updateCivViewableTiles = false) }
            player.cache.updateViewableTiles()
        }
        check(game.victoryData == null)
    }

    private fun start(runtime: HeadlessRuntime, api: EconomyApi, root: Path) {
        api.acquire()
        var activation = api.activate()
        var token = checkNotNull(activation.playerToken)
        var turn = api.readAlerts(token)
        val worker = turn.map.units.single { it.name == "Worker" }
        check(turn.economy!!.resources.single { it.name == "Iron" }.available == 0)
        val warrior = turn.map.units.single { it.position == TilePosition(-4, -1) }
        check(warrior.economy!!.upgrades.any { !it.available && (it.resourceRequirements["Iron"] ?: 0) > 0 })
        check(worker.worker!!.options.any { it.name == "Mine" && it.available })
        api.execute(token, command(turn, TurnCommandKind.START_WORKER_JOB, worker.handle, "Mine"))
        var worked = false
        repeat(12) {
            if (worked) return@repeat
            api.finishTurn(token)
            api.finishTurn(checkNotNull(api.activate().playerToken))
            activation = api.activate()
            token = checkNotNull(activation.playerToken)
            turn = api.readAlerts(token)
            worked = turn.economy!!.resources.single { it.name == "Iron" }.available == 2
        }
        check(worked) { "Worker never connected the Iron" }
        check(runtime.game.tileMap[HexCoord(-3, 0)].improvement == "Mine")
        val before = facts(runtime)
        val actions = economicActions(runtime, api, token)
        val after = facts(runtime)
        save(root, EconomyRecovery(actions, before, after, checkNotNull(activation.activationId)))
        api.operator(OperatorRequest("release", api.lease))
    }

    private fun economicActions(runtime: HeadlessRuntime, api: EconomyApi, token: String): List<SavedReceipt> {
        val actions = arrayListOf<SavedReceipt>()
        fun act(request: TurnCommandRequest): TurnCommandReceipt {
            val before = facts(runtime)
            val receipt = api.execute(token, request)
            check(receipt.economy != null)
            check(receipt.economy.goldChange == facts(runtime).gold - before.gold)
            val committed = facts(runtime)
            check(api.execute(token, request) == receipt && facts(runtime) == committed)
            api.reject(token, request.copy(confirmAction = !request.confirmAction), "REQUEST_ID_CONFLICT")
            api.reject(token, request.copy(requestId = UUID.randomUUID().toString()), "STALE_REVISION")
            actions += SavedReceipt(request, receipt)
            return receipt
        }
        var turn = api.readAlerts(token)
        var city = turn.cities.single { it.name == "Rome" }
        val tile = city.purchases!!.tiles.single { it.position == TilePosition(-2, 0) }
        check(tile.available)
        val purchase = command(turn, TurnCommandKind.BUY_TILE, city.handle).copy(destination = tile.position, confirmAction = true)
        val unchanged = facts(runtime)
        api.reject(token, purchase.copy(confirmAction = false), "INVALID_ARGUMENT")
        api.reject(token, purchase.copy(destination = TilePosition(900, 900)), "OPTION_UNAVAILABLE")
        api.reject(api.playerToken("Greece"), purchase, "COMMAND_UNAVAILABLE")
        check(facts(runtime) == unchanged)
        check(act(purchase).economy!!.goldChange == -tile.goldCost)
        check(runtime.game.tileMap[HexCoord(-2, 0)].getOwner()?.civName == "Rome")

        turn = api.turn(token)
        city = turn.cities.single { it.name == "Rome" }
        val granary = city.purchases!!.constructions.single { it.name == "Granary" }
        check(granary.available && granary.description.isNotBlank())
        check(act(command(turn, TurnCommandKind.BUY_CONSTRUCTION, city.handle, granary.name).copy(confirmAction = true)).economy!!.goldChange == -granary.goldCost!!)
        check(runtime.game.getCivilization("Rome").cities.single().cityConstructions.isBuilt("Granary"))

        // A receipt revision alone does not acknowledge the next offered price/confirmation.
        val unobserved = actions.last().receipt
        val unseen = command(turn, TurnCommandKind.BUY_CONSTRUCTION, city.handle, "Archer").copy(revision = unobserved.revision, confirmAction = true)
        api.reject(token, unseen, "OBSERVATION_REQUIRED")
        turn = api.turn(token)
        city = turn.cities.single { it.name == "Rome" }
        val archer = city.purchases!!.constructions.single { it.name == "Archer" }
        check(archer.available)
        val recruited = act(command(turn, TurnCommandKind.BUY_CONSTRUCTION, city.handle, archer.name).copy(confirmAction = true))
        check(recruited.economy!!.units.any { it.created && it.name == "Archer" })
        check(runtime.game.getCivilization("Rome").units.getCivUnits().any { it.name == "Archer" })

        turn = api.turn(token)
        val unit = turn.map.units.single { it.position == TilePosition(-4, -1) }
        val upgrade = unit.economy!!.upgrades.first { it.available && (it.resourceRequirements["Iron"] ?: 0) > 0 }
        val upgraded = act(command(turn, TurnCommandKind.UPGRADE_UNIT, unit.handle, upgrade.targetName))
        check(upgraded.economy!!.goldChange == -upgrade.goldCost)
        turn = api.turn(token)
        check(turn.map.units.single { it.handle == unit.handle }.name == upgrade.targetName)
        check(turn.economy!!.resources.single { it.name == "Iron" }.available < 2)

        val pillager = turn.map.units.single { it.position == TilePosition(3, 0) }
        val pillage = pillager.economy!!.pillage!!
        check(pillage.available && pillage.confirmation != null)
        val pillaged = act(command(turn, TurnCommandKind.PILLAGE, pillager.handle).copy(confirmAction = true))
        check(runtime.game.tileMap[HexCoord(3, 0)].improvementIsPillaged)
        check(pillaged.economy!!.units.single { it.handle == pillager.handle }.health > pillager.health) { "Pillage did not heal the injured unit (previous health ${pillager.health})" }

        // Disband enough of the reserve to cross displayed maintenance rounding boundaries.
        turn = api.turn(token)
        val maintenance = turn.economy!!.unitMaintenance
        val reserves = turn.map.units.filter { it.name == "Warrior" && it.position != TilePosition(3, 0) }.take(4)
        check(reserves.size == 4)
        for (reserve in reserves) {
            turn = api.turn(token)
            val option = turn.map.units.single { it.handle == reserve.handle }.economy!!.disband!!
            val receipt = act(command(turn, TurnCommandKind.DISBAND_UNIT, reserve.handle).copy(confirmAction = true))
            check(receipt.economy!!.goldChange == option.gold)
            check(receipt.economy.units.single { it.handle == reserve.handle }.removed)
        }
        check(api.turn(token).economy!!.unitMaintenance < maintenance) { "Disbanded units did not reduce displayed maintenance" }
        return actions
    }

    private fun restore(runtime: HeadlessRuntime, api: EconomyApi, root: Path) {
        val saved = expected(root)
        check(facts(runtime) == saved.after)
        val stable = Files.readString(root.resolve("checkpoint.json"))
        for (action in saved.actions) check(api.execute(api.playerToken("Rome"), action.request) == action.receipt)
        check(Files.readString(root.resolve("checkpoint.json")) == stable)
        api.acquire()
        val status = api.status()
        api.operator(OperatorRequest("rollback", api.lease, status.epoch, status.serverInstance, status.handoff, "Rome", saved.activationId))
        check(facts(runtime) == saved.before) { "Rollback did not restore gold, units, tiles, buildings and resources" }
        val activation = api.activate()
        val token = checkNotNull(activation.playerToken)
        api.reject(token, saved.actions.first().request, "STALE_REVISION")
        val actions = economicActions(runtime, api, token)
        api.finishTurn(token)
        api.finishTurn(checkNotNull(api.activate().playerToken))
        save(root, EconomyRecovery(actions, saved.before, facts(runtime), checkNotNull(activation.activationId)))
        api.operator(OperatorRequest("release", api.lease))
    }

    private fun verify(runtime: HeadlessRuntime, api: EconomyApi, root: Path) {
        val saved = expected(root)
        check(facts(runtime) == saved.after)
        val before = Files.readString(root.resolve("checkpoint.json"))
        for (action in saved.actions) {
            check(api.execute(api.playerToken("Rome"), action.request) == action.receipt)
            api.reject(api.playerToken("Greece"), action.request, "COMMAND_UNAVAILABLE")
        }
        check(Files.readString(root.resolve("checkpoint.json")) == before)
        api.acquire()
        api.finishTurn(checkNotNull(api.activate().playerToken))
        check(api.status().activeCivilization == "Greece")
    }

    private fun facts(runtime: HeadlessRuntime): EconomyFacts {
        val player = runtime.game.getCivilization("Rome")
        val economy = observeEconomy(player)
        return EconomyFacts(
            player.gold,
            player.units.getCivUnits().sortedBy { it.id }.map { "${it.id}:${it.name}:${it.health}:${it.currentMovement}:${it.currentTile.position}:${it.action}:${it.promotions.XP}:${it.promotions.promotions.sorted()}" }.toList(),
            player.cities.single().cityConstructions.getBuiltBuildings().map { it.name }.sorted().toList(),
            runtime.game.tileMap[HexCoord(-2, 0)].getOwner()?.civName,
            runtime.game.tileMap[HexCoord(3, 0)].improvementIsPillaged,
            economy.resources.single { it.name == "Iron" }.available,
            economy.unitMaintenance,
        )
    }

    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection).reviewedFor(turn)
    private fun save(root: Path, value: EconomyRecovery) = Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(value))
    private fun expected(root: Path) = PlayerTurnJson.json.decodeFromString<EconomyRecovery>(Files.readString(root.resolve("expected.json")))

    private class EconomyApi(runtime: HeadlessRuntime, tlsRoot: Path) : AutoCloseable {
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
                    else -> error("Unsupported economy fixture decision: ${turn.pending}")
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
        private fun tool(token: String, name: String, args: JsonObject = JsonObject(emptyMap())): JsonObject {
            val response = call(token, name, args)
            check(!response.getValue("isError").jsonPrimitive.boolean) { response.toString() }
            return response.getValue("structuredContent").jsonObject
        }
        override fun close() {
            server.close()
            client.close()
        }
    }
}
