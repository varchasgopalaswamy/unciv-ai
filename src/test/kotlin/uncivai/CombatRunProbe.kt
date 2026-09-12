package uncivai

import com.unciv.json.json
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.mapunit.UnitSettlement
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.mcp.OperatorMcpBinding
import uncivai.mcp.PlayerMcpBinding
import uncivai.mcp.PlayerMcpServer
import uncivai.player.AttackOption
import uncivai.player.CombatReport
import uncivai.player.PeaceTerm
import uncivai.player.PeaceTermSelection
import uncivai.player.PeaceTerms
import uncivai.player.PlayerFinalState
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
private data class CombatCommand(val civilization: String, val request: TurnCommandRequest, val receipt: TurnCommandReceipt)

@Serializable
private data class CombatUnitFacts(val civilization: String, val id: Int, val name: String, val health: Int, val experience: Int, val promotions: List<String>, val position: TilePosition, val action: String?)

@Serializable
private data class CombatRecovery(
    val commands: List<CombatCommand>,
    val units: List<CombatUnitFacts>,
    val reports: Map<String, List<CombatReport>>,
    val handoff: Long,
    val greekArcherHealth: Int = 0,
    val results: Map<String, PlayerFinalState> = emptyMap(),
    val activationId: String? = null,
)

/** Trusted scenario arrangement and save inspection; all gameplay and scheduling use HTTPS. */
object CombatRunProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val stage = args[1]
        if (stage == "prepare-conquest" || stage == "prepare-victory") {
            if (stage == "prepare-conquest") prepareConquest(root) else prepareVictory(root)
            return
        }
        val fresh = stage == "combat" || stage == "conquest" || stage.startsWith("last-settler-")
        val checkpoint = root.resolve("checkpoint.json")
        val runtime = when {
            fresh -> HeadlessRuntime.createDuel(root.resolve(stage), 412L)
            stage == "unrecorded-final" -> HeadlessRuntime.load(root.resolve("unrecorded-victory.json"), root.resolve(stage))
            else -> HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve(stage))
        }
        runtime.use {
            if (fresh) {
                arrange(runtime, conquest = stage == "conquest", lastSettler = stage.startsWith("last-settler-"))
                runtime.executor.enableCheckpoints(checkpoint)
            }
            CombatApi(runtime, root.resolve("$stage-tls")).use { api ->
                when (stage) {
                    "combat" -> combat(runtime, api, root)
                    "restore" -> restore(runtime, api, root)
                    "verify" -> verify(runtime, api, root)
                    "conquest" -> conquer(runtime, api, root)
                    "last-settler-attack", "last-settler-move" -> captureLastSettler(runtime, api, root, move = stage.endsWith("move"))
                    "final" -> verifyFinal(runtime, api, root)
                    "unrecorded-final" -> verifyUnrecordedFinal(runtime, api, root)
                    else -> error("Unknown combat test stage: $stage")
                }
            }
        }
    }

    /** Reuses the verified capture fixture, leaving every gameplay decision to its players. */
    private fun prepareConquest(root: Path) {
        val checkpoint = root.resolve("exploration.json")
        require(!Files.exists(checkpoint)) { "Refusing to overwrite an existing conquest checkpoint: $checkpoint" }
        HeadlessRuntime.createDuel(root.resolve("fixture-data"), 412L).use { runtime ->
            arrange(runtime, conquest = true)
            runtime.game.tileMap.mapParameters.name = "Combat exercise"
            val rome = runtime.game.getCivilization("Rome")
            val greece = runtime.game.getCivilization("Greece")
            check(runtime.game.currentPlayerCiv === rome && rome.isAtWarWith(greece))
            check(greece.cities.size == 2 && greece.cities.all { it.health == 1 })
            val snapshot = PlayerHost(runtime).bindPlayer("Rome").turn()
            check(snapshot.map.units.count { unit -> unit.combat!!.attacks.any { it.defenderIsCity } } == 2)
            check(snapshot.scenario?.name == "COMBAT_EXERCISE")
            prepareDuelCredentials(root)
            runtime.executor.enableCheckpoints(checkpoint)
            println("Prepared conquest checkpoint: ${checkpoint.toAbsolutePath()}")
            println("Rome acts first; capture the Greek outpost at (0, 0), choose Puppet then Annex, and capture Athens at (4, 0).")
        }
    }

    /** A single legal attack followed by the ordinary capital disposition completes this game. */
    private fun prepareVictory(root: Path) {
        val checkpoint = root.resolve("exploration.json")
        require(!Files.exists(checkpoint)) { "Refusing to overwrite an existing victory checkpoint: $checkpoint" }
        HeadlessRuntime.createDuel(root.resolve("fixture-data"), 412L).use { runtime ->
            arrange(runtime, conquest = true)
            val game = runtime.game
            val rome = game.getCivilization("Rome")
            val greece = game.getCivilization("Greece")
            greece.cities.filterNot { it.isOriginalCapital }.toList().forEach { it.destroyCity() }
            greece.units.getCivUnits().toList().forEach { it.destroy() }
            rome.units.getCivUnits().filterNot { it.currentTile.position == HexCoord(3, 0) }.toList().forEach { it.destroy() }
            game.tileMap.mapParameters.name = "Victory exercise"
            val players = listOf(rome, greece)
            for (tile in game.tileMap.values) players.forEach { tile.setExplored(it, false) }
            players.forEach { player ->
                for (technology in listOf("Pottery", "Archery", "Mining")) player.tech.addTechnology(technology, false)
                player.tech.techsToResearch = arrayListOf("Writing", "The Wheel", "Bronze Working")
                for (city in player.cities) {
                    city.cityConstructions.constructionQueue.clear()
                    city.cityConstructions.addToQueue("Archer")
                    city.cityConstructions.addToQueue("Monument")
                    city.cityStats.update()
                }
                player.units.getCivUnits().forEach { it.updateVisibleTiles(updateCivViewableTiles = false) }
                player.cache.updateViewableTiles()
            }
            val capital = greece.cities.single()
            val warrior = rome.units.getCivUnits().single()
            check(rome.cities.size == 1 && capital.isOriginalCapital && capital.health == 1)
            check(capital.location == HexCoord(4, 0) && warrior.currentTile.position == HexCoord(3, 0) && warrior.health == 100)
            check(greece.units.getCivUnits().none() && game.currentPlayerCiv === rome && rome.isAtWarWith(greece))
            check(game.victoryData == null && players.none { it.victoryManager.hasWon() })
            val snapshot = PlayerHost(runtime).bindPlayer("Rome").turn()
            val unit = snapshot.map.units.single()
            val attacks = unit.combat!!.attacks
            check(attacks.map { it.destination }.distinct() == listOf(TilePosition(4, 0)))
            check(attacks.count { it.defenderIsCity && it.attackFrom == unit.position } == 1)
            check(snapshot.researchQueue.isNotEmpty() && snapshot.cities.single().constructionQueue.isNotEmpty())
            prepareDuelCredentials(root)
            runtime.executor.enableCheckpoints(checkpoint)
            println("Prepared victory checkpoint: ${checkpoint.toAbsolutePath()}")
            println("Rome's Warrior at (3, 0) can directly attack Athens at (4, 0), read the capture decision and Annex it to win on turn 0.")
        }
    }

    private fun arrange(runtime: HeadlessRuntime, conquest: Boolean, lastSettler: Boolean = false) {
        val game = runtime.game
        val players = listOf("Rome", "Greece").map(game::getCivilization)
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
            players.forEach { tile.setExplored(it, false) }
        }
        for (player in players) {
            val settler = player.units.getCivUnits().single { it.name == "Settler" }
            val warrior = player.units.getCivUnits().single { it.name == "Warrior" }
            place(
                settler,
                game.tileMap[
                    when (player.civName) {
                        "Greece" -> HexCoord(4, 0)
                        else -> if (conquest) HexCoord(-4, 0) else HexCoord(-2, -1)
                    },
                ],
            )
            if (lastSettler && player.civName == "Greece") {
                place(settler, game.tileMap[HexCoord(1, 0)])
                warrior.destroy()
                continue
            }
            checkNotNull(UnitSettlement.tryFoundCity(settler))
            val warriorPosition = when {
                conquest && player.civName == "Rome" -> HexCoord(-1, 0)
                conquest -> HexCoord(4, 1)
                player.civName == "Rome" -> HexCoord(0, 0)
                else -> HexCoord(1, 0)
            }
            place(warrior, game.tileMap[warriorPosition])
            player.addGold(500)
        }
        val rome = players.first()
        val greece = players.last()
        if (conquest) {
            val settler = checkNotNull(game.tileMap.placeUnitNearTile(HexCoord(0, 0), "Settler", greece))
            checkNotNull(UnitSettlement.tryFoundCity(settler))
            place(rome.units.getCivUnits().single { it.name == "Warrior" }, game.tileMap[HexCoord(-1, 0)])
            val second = checkNotNull(game.tileMap.placeUnitNearTile(HexCoord(3, 0), "Warrior", rome))
            place(second, game.tileMap[HexCoord(3, 0)])
            greece.cities.forEach { it.health = 1 }
        } else if (!lastSettler) {
            checkNotNull(game.tileMap.placeUnitNearTile(HexCoord(-1, -1), "Archer", rome))
            checkNotNull(game.tileMap.placeUnitNearTile(HexCoord(0, -1), "Archer", greece))
            rome.units.getCivUnits().single { it.name == "Warrior" }.promotions.XP = 9
            val remote = game.tileMap.tileList.maxBy { tile -> rome.units.getCivUnits().minOf { it.currentTile.aerialDistanceTo(tile) } }
            checkNotNull(game.tileMap.placeUnitNearTile(remote.position, "Scout", greece))
        }
        rome.diplomacyFunctions.makeCivilizationsMeet(greece)
        rome.getDiplomacyManager(greece)!!.declareWar()
        players.forEach { player ->
            player.units.getCivUnits().forEach { it.updateVisibleTiles(updateCivViewableTiles = false) }
            player.cache.updateViewableTiles()
        }
    }

    private fun place(unit: MapUnit, tile: com.unciv.logic.map.tile.Tile) {
        unit.removeFromTile()
        unit.currentTile = tile
        if (unit.isCivilian()) tile.civilianUnit = unit else tile.militaryUnit = unit
        unit.currentMovement = unit.getMaxMovement().toFloat()
        unit.updateVisibleTiles(updateCivViewableTiles = false)
    }

    private fun combat(runtime: HeadlessRuntime, api: CombatApi, root: Path) {
        api.acquire()
        var activation = api.activate()
        var token = checkNotNull(activation.playerToken)
        val originalUnits = facts(runtime)
        val rejectedTurn = api.turn(token)
        val warrior = rejectedTurn.map.units.single { it.name == "Warrior" }
        val offer = checkNotNull(warrior.combat).attacks.first()
        val valid = attack(rejectedTurn, warrior.handle, offer)
        val enemySnapshot = api.turn(api.playerToken("Greece"))
        val enemyHandle = enemySnapshot.map.units.single { it.name == "Warrior" }.handle
        val hidden = runtime.game.getCivilization("Greece").units.getCivUnits().single { it.name == "Scout" }
        check(!hidden.isVisibleTo(runtime.game.getCivilization("Rome")))
        val unchanged = json().toJson(runtime.game)
        api.reject(token, valid.copy(revision = "stale-revision"))
        api.reject(token, valid.copy(target = "unit999"))
        api.reject(token, valid.copy(destination = TilePosition(hidden.currentTile.position.x, hidden.currentTile.position.y)))
        api.reject(api.playerToken("Greece"), valid.copy(target = enemyHandle))
        check(json().toJson(runtime.game) == unchanged)

        val discarded = romanCombat(api, token)
        check(facts(runtime) != originalUnits)
        check(api.turn(api.playerToken("Greece")).combatReports.size >= 3)
        val discardedReportIds = allReports(runtime).mapValues { (_, reports) -> reports.map { it.id }.toSet() }
        val status = api.status()
        val rollback = OperatorRequest(
            "rollback",
            api.lease,
            status.epoch,
            status.serverInstance,
            status.handoff,
            status.activeCivilization,
            activation.activationId,
        )
        api.operator(rollback)
        check(facts(runtime) == originalUnits) { "Interrupted combat rollback retained damage, experience or movement" }
        check(allReports(runtime).values.all { it.isEmpty() }) { "Interrupted battle reports survived rollback" }
        check(discarded.none { runtime.executor.turnSessions["Rome"]?.receipts?.containsKey(it.request.requestId) == true })
        val reset = json().toJson(runtime.game)
        api.operator(rollback)
        check(json().toJson(runtime.game) == reset)

        activation = api.activate()
        token = checkNotNull(activation.playerToken)
        val committed = romanCombat(api, token).toMutableList()
        check(committed.map { it.receipt.combat } == discarded.map { it.receipt.combat }) { "Rollback rerolled combat damage" }
        api.finishTurn(token)
        token = checkNotNull(api.activate().playerToken)
        var snapshot = api.readAlerts(token)
        check(snapshot.combatReports.size >= 3)
        check(snapshot.combatReports.all { it.id.matches(Regex("report[1-9][0-9]*")) && it.locations.isNotEmpty() })
        check(snapshot.combatReports.none { it.id in discardedReportIds.getValue("Greece") }) { "Rollback recycled combat report identifiers" }
        check(snapshot.combatReports.any { it.damageReceived != null && it.damageReceived > 0 })
        val greekWarrior = snapshot.map.units.single { it.name == "Warrior" }
        val counter = checkNotNull(greekWarrior.combat).attacks.first { it.destination == TilePosition(0, 0) }
        committed += api.record("Greece", token, attack(snapshot, greekWarrior.handle, counter))
        check(committed.last().receipt.combat!!.damageToDefender > 0)
        snapshot = api.turn(token)
        val archer = snapshot.map.units.single { it.name == "Archer" }
        check(archer.health < 100)
        val heal = checkNotNull(archer.combat).orders.first { it.available && it.name.endsWith("UNTIL_HEALED") }
        committed += api.record("Greece", token, command(snapshot, TurnCommandKind.SET_UNIT_ORDER, archer.handle, heal.name))
        api.finishTurn(token)
        check(api.turn(api.playerToken("Rome")).combatReports.isNotEmpty())
        save(root, CombatRecovery(committed, facts(runtime), allReports(runtime), api.status().handoff, archer.health))
    }

    private fun romanCombat(api: CombatApi, token: String): List<CombatCommand> {
        val result = mutableListOf<CombatCommand>()
        var snapshot = api.readAlerts(token)
        val city = snapshot.cities.single()
        val bombard = city.attacks.first { it.destination == TilePosition(0, -1) }
        result += api.record("Rome", token, command(snapshot, TurnCommandKind.CITY_ATTACK, city.handle).copy(destination = bombard.destination))
        snapshot = api.turn(token)
        val archer = snapshot.map.units.single { it.name == "Archer" }
        result += api.record(
            "Rome",
            token,
            attack(
                snapshot,
                archer.handle,
                archer.combat!!.attacks.first {
                    it.destination == TilePosition(1, 0) && it.attackFrom == archer.position
                },
            ),
        )
        snapshot = api.turn(token)
        val warrior = snapshot.map.units.single { it.name == "Warrior" }
        result += api.record(
            "Rome",
            token,
            attack(
                snapshot,
                warrior.handle,
                warrior.combat!!.attacks.first {
                    it.destination == TilePosition(1, 0) && it.attackFrom == warrior.position
                },
            ),
        )
        check(result.all { it.receipt.combat!!.attacked && it.receipt.combat.damageToDefender > 0 })
        check(result[0].receipt.combat!!.damageToAttacker == 0 && result[1].receipt.combat!!.damageToAttacker == 0)
        check(result[2].receipt.combat!!.damageToAttacker > 0)
        check(api.turn(token).map.units.single { it.name == "Warrior" }.combat!!.promotions!!.experience > 9)
        return result
    }

    private fun restore(runtime: HeadlessRuntime, api: CombatApi, root: Path) {
        val saved = expected(root)
        check(facts(runtime) == saved.units) { "Restart changed saved health, experience, promotions or orders" }
        check(allReports(runtime) == saved.reports) { "Restart lost or duplicated battle reports" }
        check(api.status().handoff == saved.handoff)
        api.acquire()
        var token = checkNotNull(api.activate().playerToken)
        replay(runtime, api, token, saved.commands.filter { it.civilization == "Rome" })
        var snapshot = api.readAlerts(token)
        val reports = snapshot.combatReports
        check(reports.isNotEmpty() && reports.map { it.id }.distinct().size == reports.size)
        val local = PlayerHost(runtime).bindPlayer("Rome").turn()
        check(runCatching { (local.combatReports as MutableList<*>).clear() }.exceptionOrNull() is UnsupportedOperationException)
        check(runCatching { (local.combatReports.first().locations as MutableList<*>).clear() }.exceptionOrNull() is UnsupportedOperationException)
        val warrior = snapshot.map.units.single { it.name == "Warrior" }
        val promotion = warrior.combat!!.promotions!!.choices.first { it.available && !it.name.contains("heal", ignoreCase = true) }
        val promoted = api.record("Rome", token, command(snapshot, TurnCommandKind.PROMOTE_UNIT, warrior.handle, promotion.name))
        snapshot = api.turn(token)
        check(snapshot.map.units.single { it.handle == warrior.handle }.combat!!.promotions!!.choices.any { it.name == promotion.name && it.acquired })
        check(snapshot.combatReports == reports) { "A promotion duplicated incoming battle reports" }
        val hold = snapshot.map.units.single { it.handle == warrior.handle }.combat!!.orders.first { it.available && it.name.endsWith("UNTIL_HEALED") }
        api.execute(token, command(snapshot, TurnCommandKind.SET_UNIT_ORDER, warrior.handle, hold.name))
        api.finishTurn(token)
        token = checkNotNull(api.activate().playerToken)
        replay(runtime, api, token, saved.commands.filter { it.civilization == "Greece" })
        check(api.turn(token).map.units.single { it.name == "Archer" }.health > saved.greekArcherHealth) { "Persistent healing did not restore health across scheduled turns" }

        // Peace becomes legal through normal turns, preserving the same battle and treaty systems.
        var rounds = 0
        while (true) {
            snapshot = api.readAlerts(token)
            if (api.status().activeCivilization == "Rome" && snapshot.diplomacy.knownCivilizations.single().negotiatePeace.available) break
            api.finishTurn(token)
            token = checkNotNull(api.activate().playerToken)
            check(++rounds < 30) { "War cooldown did not expire" }
        }
        val options = snapshot.diplomacy.knownCivilizations.single().peaceOptions!!
        fun treaty(terms: List<PeaceTerm>) = terms.single { it.type == "Treaty" && it.name == "Peace Treaty" }.let {
            PeaceTermSelection(it.type, checkNotNull(it.name), it.amount, it.duration)
        }
        api.execute(
            token,
            command(snapshot, TurnCommandKind.PROPOSE_PEACE, "Greece").copy(
                peaceTerms = PeaceTerms(listOf(treaty(options.give)), listOf(treaty(options.receive))),
            ),
        )
        api.finishTurn(token)
        token = checkNotNull(api.activate().playerToken)
        snapshot = api.readAlerts(token)
        val offer = snapshot.diplomacy.incomingOffers.single()
        check(offer.accept.available && offer.give.isNotEmpty() && offer.receive.isNotEmpty())
        api.execute(token, command(snapshot, TurnCommandKind.ACCEPT_PEACE, offer.handle))
        api.finishTurn(token)
        token = checkNotNull(api.activate().playerToken)
        snapshot = api.turn(token)
        check(snapshot.diplomacy.knownCivilizations.single().let { !it.atWar && it.peaceTreatyTurns > 0 })
        check(snapshot.map.units.all { it.combat!!.attacks.isEmpty() })
        check(runtime.game.getCivilization("Rome").getDiplomacyManager(runtime.game.getCivilization("Greece"))!!.turnsToPeaceTreaty() > 0)
        check(allReports(runtime).mapValues { it.value.map(CombatReport::id) } == saved.reports.mapValues { it.value.map(CombatReport::id) })
        save(root, CombatRecovery(saved.commands + promoted, facts(runtime), allReports(runtime), api.status().handoff))
    }

    private fun verify(runtime: HeadlessRuntime, api: CombatApi, root: Path) {
        val saved = expected(root)
        check(facts(runtime) == saved.units && allReports(runtime) == saved.reports)
        api.acquire()
        val token = checkNotNull(api.activate().playerToken)
        replay(runtime, api, token, saved.commands.filter { it.civilization == "Rome" })
        val before = api.turn(token)
        check(before.combatReports.isEmpty()) { "Delivered reports appeared again after restart" }
        check(before.map.units.single { it.name == "Warrior" }.combat!!.promotions!!.earnedPromotions == 1)
        api.finishTurn(token)
        check(api.status().handoff == saved.handoff + 1)
    }

    private fun conquer(runtime: HeadlessRuntime, api: CombatApi, root: Path) {
        api.acquire()
        val activation = api.activate()
        val token = checkNotNull(activation.playerToken)
        var snapshot = api.readAlerts(token)
        val requests = mutableListOf<CombatCommand>()
        val first = checkNotNull(snapshot.map.units.singleOrNull { it.position == TilePosition(-1, 0) }) { "Missing first attacker: ${snapshot.map.units}" }
        val firstAttack = checkNotNull(first.combat!!.attacks.firstOrNull { it.destination == TilePosition(0, 0) }) { "No attack on noncapital: ${first.combat}" }
        val other = snapshot.map.units.single { it.position == TilePosition(3, 0) }
        val otherAttack = other.combat!!.attacks.first { it.destination == TilePosition(4, 0) }
        val otherMove = other.moves.first()
        requests += api.record("Rome", token, attack(snapshot, first.handle, firstAttack))
        check(requests.last().receipt.combat!!.defenderCaptured)
        snapshot = api.turn(token)
        val capture = snapshot.alerts.single { it.capture != null }
        check(capture.capture!!.title.isNotBlank() && capture.capture.options.any { it.paragraphs.isNotEmpty() })
        check(!snapshot.canEndTurn && api.status().phase == "ACTIVE")
        val pendingGame = json().toJson(runtime.game)
        api.reject(token, attack(snapshot, other.handle, otherAttack))
        api.reject(token, command(snapshot, TurnCommandKind.MOVE_UNIT, other.handle).copy(destination = otherMove.destination))
        api.reject(token, attack(snapshot, first.handle, firstAttack))
        check(json().toJson(runtime.game) == pendingGame) { "A pending capture allowed another combat or movement mutation" }
        api.reject(token, command(snapshot, TurnCommandKind.ACKNOWLEDGE_ALERT, capture.handle))
        api.reject(token, command(snapshot, TurnCommandKind.RESOLVE_CAPTURED_CITY, capture.handle, "Invalid"))
        val puppet = capture.capture.options.single { it.name == "Puppet" }
        check(puppet.available && puppet.paragraphs.isNotEmpty())
        requests += api.record("Rome", token, command(snapshot, TurnCommandKind.RESOLVE_CAPTURED_CITY, capture.handle, puppet.name))
        check(runtime.game.getCivilization("Rome").cities.single { it.location == HexCoord(0, 0) }.isPuppet)

        snapshot = api.turn(token)
        val second = checkNotNull(snapshot.map.units.singleOrNull { it.position == TilePosition(3, 0) }) { "Missing second attacker: ${snapshot.map.units}" }
        val secondAttack = checkNotNull(second.combat!!.attacks.firstOrNull { it.destination == TilePosition(4, 0) }) { "No attack on capital: ${second.combat}" }
        requests += api.record("Rome", token, attack(snapshot, second.handle, secondAttack))
        snapshot = api.turn(token)
        val capital = snapshot.alerts.single { it.capture != null }
        check(api.status().phase == "ACTIVE") { "Scheduler stopped before the winning capture decision" }
        check(capital.capture!!.options.single { it.name == "Raze" }.let { !it.available && it.unavailableReasons.isNotEmpty() })
        api.reject(token, command(snapshot, TurnCommandKind.RESOLVE_CAPTURED_CITY, capital.handle, "Raze"))
        requests += api.record("Rome", token, command(snapshot, TurnCommandKind.RESOLVE_CAPTURED_CITY, capital.handle, "Annex"))
        check(runtime.game.getCivilization("Greece").isDefeated())
        check(runtime.game.getCivilization("Rome").cities.size == 3)
        val results = api.operator(OperatorRequest("results")).results
        verifyResults(api, results)
        verifyTerminalRollback(runtime, api, root, checkNotNull(activation.activationId))
        save(root, CombatRecovery(requests, facts(runtime), allReports(runtime), api.status().handoff, results = results, activationId = activation.activationId))
    }

    private fun captureLastSettler(runtime: HeadlessRuntime, api: CombatApi, root: Path, move: Boolean) {
        api.acquire()
        val activation = api.activate()
        val token = checkNotNull(activation.playerToken)
        val before = api.readAlerts(token)
        check(runtime.game.victoryData == null && before.result == null)
        val greece = runtime.game.getCivilization("Greece")
        check(greece.cities.isEmpty() && greece.units.getCivUnits().single().name == "Settler")
        val warrior = before.map.units.single { it.name == "Warrior" }
        val destination = TilePosition(1, 0)
        val request = if (move) {
            check(warrior.moves.any { it.destination == destination })
            command(before, TurnCommandKind.MOVE_UNIT, warrior.handle).copy(destination = destination)
        } else {
            attack(before, warrior.handle, warrior.combat!!.attacks.first { it.destination == destination })
        }
        val completed = api.record("Rome", token, request)
        if (!move) check(completed.receipt.combat!!.let { it.attacked && it.defenderCaptured && !it.defenderDestroyed })
        check(greece.isDefeated())
        check(runtime.game.getCivilization("Rome").units.getCivUnits().any { it.name == "Worker" })
        check(runtime.game.victoryData != null) { "A final civilian capture did not record the victory" }
        check(api.turn(token).alerts.none { it.capture != null })
        val results = api.operator(OperatorRequest("results")).results
        verifyResults(api, results)
        verifyTerminalRollback(runtime, api, root, checkNotNull(activation.activationId))
        save(
            root,
            CombatRecovery(
                listOf(completed),
                facts(runtime),
                allReports(runtime),
                api.status().handoff,
                results = results,
                activationId = activation.activationId,
            ),
        )
        // Model an ordinary desktop autosave between the decisive move and the screen's
        // victory check, retaining the capture itself and its independently emitted alerts.
        val beforeVictoryCheck = runtime.game.clone().apply {
            victoryData = null
            civilizations.forEach { player -> player.popupAlerts.removeAll { it.type == AlertType.GameHasBeenWon } }
        }
        Files.writeString(root.resolve("unrecorded-victory.json"), json().toJson(beforeVictoryCheck))
    }

    private fun verifyUnrecordedFinal(runtime: HeadlessRuntime, api: CombatApi, root: Path) {
        val saved = expected(root)
        check(runtime.game.victoryData?.winningCiv == "Rome") { "Loading a won save did not establish its victory" }
        check(facts(runtime) == saved.units) { "Recording the loaded victory changed units" }
        val originalSave = Files.readString(root.resolve("unrecorded-victory.json"))
        val before = json().toJson(runtime.game)
        repeat(2) {
            val results = api.operator(OperatorRequest("results")).results
            verifyResults(api, results)
            check(results.mapValues { it.value.result } == saved.results.mapValues { it.value.result })
            val token = api.playerToken("Rome")
            val turn = api.turn(token)
            check(turn.phase == "FINISHED")
            api.reject(token, command(turn, TurnCommandKind.END_TURN))
            val warrior = turn.map.units.single { it.name == "Warrior" }
            api.reject(token, command(turn, TurnCommandKind.SET_UNIT_ORDER, warrior.handle, "SKIP"))
        }
        check(runtime.game.getCivilization("Greece").popupAlerts.count { it.type == AlertType.GameHasBeenWon } == 1)
        check(json().toJson(runtime.game) == before) { "Reading or rejecting commands changed the loaded game" }
        check(Files.readString(root.resolve("unrecorded-victory.json")) == originalSave) { "Loading rewrote the original save" }
    }

    private fun verifyTerminalRollback(runtime: HeadlessRuntime, api: CombatApi, root: Path, activationId: String) {
        val saved = PlayerTurnJson.json.decodeFromString<GameCheckpoint>(Files.readString(root.resolve("checkpoint.json")))
        check(saved.turnStart == null) { "Winning without END_TURN retained the activation's rollback point" }
        api.acquire()
        val status = api.status()
        val game = json().toJson(runtime.game)
        val reports = allReports(runtime)
        api.rejectOperator(
            OperatorRequest(
                "rollback",
                api.lease,
                status.epoch,
                status.serverInstance,
                status.handoff,
                status.activeCivilization,
                activationId,
            ),
        )
        check(api.status().phase == "FINISHED" && api.status().handoff == status.handoff)
        check(json().toJson(runtime.game) == game && allReports(runtime) == reports) { "Terminal rollback erased the committed victory" }
    }

    private fun verifyFinal(runtime: HeadlessRuntime, api: CombatApi, root: Path) {
        val saved = expected(root)
        check(facts(runtime) == saved.units && allReports(runtime) == saved.reports)
        val results = api.operator(OperatorRequest("results")).results
        check(results == saved.results) { "Terminal state or combat reports changed after restart" }
        verifyResults(api, results)
        val status = api.status()
        check(status.scheduled && status.phase == "FINISHED")
        val before = json().toJson(runtime.game)
        val checkpoint = Files.readString(root.resolve("checkpoint.json"))
        for (entry in saved.commands) {
            val owner = api.playerToken(entry.civilization)
            val opponent = api.playerToken(if (entry.civilization == "Rome") "Greece" else "Rome")
            check(api.execute(owner, entry.request) == entry.receipt) { "Own completed command did not replay over HTTPS after restart" }
            api.reject(opponent, entry.request)
            api.reject(owner, entry.request.copy(requestId = UUID.randomUUID().toString()))
            api.reject(owner, entry.request.copy(revision = UUID.randomUUID().toString()))
            api.reject(owner, entry.request.copy(epoch = UUID.randomUUID().toString()))
        }
        check(json().toJson(runtime.game) == before)
        check(api.status().handoff == status.handoff)
        check(Files.readString(root.resolve("checkpoint.json")) == checkpoint) { "Terminal retries or fenced commands changed the checkpoint" }
        verifyTerminalRollback(runtime, api, root, checkNotNull(saved.activationId))
    }

    private fun verifyResults(api: CombatApi, results: Map<String, PlayerFinalState>) {
        check(api.status().phase == "FINISHED")
        check(results.keys == setOf("Rome", "Greece"))
        check(results.getValue("Rome").result!!.outcome == "Victory")
        check(results.getValue("Greece").result!!.outcome == "Defeat")
        check(results.values.all { it.result!!.winningCivilizationName == "Rome" && it.result.paragraphs.isNotEmpty() })
        check(results.getValue("Greece").combatReports.isNotEmpty()) { "Defeated player lost the final battle report" }
        check(results.getValue("Rome").messages.any { it.acknowledgement == "Farewell." && it.paragraphs.isNotEmpty() }) {
            "The terminal delivery omitted the defeated leader's final message"
        }
        for (civilization in results.keys) {
            val turn = api.turn(api.playerToken(civilization))
            check(turn.result == results.getValue(civilization).result && !turn.canEndTurn)
            check(turn.map.units.all { it.combat!!.attacks.isEmpty() })
            check(api.result(api.playerToken(civilization)) == results.getValue(civilization))
            for (message in turn.alerts.filter { it.kind == "Defeated" }.mapNotNull { it.content }) {
                check(message in results.getValue(civilization).messages)
            }
        }
    }

    private fun replay(runtime: HeadlessRuntime, api: CombatApi, token: String, commands: List<CombatCommand>) {
        val before = json().toJson(runtime.game)
        val reports = allReports(runtime)
        for (entry in commands) check(api.execute(token, entry.request) == entry.receipt)
        check(json().toJson(runtime.game) == before) { "Retry repeated combat, movement or promotion effects" }
        check(allReports(runtime) == reports) { "Retry duplicated a battle report" }
    }

    private fun facts(runtime: HeadlessRuntime) = runtime.game.civilizations.filter { it.isMajorCiv() }.flatMap { civilization ->
        civilization.units.getCivUnits().map { unit ->
            CombatUnitFacts(
                civilization.civName,
                unit.id,
                unit.name,
                unit.health,
                unit.promotions.XP,
                unit.promotions.promotions.sorted(),
                TilePosition(unit.currentTile.position.x, unit.currentTile.position.y),
                unit.action,
            )
        }.toList()
    }.sortedWith(compareBy({ it.civilization }, { it.id }))

    private fun allReports(runtime: HeadlessRuntime): Map<String, List<CombatReport>> = listOf("Rome", "Greece").associateWith {
        runtime.executor.turnSessions[it]?.combatReports?.values?.toList() ?: emptyList()
    }

    private fun attack(turn: PlayerTurnSnapshot, unit: String, offer: AttackOption) = command(turn, TurnCommandKind.ATTACK, unit)
        .copy(destination = offer.destination, attackFrom = offer.attackFrom)
    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection).reviewedFor(turn)
    private fun save(root: Path, value: CombatRecovery) = Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(value))
    private fun expected(root: Path) = PlayerTurnJson.json.decodeFromString<CombatRecovery>(Files.readString(root.resolve("expected.json")))

    private class CombatApi(runtime: HeadlessRuntime, tlsDirectory: Path) : AutoCloseable {
        private val tls = TlsTestFixture(Files.createDirectories(tlsDirectory))
        private val client = HttpClient.newBuilder().sslContext(tls.context).connectTimeout(Duration.ofSeconds(5)).build()
        private val operatorToken = "O".repeat(43)
        private val tokens = mapOf("Rome" to "R".repeat(43), "Greece" to "G".repeat(43))
        private val server = PlayerMcpServer(
            tokens.map { (civilization, token) -> PlayerMcpBinding(token, PlayerHost(runtime).bindPlayer(civilization)) },
            0,
            OperatorMcpBinding(operatorToken, OperatorControl(runtime.executor)),
            tls.tls,
        )
        private val base = "https://localhost:${server.start()}"
        val lease = UUID.randomUUID().toString()
        fun playerToken(civilization: String) = tokens.getValue(civilization)
        fun acquire() = operator(OperatorRequest("acquire", lease))
        fun status() = operator(OperatorRequest("status")).status
        fun activate(): OperatorResponse {
            val status = status()
            return operator(
                OperatorRequest(
                    "activate",
                    lease,
                    status.epoch,
                    status.serverInstance,
                    status.handoff,
                    status.activeCivilization,
                    UUID.randomUUID().toString(),
                ),
            )
        }
        fun turn(token: String): PlayerTurnSnapshot = PlayerTurnJson.json.decodeFromString(tool(token, "player_turn").toString())
        fun result(token: String): PlayerFinalState = PlayerTurnJson.json.decodeFromString(tool(token, "player_result").toString())
        fun execute(token: String, request: TurnCommandRequest): TurnCommandReceipt = PlayerTurnJson.json.decodeFromString(
            tool(token, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject).toString(),
        )
        fun record(civilization: String, token: String, request: TurnCommandRequest): CombatCommand {
            val receipt = execute(token, request)
            check(execute(token, request) == receipt)
            return CombatCommand(civilization, request, receipt)
        }
        fun reject(token: String, request: TurnCommandRequest) {
            tool(token, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject, true)
        }
        fun operator(request: OperatorRequest): OperatorResponse = OperatorControl.json.decodeFromString(raw("/operator", OperatorControl.json.encodeToString(request), operatorToken))
        fun rejectOperator(request: OperatorRequest) {
            val response = raw("/operator", OperatorControl.json.encodeToString(request), operatorToken, expectedStatus = 409)
            check(Json.parseToJsonElement(response).jsonObject.getValue("error").jsonPrimitive.content == "GAME_FINISHED") { response }
        }
        fun readAlerts(token: String): PlayerTurnSnapshot {
            repeat(20) {
                val snapshot = turn(token)
                val alert = snapshot.alerts.firstOrNull { it.canAcknowledge } ?: return snapshot
                check(alert.content!!.paragraphs.isNotEmpty())
                execute(token, command(snapshot, TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle))
            }
            error("Combat fixture alert budget exhausted")
        }
        fun finishTurn(token: String) {
            operator(OperatorRequest("heartbeat", lease))
            repeat(40) {
                val turn = readAlerts(token)
                val city = turn.cities.firstOrNull { !it.puppet && it.constructionQueue.isEmpty() }
                val request = when {
                    turn.freeTechnologies > 0 -> command(turn, TurnCommandKind.CHOOSE_FREE_TECHNOLOGY, selection = turn.researchOptions.first())
                    turn.freeGreatPeople > 0 -> command(turn, TurnCommandKind.CHOOSE_FREE_GREAT_PERSON, selection = turn.greatPersonOptions.first())
                    turn.researchQueue.isEmpty() -> command(turn, TurnCommandKind.SET_RESEARCH_QUEUE).copy(queue = listOf(turn.researchOptions.first()))
                    city != null -> command(turn, TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, city.constructionOptions.first())
                    turn.pending.any { it.kind == "Policy" } && turn.policyOptions.isNotEmpty() -> command(turn, TurnCommandKind.ADOPT_POLICY, selection = turn.policyOptions.first())
                    turn.canEndTurn -> command(turn, TurnCommandKind.END_TURN)
                    else -> error("Combat fixture blocked by ${turn.pending}")
                }
                execute(token, request)
                if (request.kind == TurnCommandKind.END_TURN) return
            }
            error("Combat fixture turn budget exhausted")
        }
        private fun raw(path: String, body: String, token: String, expectedStatus: Int = 200): String {
            val response = client.send(
                HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofSeconds(20))
                    .header("Authorization", "Bearer $token").header("Content-Type", "application/json")
                    .header("Accept", "application/json, text/event-stream")
                    .POST(HttpRequest.BodyPublishers.ofString(body)).build(),
                HttpResponse.BodyHandlers.ofString(),
            )
            check(response.statusCode() == expectedStatus) { "HTTPS ${response.statusCode()}: ${response.body()}" }
            return response.body()
        }
        private fun tool(token: String, name: String, arguments: JsonObject = JsonObject(emptyMap()), error: Boolean = false): JsonObject {
            val request = """{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"$name","arguments":$arguments}}"""
            val result = Json.parseToJsonElement(raw("/mcp", request, token)).jsonObject.getValue("result").jsonObject
            check(result.getValue("isError").jsonPrimitive.boolean == error) { result.toString() }
            return if (error) result else result.getValue("structuredContent").jsonObject
        }
        override fun close() {
            server.close()
            client.close()
        }
    }
}
