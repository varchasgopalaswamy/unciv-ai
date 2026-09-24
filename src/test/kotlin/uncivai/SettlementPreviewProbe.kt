package uncivai

import com.unciv.json.json
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.UnitSettlement
import uncivai.player.MovementPreview
import uncivai.player.MovementPreviewRequest
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path
import java.util.UUID

object SettlementPreviewProbe {
    @JvmStatic fun main(args: Array<String>) {
        try {
            val root = Path.of(args[0])
            when (args[1]) {
                "rules" -> rules(root)
                "privacy" -> privacy(root)
                "orders" -> orders(root)
                else -> error("Unknown scenario")
            }
            println("SETTLEMENT_PREVIEW_${args[1]}_PASS")
        } catch (failure: Throwable) {
            failure.printStackTrace()
            kotlin.system.exitProcess(1)
        }
    }

    internal fun fixture(root: Path) = HeadlessRuntime.createVanillaGame(root, 412L).also { runtime ->
        val game = runtime.game
        game.gameParameters.noBarbarians = true
        for (civ in game.civilizations) civ.units.getCivUnits().toList().forEach { it.destroy() }
        for (tile in game.tileMap.values) {
            tile.naturalWonder = null
            tile.setBaseTerrain(game.ruleset.terrains.getValue("Grassland"))
            tile.setTerrainFeatures(arrayListOf())
            tile.setTileResource(null)
            tile.setImprovement(null)
            tile.setTerrainTransients()
            tile.clearContinent()
            tile.setContinent(0)
            tile.hasBottomRiver = false
            tile.hasBottomLeftRiver = false
            tile.hasBottomRightRiver = false
        }
        for ((name, position) in listOf("Rome" to HexCoord(0, 0), "Greece" to HexCoord(-7, 0), "France" to HexCoord(7, 5))) {
            val civ = game.getCivilization(name)
            civ.addCity(position).cityConstructions.constructionQueue = arrayListOf("Monument")
            civ.tech.techsToResearch = arrayListOf("Pottery")
            for (tile in game.tileMap.values) tile.setExplored(civ, true)
        }
        val rome = game.getCivilization("Rome")
        rome.units.placeUnitNearTile(HexCoord(2, 0), "Settler")!!.also { check(it.currentTile.position == HexCoord(2, 0)) }
        for (civ in game.civilizations) {
            civ.cache.updateViewableTiles()
            civ.popupAlerts.clear()
            civ.notifications.clear()
        }
        rome.viewableTiles = game.tileMap.values.toSet()
    }

