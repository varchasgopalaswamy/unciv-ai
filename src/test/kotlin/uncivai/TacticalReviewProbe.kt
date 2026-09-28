package uncivai

import com.unciv.json.json
import com.unciv.logic.GameStarter
import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.MapParameters
import com.unciv.logic.map.MapSize
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.models.metadata.BaseRuleset
import com.unciv.models.metadata.GameParameters
import com.unciv.models.metadata.GameSetupInfo
import com.unciv.models.metadata.Player
import com.unciv.models.ruleset.unique.UniqueType
import kotlinx.serialization.json.jsonObject
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import uncivai.player.VisibleUnit
import java.nio.file.Path
import java.util.UUID
import kotlin.math.abs

/** The France failure pattern: no army, a worker beside a Brute, and an unused city shot. */
object TacticalReviewProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        if (args[1] == "wrap") {
            wrapping(root)
            return
        }
        if (args[1] == "captives") {
            captives(root)
            return
        }
        val checkpoint = root.resolve("checkpoint.json")
        var workerId = -1
        lateinit var shotRequest: TurnCommandRequest
        lateinit var shotReceipt: TurnCommandReceipt
        HeadlessRuntime.createVanillaGame(root.resolve("initial"), 412L).use { runtime ->
            val game = runtime.game
            for (tile in game.tileMap.tileList) {
                tile.baseTerrain = "Grassland"
                tile.setTerrainFeatures(emptyList())
                tile.naturalWonder = null
                tile.tileResource = null
                tile.improvement = null
                tile.setTerrainTransients()
            }
            for ((name, position) in listOf("Rome" to HexCoord(4, 0), "Greece" to HexCoord(0, 5), "France" to HexCoord(-5, -1))) {
                val civ = game.getCivilization(name)
                val settler = civ.units.getCivUnits().single { it.name == "Settler" }
                place(settler, position)
                checkNotNull(UnitSettlement.tryFoundCity(settler))
                if (name == "France") civ.units.getCivUnits().toList().forEach { it.destroy() }
            }
            val france = game.getCivilization("France")
            val barbarians = game.getCivilization("Barbarians")
            val city = france.cities.single()
            city.name = "Orleans"
            val worker = checkNotNull(game.tileMap.placeUnitNearTile(HexCoord(-6, -2), "Worker", france))
            place(worker, HexCoord(-6, -2))
            workerId = worker.id
            val brute = checkNotNull(game.tileMap.placeUnitNearTile(HexCoord(-6, -1), "Brute", barbarians))
            place(brute, HexCoord(-6, -1))
            brute.health = 32
            // Enemy civilians do not threaten capture; an unseen military unit must not leak.
            val captive = checkNotNull(game.tileMap.placeUnitNearTile(HexCoord(-6, -1), "Worker", barbarians))
            place(captive, HexCoord(-6, -1))
            france.cache.updateViewableTiles()
            val remote = game.tileMap.tileList.first { !it.isVisible(france) && it.aerialDistanceTo(worker.currentTile) > 5 && it.isLand }
            val hidden = checkNotNull(game.tileMap.placeUnitNearTile(remote.position, "Brute", barbarians))
            remote.setExplored(france, true)
            game.turns = 60
            game.currentPlayer = "France"
            game.currentPlayerCiv = france
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls"), mapOf("France" to "F".repeat(43))).use { api ->
                api.acquire()
                val token = checkNotNull(api.activate().playerToken)
                val before = api.readAlerts(token)
                val ours = before.map.units.single()
                check(!ours.military && ours.nearbyHostiles.single().unit.name == "Brute")
                check(ours.nearbyHostiles.single().distance == 1)
                check(before.map.tiles.flatMap { it.units }.any { !it.military && it.atWar })
                check(before.map.tiles.none { tile -> tile.position == TilePosition(hidden.currentTile.position.x, hidden.currentTile.position.y) && tile.units.isNotEmpty() })
                val offered = before.cities.single().attacks.single { it.destination == TilePosition(-6, -1) }
                val nativeBefore = json().toJson(game)
                shotRequest = TurnCommandRequest(UUID.randomUUID().toString(), before.epoch, before.revision, TurnCommandKind.CITY_ATTACK, before.cities.single().handle, destination = offered.destination)
                api.reject(token, shotRequest.copy(requestId = UUID.randomUUID().toString(), attackFrom = offered.attackFrom), "INVALID_ARGUMENT")
                check(json().toJson(game) == nativeBefore)
                shotReceipt = api.execute(token, shotRequest)
                val combat = checkNotNull(shotReceipt.combat)
                check(combat.attacked && combat.damageToDefender > 0)
                check(brute.health < 32 || brute.isDestroyed)
                check(api.execute(token, shotRequest) == shotReceipt)
                val after = api.turn(token)
                check(after.cities.single().attacks.isEmpty())
                val move = after.map.units.single().moves.single { it.destination == TilePosition(-5, -1) }
                api.execute(token, TurnCommandRequest(UUID.randomUUID().toString(), after.epoch, after.revision, TurnCommandKind.MOVE_UNIT, ours.handle, destination = move.destination))
                check(worker.civ === france && worker.currentTile === city.getCenterTile())
                // End through the barbarian turn, then verify protection in the native state.
                api.finishTurn(token)
                check(game.turns == 61 && worker.civ === france && worker.currentTile === city.getCenterTile())
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val france = runtime.game.getCivilization("France")
            val worker = france.units.getCivUnits().single { it.id == workerId }
            check(worker.currentTile === france.cities.single().getCenterTile())
            check(PlayerHost(runtime).bindPlayer("France").completedReceipt(shotRequest) == shotReceipt)
        }
    }

    private fun captives(root: Path) = SettlementPreviewProbe.fixture(root.resolve("captives")).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val barbarians = game.getCivilization("Barbarians")
        val settler = player.units.getCivUnits().single()
        val brute = checkNotNull(barbarians.units.placeUnitNearTile(HexCoord(3, 0), "Brute"))
        // Native movement captures the Settler and leaves a visible military guard on it.
        place(brute, settler.currentTile.position)
        check(settler.civ === barbarians && settler.originalOwningCiv === player && settler.name == "Settler")
        brute.health = 1
        val warrior = checkNotNull(player.units.placeUnitNearTile(HexCoord(1, 0), "Warrior"))
        val worker = checkNotNull(player.units.placeUnitNearTile(HexCoord(1, 2), "Worker"))
        worker.capturedBy(barbarians)
        val other = checkNotNull(game.getCivilization("Greece").units.placeUnitNearTile(HexCoord(2, 2), "Worker"))
        other.capturedBy(barbarians)
        val uncapturable = checkNotNull(player.units.placeUnitNearTile(HexCoord(0, 2), "Great Scientist"))
        check(uncapturable.hasUnique(UniqueType.Uncapturable))
        // Transfer ownership directly to exercise the native destruction exception.
        uncapturable.capturedBy(barbarians)
        val hidden = checkNotNull(player.units.placeUnitNearTile(HexCoord(0, 8), "Settler"))
        hidden.capturedBy(barbarians)
        player.cache.updateViewableTiles()
        check(!hidden.isVisibleTo(player) && hidden.currentTile.isExplored(player))
        check(listOf(settler, worker, other, uncapturable).all { it.isVisibleTo(player) })
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val nativeBefore = json().toJson(game)
        val before = session.turn()
        check(json().toJson(game) == nativeBefore) { "Recapture observation changed native state" }
        val recaptures = before.map.tiles.flatMap { tile -> tile.units.mapNotNull { it.recapture } }
        check(recaptures.map { it.unitAfterCapture }.sorted() == listOf("Settler", "Worker"))
        check(recaptures.all { it.originalOwner == "Rome" })
        val captiveTile = before.map.tiles.single { it.position == TilePosition(2, 0) }
        check(captiveTile.units.single { it.military }.name == "Brute")
        check(captiveTile.units.single { !it.military }.recapture?.unitAfterCapture == "Settler")
        for (visible in captiveTile.units) {
            val encoded = PlayerTurnJson.json.encodeToJsonElement(VisibleUnit.serializer(), visible).jsonObject
            check(("recapture" in encoded) == !visible.military) { "Only relevant units should carry recapture data" }
            check(PlayerTurnJson.json.decodeFromJsonElement(VisibleUnit.serializer(), encoded) == visible)
        }
        check(before.map.tiles.single { it.position == TilePosition(0, 8) }.units.isEmpty())
        // Losing vision removes the captive entirely, even though it is still alive and explored.
        player.viewableTiles = player.viewableTiles - settler.currentTile
        check(session.turn().map.tiles.single { it.position == TilePosition(2, 0) }.units.isEmpty())
        player.cache.updateViewableTiles()
        val turn = session.turn()
        val unit = turn.map.units.single { it.name == "Warrior" }
        val attack = checkNotNull(unit.combat).attacks.single {
            it.destination == TilePosition(2, 0) && it.attackFrom == unit.position
        }
        session.execute(
            TurnCommandRequest(
                "recapture-settler",
                turn.epoch,
                turn.revision,
                TurnCommandKind.ATTACK,
                unit.handle,
                destination = attack.destination,
                attackFrom = attack.attackFrom,
            ),
        )
        check(brute.isDestroyed && warrior.currentTile === settler.currentTile)
        check(settler.civ === player && settler.name == "Settler") { "Recapture must restore a Settler, not a Worker" }
        val after = session.turn()
        check(after.map.units.any { it.name == "Settler" && it.position == TilePosition(2, 0) })
        check(after.map.tiles.single { it.position == TilePosition(2, 0) }.units.all { it.recapture == null })
        worker.destroy()
        check(session.turn().map.tiles.flatMap { it.units }.none { it.recapture != null })
    }

    private fun wrapping(root: Path) {
        HeadlessRuntime.create(root.resolve("wrap")) {
            val parameters = GameParameters().apply {
                baseRuleset = BaseRuleset.Civ_V_Vanilla.fullName
                numberOfCityStates = 0
                players = arrayListOf(Player("Rome", PlayerType.Human), Player("Greece", PlayerType.Human))
            }
            val map = MapParameters().apply {
                baseRuleset = parameters.baseRuleset
                mapSize = MapSize.Tiny
                worldWrap = true
                seed = 412L
            }
            GameStarter.startNewGame(GameSetupInfo(parameters, map))
        }.use { runtime ->
            val game = runtime.game
            val rome = game.getCivilization("Rome")
            val barbarians = game.getCivilization("Barbarians")
            val edge = game.tileMap.tileList.first { tile -> tile.neighbors.any { abs(it.position.x - tile.position.x) > 2 } }
            val across = edge.neighbors.first { abs(it.position.x - edge.position.x) > 2 }
            for (tile in listOf(edge, across)) {
                tile.baseTerrain = "Grassland"
                tile.setTerrainFeatures(emptyList())
                tile.setTerrainTransients()
            }
            val settler = rome.units.getCivUnits().single { it.name == "Settler" }
            place(settler, edge.position)
            val brute = checkNotNull(game.tileMap.placeUnitNearTile(across.position, "Brute", barbarians))
            place(brute, across.position)
            rome.cache.updateViewableTiles()
            val snapshot = PlayerHost(runtime).bindPlayer("Rome").turn()
            val wrap = checkNotNull(snapshot.map.wrapOffset)
            // The agent's region query can reproduce actual map distance from this public offset.
            for (tile in game.tileMap.tileList) {
                val distance = (-1..1).minOf { offset ->
                    val dx = tile.position.x - edge.position.x + offset * wrap.x
                    val dy = tile.position.y - edge.position.y + offset * wrap.y
                    maxOf(abs(dx), abs(dy), abs(dx - dy))
                }
                check(distance == tile.aerialDistanceTo(edge))
            }
            val seen = snapshot.map.units.single { it.name == "Settler" }.nearbyHostiles.single()
            check(seen.distance == 1 && seen.position == TilePosition(across.position.x, across.position.y))
        }
    }

    private fun place(unit: MapUnit, position: HexCoord) {
        unit.removeFromTile()
        unit.putInTile(unit.civ.gameInfo.tileMap[position])
        unit.currentMovement = unit.getMaxMovement().toFloat()
        unit.updateVisibleTiles()
    }
}
