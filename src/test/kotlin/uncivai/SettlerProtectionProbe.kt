package uncivai

import com.unciv.json.json
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.MapUnit
import uncivai.player.MovementPreviewRequest
import uncivai.player.PlayerHost
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path

internal object SettlerProtectionProbe {
    private fun place(unit: MapUnit, position: HexCoord) {
        unit.removeFromTile()
        unit.putInTile(unit.civ.gameInfo.tileMap[position])
        unit.currentMovement = unit.getMaxMovement().toFloat()
    }

    fun protection(root: Path) = SettlementPreviewProbe.fixture(root).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val settler = player.units.getCivUnits().single()
        val warrior = player.units.placeUnitNearTile(HexCoord(0, 0), "Warrior")!!
        player.cache.updateViewableTiles()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        var turn = session.turn()
        val handle = turn.map.units.single { it.name == "Settler" }.handle
        val soldier = turn.map.units.single { it.name == "Warrior" }.handle
        var protection = turn.map.units.single { it.handle == handle }.protection!!
        check(protection.current.protection == "UNESCORTED")
        check(protection.current.captureAssessment == "NO_VISIBLE_CAPTURE_THREAT")
        val candidate = protection.escortCandidates.single()
        check(candidate.unit == soldier && !candidate.atSettler && candidate.canReachThisTurn)
        check(candidate.estimatedTurns == PlayerUnitOperations(player).route(warrior, settler.currentTile)!!.turns.size)

        // The exhausted auto-explorer is still a candidate, with no invented arrival estimate.
        warrior.currentMovement = 0f
        warrior.action = "Explore"
        protection = session.turn().map.units.single { it.handle == handle }.protection!!
        check(!protection.escortCandidates.single().canReachThisTurn)
        check(protection.escortCandidates.single().routeStatus == "NO_MOVEMENT_REMAINING")
        check(protection.escortCandidates.single().estimatedTurns == null)

        // A reachable route with surrounding fog is never labelled safe.
        val preview = session.movementPreview(MovementPreviewRequest(handle, TilePosition(4, 0)))
        val stop = preview.turns.first().afterMove!!
        check(stop.civilianExposure!!.protection == "UNESCORTED")
        check(stop.civilianExposure.captureAssessment == "NO_VISIBLE_CAPTURE_THREAT")
        check(stop.civilianExposure.unseenTilesWithinTwoHexes > 0)
        check(stop.movementRemaining == 0f)

        // Standing together provides immediate cover, but an independent move leaves it behind.
        place(warrior, settler.currentTile.position)
        player.cache.updateViewableTiles()
        turn = session.turn()
        protection = turn.map.units.single { it.handle == handle }.protection!!
        check(protection.current.protection == "SAME_TILE_MILITARY" && protection.current.military == soldier)
        check(protection.escortCandidates.single().atSettler)
        val alone = session.movementPreview(MovementPreviewRequest(handle, TilePosition(4, 0)))
        check(alone.turns.first().afterMove!!.civilianExposure!!.protection == "UNESCORTED")
        session.execute(TurnCommandRequest("pair", turn.epoch, turn.revision, TurnCommandKind.SET_UNIT_ORDER, handle, selection = "ESCORT"))
        val paired = session.movementPreview(MovementPreviewRequest(handle, TilePosition(4, 0)))
        check(paired.turns.first().afterMove!!.civilianExposure!!.protection == "ESCORT_FORMATION")
        settler.action = "moveTo 6,0"
        protection = session.turn().map.units.single { it.handle == handle }.protection!!
        check(protection.nextStop!!.exposure.protection == "ESCORT_FORMATION")
        check(protection.nextStop.exposure.military == soldier)
        val before = json().toJson(game)
        session.turn()
        check(before == json().toJson(game)) { "Protection inspection changed native state" }
    }

    fun sightings(root: Path) {
        val checkpoint = root.resolve("checkpoint.json")
        val camp = TilePosition(4, 0)
        SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
            val game = runtime.game
            val player = game.getCivilization("Rome")
            game.turns = 10
            val brute = game.getCivilization("Barbarians").units.placeUnitNearTile(HexCoord(4, 0), "Brute")!!
            game.tileMap[4, 0].setImprovement("Barbarian encampment")
            player.cache.updateViewableTiles()
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(session.turn().map.tiles.single { it.position == camp }.units.any { it.name == "Brute" })
            check(session.turn().map.recentBarbarianSightings.isEmpty()) { "Visible enemies duplicated as historical sightings" }
            place(player.units.getCivUnits().single(), HexCoord(0, -3))
            player.cache.updateViewableTiles()
            check(!game.tileMap[4, 0].isVisible(player))
            val remembered = session.turn().map.recentBarbarianSightings.single()
            check(remembered.position == camp && remembered.name == "Brute" && remembered.lastSeenTurn == 10)
            // Hidden movement, destruction and camp removal cannot change remembered facts.
            place(brute, HexCoord(5, 0))
            brute.destroy()
            game.tileMap[4, 0].setImprovement(null)
            val hidden = game.getCivilization("Barbarians").units.placeUnitNearTile(HexCoord(8, 0), "Brute")!!
            check(!hidden.isVisibleTo(player))
            check(session.turn().map.recentBarbarianSightings == listOf(remembered))
            check(session.turn().map.tiles.single { it.position == camp }.improvement == "Barbarian encampment")
            runtime.executor.enableCheckpoints(checkpoint)
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val player = runtime.game.getCivilization("Rome")
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val remembered = session.turn().map.recentBarbarianSightings.single()
            check(remembered.position == camp && remembered.lastSeenTurn == 10)
            runtime.executor.scheduled = true
            runtime.executor.communicationPaused = true
            runtime.executor.pause()
            val boundary = runtime.executor.turnSnapshot()
            runtime.executor.resume()
            // Seeing the old position empty retires that position, without tracking the hidden unit.
            place(player.units.getCivUnits().single(), HexCoord(2, 0))
            player.cache.updateViewableTiles()
            check(session.turn().map.recentBarbarianSightings.isEmpty())
            check(session.turn().map.tiles.single { it.position == camp }.improvement == null)
            runtime.executor.pause()
            runtime.executor.restoreTurn(boundary, "rewound-sightings")
            runtime.executor.resume()
            check(session.turn().map.recentBarbarianSightings == listOf(remembered))
            runtime.game.turns = 15
            check(session.turn().map.recentBarbarianSightings == listOf(remembered))
            runtime.game.turns = 16
            check(session.turn().map.recentBarbarianSightings.isEmpty())
        }
    }
}