    private fun preview(api: GameplayTestApi, token: String, target: String, destination: TilePosition): MovementPreview = PlayerTurnJson.json.decodeFromString(api.tool(token, "movement_preview", obj("target" to target, "destination" to obj("x" to destination.x, "y" to destination.y))).toString())

    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target).reviewedFor(turn)

    private fun rules(root: Path) = fixture(root.resolve("initial")).use { runtime ->
        val game = runtime.game
        val rome = game.getCivilization("Rome")
        val settler = rome.units.getCivUnits().single()
        GameplayTestApi(runtime, root.resolve("tls")).use { api ->
            val token = api.playerToken("Rome")
            var turn = api.turn(token)
            val unit = turn.map.units.single()
            val current = checkNotNull(unit.settlement)
            check(current.siteStatus == "BLOCKED" && current.canFoundNow == false && current.atDestination)
            check(current.siteBlockers.single().city == "Rome")
            check(current.siteBlockers.single().distance == 2 && current.siteBlockers.single().minimumDistance == 4)

            val nativeBefore = json().toJson(game)
            val blocked = preview(api, token, unit.handle, TilePosition(3, 0))
            val blockedSite = checkNotNull(blocked.settlement)
            check(json().toJson(game) == nativeBefore) { "Preview changed the game" }
            check(blocked.available && blockedSite.siteStatus == "BLOCKED")
            check(blockedSite.canFoundNow == null && !blockedSite.atDestination)
            // An unsuitable city site remains a legal movement destination.
            api.execute(token, command(turn, TurnCommandKind.SET_UNIT_DESTINATION, unit.handle).copy(destination = blocked.destination))
            turn = api.turn(token)
            check(turn.map.units.single().position == blocked.destination)
            check(!turn.map.units.single().canFoundCity)

            settler.currentMovement = 0f
            rome.viewableTiles = game.tileMap.values.toSet()
            turn = api.turn(token)
            val legalSite = TilePosition(0, 4)
            val exhausted = preview(api, token, unit.handle, legalSite)
            val exhaustedSite = checkNotNull(exhausted.settlement)
            check(!exhausted.available && exhaustedSite.siteStatus == "ELIGIBLE") { "Unexpected exhausted preview: $exhausted" }
            check(exhaustedSite.unitBlockers.single().code == "NO_MOVEMENT")
            check(turn.map.units.single().settlement!!.siteStatus == "BLOCKED")
            settler.currentMovement = 2f
            turn = api.turn(token)
            check(turn.map.units.single().settlement!!.unitBlockers.isEmpty())
            check(turn.map.units.single().settlement!!.siteStatus == "BLOCKED")

            // A known foreign city's spacing and tile ownership are explained separately.
            val france = game.getCivilization("France")
            val foreignCity = france.addCity(HexCoord(6, 0))
            rome.viewableTiles = game.tileMap.values.toSet()
            api.turn(token)
            val nearForeignCity = preview(api, token, unit.handle, TilePosition(4, 0)).settlement!!
            check(nearForeignCity.siteBlockers.single().city == foreignCity.name)
            check(nearForeignCity.siteBlockers.single().distance == 2)
            foreignCity.expansion.takeOwnership(game.tileMap[0, 4])
            api.turn(token)
            check(preview(api, token, unit.handle, legalSite).settlement!!.siteBlockers.single().code == "FOREIGN_TERRITORY")
            rome.cities.single().expansion.takeOwnership(game.tileMap[0, 4])
            rome.viewableTiles = game.tileMap.values.toSet()
            api.turn(token)
            check(preview(api, token, unit.handle, legalSite).settlement!!.siteStatus == "ELIGIBLE")

            // Restore a valid current site, then use the ordinary founding command and retry receipt.
            settler.removeFromTile()
            settler.putInTile(game.tileMap[0, 4])
            settler.currentMovement = 2f
            rome.viewableTiles = game.tileMap.values.toSet()
            turn = api.turn(token)
            check(turn.map.units.single().canFoundCity)
            val ready = preview(api, token, unit.handle, legalSite).settlement!!
            check(ready.canFoundNow == true && ready.siteStatus == "ELIGIBLE")
            val found = command(turn, TurnCommandKind.FOUND_CITY, unit.handle)
            val receipt = api.execute(token, found)
            check(api.execute(token, found) == receipt)
            check(rome.cities.size == 2 && settler.isDestroyed)
        }
    }

    private fun privacy(root: Path) = fixture(root.resolve("initial")).use { runtime ->
        val game = runtime.game
        val rome = game.getCivilization("Rome")
        val settler = rome.units.getCivUnits().single()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val handle = session.turn().map.units.single().handle
        val hidden = game.tileMap[6, 0]
        val visible = game.tileMap.values.filter { it !== hidden }.toSet()
        hidden.setExplored(rome, false)
        rome.viewableTiles = visible
        val request = MovementPreviewRequest(handle, TilePosition(4, 0))
        settler.action = "moveTo 4,0"
        val beforeOrder = checkNotNull(session.turn().map.units.single().movementOrder)
        check(beforeOrder.destination == request.destination)
        check(beforeOrder.settlement!!.siteStatus == "UNKNOWN" && beforeOrder.settlement.siteBlockers.isEmpty())
        val before = session.movementPreview(request).settlement!!
        check(before.siteStatus == "UNKNOWN" && before.canFoundNow == null && before.siteBlockers.isEmpty())
        check(UnitSettlement.canFoundCity(settler, game.tileMap[4, 0]))
        val secretCity = game.getCivilization("France").addCity(hidden.position)
        rome.viewableTiles = visible
        hidden.setExplored(rome, false)
        check(!UnitSettlement.canFoundCity(settler, game.tileMap[4, 0]))
        check(before == session.movementPreview(request).settlement) { "Unseen city changed prospective settlement response" }
        check(beforeOrder == session.turn().map.units.single().movementOrder) { "Unseen city changed standing-order advice" }

        // Exploring a center does not expose its current existence under fog.
        hidden.setExplored(rome, true)
        check(before == session.movementPreview(request).settlement)
        check(beforeOrder == session.turn().map.units.single().movementOrder)
        rome.viewableTiles = game.tileMap.values.toSet()
        val nativeBefore = json().toJson(game)
        val revealedOrder = session.turn().map.units.single().movementOrder!!
        check(json().toJson(game) == nativeBefore) { "Observing a blocked destination changed the game" }
        check(revealedOrder.destination == beforeOrder.destination && settler.isMoving())
        check(revealedOrder.settlement!!.siteStatus == "BLOCKED")
        val revealed = session.movementPreview(request).settlement!!
        check(revealed.siteStatus == "BLOCKED" && revealed.uncertainty == null)
        check(revealed.siteBlockers.single().city == secretCity.name)
        check(revealed.siteBlockers.single().position == TilePosition(6, 0))
        check(revealedOrder.settlement.siteBlockers == revealed.siteBlockers)
        check(revealedOrder.settlement.uncertainty == revealed.uncertainty)
        settler.currentMovement = 0f
        check(session.turn().map.units.single().movementOrder!!.settlement == revealedOrder.settlement)
        settler.currentMovement = 2f
        rome.viewableTiles = visible
        val remembered = session.turn().map.units.single().movementOrder!!.settlement!!
        check(remembered.siteStatus == "BLOCKED" && remembered.uncertainty != null)
        check(remembered.siteBlockers.single().city == secretCity.name && remembered.siteBlockers.single().remembered)
        check(remembered.siteBlockers.single().observedTurn == game.turns)

        // Unexplored terrain and out-of-map coordinates share an unknown response.
        hidden.setExplored(rome, false)
        rome.viewableTiles = setOf(settler.currentTile)
        val unknown = session.movementPreview(MovementPreviewRequest(handle, TilePosition(6, 0))).settlement!!
        val outside = session.movementPreview(MovementPreviewRequest(handle, TilePosition(Int.MAX_VALUE, Int.MIN_VALUE))).settlement!!
        check(unknown.copy(destination = outside.destination) == outside)
        hidden.setBaseTerrain(game.ruleset.terrains.getValue("Ocean"))
        check(unknown == session.movementPreview(MovementPreviewRequest(handle, TilePosition(6, 0))).settlement)

        try {
            PlayerHost(runtime).bindPlayer("Greece").movementPreview(MovementPreviewRequest(handle, TilePosition(4, 0)))
            error("Another player's unit was accepted")
        } catch (failure: PlayerSessionException) {
            check(failure.reason == PlayerSessionFailure.UNIT_UNAVAILABLE)
        }
    }

    private fun orders(root: Path) = fixture(root.resolve("initial")).use { runtime ->
        val game = runtime.game
        val rome = game.getCivilization("Rome")
        val settler = rome.units.getCivUnits().single()
        GameplayTestApi(runtime, root.resolve("tls")).use { api ->
            val token = api.playerToken("Rome")
            var turn = api.turn(token)
            val handle = turn.map.units.single().handle
            val destination = TilePosition(0, 5)
            check(preview(api, token, handle, destination).available)
            val receipt = api.execute(token, command(turn, TurnCommandKind.SET_UNIT_DESTINATION, handle).copy(destination = destination))
            check(receipt.movement!!.orderAfter!!.destination == destination)
            check(receipt.movement.orderAfter.settlement != null)
            rome.viewableTiles = game.tileMap.values.toSet()
            turn = api.turn(token)
            val observed = turn.map.units.single()
            check(observed.position != destination && observed.settlement!!.siteStatus == "BLOCKED")
            check(observed.movementOrder!!.settlement!!.siteStatus == "ELIGIBLE")
            check(observed.movementOrder.settlement.siteBlockers.isEmpty())
            check(observed.movementOrder.settlement.uncertainty == null)
            api.execute(token, command(turn, TurnCommandKind.CANCEL_UNIT_MOVEMENT, handle))
            check(api.turn(token).map.units.single().movementOrder == null)

            // Arrival clears the native order and leaves the ordinary current-tile diagnostic.
            settler.currentMovement = 10f
            turn = api.turn(token)
            check(preview(api, token, handle, destination).available)
            api.execute(token, command(turn, TurnCommandKind.SET_UNIT_DESTINATION, handle).copy(destination = destination))
            turn = api.turn(token)
            check(turn.map.units.single().position == destination)
            check(turn.map.units.single().movementOrder == null)
            check(turn.map.units.single().canFoundCity)

            val warrior = rome.units.placeUnitNearTile(HexCoord(2, 0), "Warrior")!!
            warrior.action = "moveTo 4,0"
            check(api.turn(token).map.units.single { it.name == "Warrior" }.movementOrder!!.settlement == null)
        }
    }
}
