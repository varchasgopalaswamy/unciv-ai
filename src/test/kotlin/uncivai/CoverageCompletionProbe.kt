package uncivai

import com.unciv.Constants
import com.unciv.json.json
import com.unciv.logic.city.CityFlags
import com.unciv.logic.civilization.Notification
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PopupAlert
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.tile.TileHistory
import com.unciv.logic.map.tile.RoadStatus
import kotlinx.serialization.json.*
import uncivai.player.*
import java.nio.file.Path
import java.util.UUID

object CoverageCompletionProbe {
    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null) =
        TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection).reviewedFor(turn)

    fun controls(root: Path) = SettlementPreviewProbe.fixture(root.resolve("game")).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val city = player.cities.single()
        val warrior = player.units.placeUnitNearTile(HexCoord(1, 0), "Warrior")!!
        val partner = player.units.placeUnitNearTile(HexCoord(0, 1), "Warrior")!!
        val worker = player.units.placeUnitNearTile(HexCoord(0, 0), "Worker")!!
        player.tech.addTechnology("The Wheel")
        player.popupAlerts.clear()
        runtime.executor.enableCheckpoints(root.resolve("checkpoint.json"))
        GameplayTestApi(runtime, root.resolve("tls")).use { api ->
            val token = api.playerToken("Rome")
            var turn = api.turn(token)
            val handle = turn.map.units.single { it.position == TilePosition(1, 0) }.handle
            val rename = command(turn, TurnCommandKind.RENAME_UNIT, handle, "First Legion")
            val renamed = api.execute(token, rename)
            check(api.execute(token, rename) == renamed)
            check(warrior.instanceName == "First Legion")
            turn = api.turn(token)
            api.execute(token, command(turn, TurnCommandKind.SAVE_UNIT_PROMOTIONS, handle))
            turn = api.turn(token)
            check(turn.cities.single().management!!.defaultPromotions.single().enabled)
            api.execute(token, command(turn, TurnCommandKind.SET_DEFAULT_PROMOTIONS, turn.cities.single().handle, "Warrior").copy(enabled = false))
            check(city.unitShouldUseSavedPromotion["Warrior"] == false)
            turn = api.turn(token)
            val swap = turn.map.units.single { it.handle == handle }.controls!!.swaps.single { it.destination == TilePosition(0, 1) }
            check(swap.partners.size == 1)
            api.execute(token, command(turn, TurnCommandKind.SWAP_UNITS, handle).copy(destination = swap.destination))
            check(warrior.currentTile.position == HexCoord(0, 1) && partner.currentTile.position == HexCoord(1, 0))
            turn = api.turn(token)
            val workerHandle = turn.map.units.single { it.name == "Worker" }.handle
            val road = command(turn, TurnCommandKind.CONNECT_ROAD, workerHandle).copy(destination = TilePosition(0, 3))
            api.reject(token, road, "OBSERVATION_REQUIRED")
            val before = json().toJson(game)
            val preview = api.tool(token, "player_inspect", obj("subject" to "ROAD", "target" to workerHandle, "destination" to obj("x" to 0, "y" to 3)))
            check(preview["data"]!!.jsonObject["available"]!!.jsonPrimitive.boolean)
            check(preview["data"]!!.jsonObject["workerTurns"]!!.jsonPrimitive.int > 0)
            check(preview["data"]!!.jsonObject["completionEffects"] is JsonObject)
            check(before == json().toJson(game))
            val receipt = api.execute(token, road)
            check(api.execute(token, road) == receipt)
            check(worker.automatedRoadConnectionDestination == HexCoord(0, 3))
            turn = api.turn(token)
            api.execute(token, command(turn, TurnCommandKind.SET_UNIT_ORDER, workerHandle, "STOP_AUTOMATION"))
            check(worker.automatedRoadConnectionDestination == null)
            // Partly repaired improvements and roads are separate jobs; only the
            // first repair inherits the saved progress, followed by the rail upgrade.
            player.tech.addTechnology("Railroads")
            val destination = game.tileMap[HexCoord(0, 3)]
            val damaged = PlayerUnitOperations(player).roadPath(worker, destination)!!.first { !it.isCityCenter() }
            damaged.setRoadStatus(RoadStatus.Road, player)
            damaged.roadIsPillaged = true
            damaged.improvement = "Farm"
            damaged.improvementIsPillaged = true
            damaged.stopWorkingOnImprovement()
            damaged.queueImprovement(Constants.repair, 1)
            val repairBefore = json().toJson(game)
            val repairPreview = api.tool(token, "player_inspect", obj("subject" to "ROAD", "target" to workerHandle, "destination" to obj("x" to 0, "y" to 3)))["data"]!!.jsonObject
            val jobs = repairPreview["work"]!!.jsonArray.single {
                it.jsonObject["position"] == obj("x" to damaged.position.x, "y" to damaged.position.y)
            }.jsonObject["jobs"]!!.jsonArray.map { it.jsonObject }
            check(jobs.map { it["job"]!!.jsonPrimitive.content } == listOf(Constants.repair, Constants.repair, "Railroad"))
            check(jobs[0]["workerTurns"]!!.jsonPrimitive.int == 1)
            check(jobs[1]["workerTurns"]!!.jsonPrimitive.int == game.ruleset.tileImprovements.getValue(Constants.repair).getTurnsToBuild(player, worker))
            check(json().toJson(game) == repairBefore)
            player.popupAlerts.clear()
            val escort = player.units.placeUnitNearTile(HexCoord(3, 0), "Warrior")!!
            val guarded = player.units.placeUnitNearTile(HexCoord(3, 0), "Worker")!!
            val returning = player.units.placeUnitNearTile(HexCoord(3, 1), "Worker")!!
            escort.startEscorting()
            game.getBarbarianCivilization().units.placeUnitNearTile(HexCoord(4, 0), "Brute")!!
            val swapBefore = json().toJson(game)
            turn = api.turn(token)
            check(json().toJson(game) == swapBefore) { "Swap inspection mutated the live game" }
            val guardedHandle = turn.map.units.single { it.position == TilePosition(3, 0) && it.name == "Warrior" }.handle
            val exposed = turn.map.units.single { it.handle == guardedHandle }.controls!!.swaps.single { it.destination == TilePosition(3, 1) }
            check(exposed.captureRisks.single().risk.destination == TilePosition(3, 0))
            check(exposed.captureRisks.single().unit == turn.map.units.single { it.position == TilePosition(3, 1) }.handle)
            val exchange = command(turn, TurnCommandKind.SWAP_UNITS, guardedHandle).copy(destination = exposed.destination)
            api.reject(token, exchange, "CIVILIAN_CAPTURE_RISK")
            check(json().toJson(game) == swapBefore)
            api.execute(token, exchange.copy(requestId = UUID.randomUUID().toString(), confirmAction = true))
            check(escort.currentTile === guarded.currentTile && escort.isEscorting())
            check(returning.currentTile.position == HexCoord(3, 0) && returning.currentTile.militaryUnit == null)
            turn = api.turn(token)
            val oldTurn = turn.turn
            api.execute(token, command(turn, TurnCommandKind.AUTOPLAY, selection = "ECONOMY"))
            check(game.turns == oldTurn && game.currentPlayer == "Rome")
            // The native menu may defer ordinary pickers, but not a capture decision.
            player.popupAlerts.clear()
            player.tech.techsToResearch.clear()
            city.cityConstructions.constructionQueue.clear()
            turn = api.turn(token)
            check(!turn.canEndTurn && turn.turnControls.canDeferChoices)
            api.reject(token, command(turn, TurnCommandKind.END_TURN), "TURN_BLOCKED")
            api.execute(token, command(turn, TurnCommandKind.END_TURN).copy(deferChoices = true))
            check(game.currentPlayer != "Rome")
            val inactive = api.tool(token, "player_inspect", obj("subject" to "ROAD", "target" to workerHandle, "destination" to obj("x" to 0, "y" to 3)))
            check(!inactive["data"]!!.jsonObject["available"]!!.jsonPrimitive.boolean)
        }
    }

    fun information(root: Path) = SettlementPreviewProbe.fixture(root.resolve("game")).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val france = game.getCivilization("France")
        val greece = game.getCivilization("Greece")
        val city = player.cities.single()
        player.diplomacyFunctions.makeCivilizationsMeet(france)
        france.diplomacyFunctions.makeCivilizationsMeet(greece)
        france.getDiplomacyManager(greece)!!.setFlag(DiplomacyFlags.Denunciation, 12)
        val unit = player.units.getCivUnits().single()
        unit.setStatus("Shock I", 3)
        city.setFlag(CityFlags.Resistance, 2)
        city.setFlag(CityFlags.WeLoveTheKing, 7)
        city.demandedResource = "Gold"
        game.turns = 6
        val hiddenCity = france.cities.single()
        hiddenCity.cityConstructions.addBuilding("The Great Library")
        hiddenCity.getCenterTile().setExplored(player, false)
        player.viewableTiles = game.tileMap.values.filter { it !== hiddenCity.getCenterTile() }.toSet()
        player.notificationsLog.add(Civilization.NotificationsLog(2).apply { notifications.add(Notification("Old event", emptyArray(), null)) })
        city.getCenterTile().history.addTestEntry(1, TileHistory.TileHistoryState(player.civID, TileHistory.TileHistoryState.CityCenterType.Capital))
        player.popupAlerts.clear()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val before = json().toJson(game)
        val turn = session.turn()
        val development = turn.cities.single().development!!
        check(development.resistanceTurns == 2 && development.weLoveTheKingTurns == 7 && development.demandedResource == "Gold")
        check(development.borderExpansion.cultureCost > 0 && development.borderExpansion.turns == null)
        check(turn.map.units.single().statuses.single().turnsRemaining == 3)
        GameplayTestApi(runtime, root.resolve("tls")).use { api ->
            val token = api.playerToken("Rome")
            fun inspect(subject: String, vararg fields: Pair<String, Any?>) = api.tool(token, "player_inspect", obj("subject" to subject, *fields))["data"]!!.jsonObject
            val politics = inspect("POLITICS")
            check(politics.toString().contains("France"))
            check(!politics.toString().contains("Greece"))
            val wonders = inspect("WONDERS")["items"]!!.jsonArray
            val library = wonders.single { it.jsonObject["name"]?.jsonPrimitive?.content == "The Great Library" }.jsonObject
            check(library["status"]!!.jsonPrimitive.content == "NotFound")
            check(library["city"] == JsonNull && library["position"] == JsonNull && library["civilizationId"] == JsonNull)
            val history = inspect("NOTIFICATIONS", "turn" to 2)
            check(history.toString().contains("Old event"))
            check(!history.toString().contains("canDismiss"))
            val replay = inspect("REPLAY", "turn" to 1, "limit" to 1)
            check(replay["items"]!!.jsonArray.size == 1 && replay["nextOffset"]!!.jsonPrimitive.int == 1)
            val replayAll = session.inspect(PlayerInspectionRequest(InspectionSubject.REPLAY, turn = 1, limit = 100)).data
            check(!replayAll.toString().contains("France"))
            val pedia = inspect("CIVILOPEDIA", "category" to "Unit", "names" to listOf("Worker"))
            check(pedia["items"]!!.jsonArray.single().jsonObject["lines"]!!.jsonArray.isNotEmpty())
            val roadReference = inspect("CIVILOPEDIA", "category" to "Improvement", "names" to listOf("Road", "Railroad"))
            check(roadReference.toString().contains("Reduces movement cost") && roadReference.toString().contains("25%"))
            val flavor = inspect("CIVILOPEDIA", "names" to listOf("Technology/Future Tech", "Unit/Warrior"))
            check(!flavor.toString().contains("Who knows what the future holds") && !flavor.toString().contains("club-swinging"))
            val catalog = inspect("CIVILOPEDIA", "search" to "warrior", "limit" to 1)
            check(catalog["items"]!!.jsonArray.size == 1)
            val tile = inspect("TILE", "destination" to obj("x" to 0, "y" to 0))
            check(tile["yieldBreakdown"]!!.jsonArray.isNotEmpty() && "defensePercent" in tile && "freshWater" in tile)
            val units = inspect("UNIT", "target" to turn.map.units.single().handle)
            check(units["units"]!!.jsonArray.single().jsonObject["statuses"]!!.jsonArray.size == 1)
            check(!units.toString().contains("originalOwner"))
        }
        check(before == json().toJson(game)) { "Inspection mutated native state" }
        // An arbitrary hidden unit position must not reveal even unit existence.
        val failure = runCatching { session.inspect(PlayerInspectionRequest(InspectionSubject.UNIT, destination = TilePosition(hiddenCity.location.x, hiddenCity.location.y))) }.exceptionOrNull()
        check(failure is PlayerSessionException && failure.reason == PlayerSessionFailure.OPTION_UNAVAILABLE)
        player.popupAlerts.add(PopupAlert(AlertType.CityConquered, city.id))
        check(!session.turn().turnControls.canDeferChoices)
    }
}
