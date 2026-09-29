package uncivai

import com.unciv.json.json
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.UnitTurnManager
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.MovementPreview
import uncivai.player.PlayerTurnJson
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path

internal object CivilianMovementProbe {
    fun run(root: Path) {
        for (kind in listOf(TurnCommandKind.MOVE_UNIT, TurnCommandKind.SET_UNIT_DESTINATION)) {
            refusal(root.resolve("refusal-$kind"), kind)
            for (leader in listOf("Settler", "Warrior")) escorted(root.resolve("escort-$kind-$leader"), kind, leader)
        }
    }

    private fun fixture(root: Path) = SettlementPreviewProbe.fixture(root).also { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val settler = player.units.getCivUnits().single()
        settler.removeFromTile()
        settler.putInTile(game.tileMap[0, 2])
        settler.currentMovement = 2f
        val enemy = game.getCivilization("Greece")
        enemy.units.placeUnitNearTile(HexCoord(4, 2), "Warrior")!!
        player.diplomacyFunctions.makeCivilizationsMeet(enemy)
        player.getDiplomacyManager(enemy)!!.declareWar()
        player.popupAlerts.clear()
        player.viewableTiles = game.tileMap.values.toSet()
    }

    private fun refusal(root: Path, kind: TurnCommandKind) = fixture(root).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val settler = player.units.getCivUnits().single()
        GameplayTestApi(runtime, root.resolve("tls")).use { api ->
            val token = api.playerToken("Rome")
            val turn = api.turn(token)
            val unit = turn.map.units.single()
            val destination = if (kind == TurnCommandKind.MOVE_UNIT) TilePosition(2, 2) else TilePosition(7, 2)
            val risk = if (kind == TurnCommandKind.MOVE_UNIT) {
                unit.moves.single { it.destination == destination }.afterMove!!.captureRisk
            } else {
                val preview = PlayerTurnJson.json.decodeFromString<MovementPreview>(api.tool(token, "movement_preview",
                    obj("target" to unit.handle, "destination" to obj("x" to destination.x, "y" to destination.y))).toString())
                check(preview.available)
                preview.turns.first().afterMove!!.captureRisk
            }
            checkNotNull(risk)
            check(risk.threats.single().position == TilePosition(4, 2))
            val command = TurnCommandRequest("unconfirmed", turn.epoch, turn.revision, kind, unit.handle, destination = destination)
            val before = json().toJson(game)
            val error = api.reject(token, command, "CIVILIAN_CAPTURE_RISK")
            check(!error.getValue("applied").jsonPrimitive.boolean && error.getValue("recoverable").jsonPrimitive.boolean)
            check(error.getValue("captureRisk").jsonObject.getValue("destination") ==
                PlayerTurnJson.json.encodeToJsonElement(TilePosition.serializer(), risk.destination))
            val recovery = error.getValue("recovery").jsonObject
            check(recovery.getValue("confirmationField").jsonPrimitive.content == "confirmAction")
            check("confirmAction" !in recovery.getValue("command").jsonObject)
            check(before == json().toJson(game)) { "Rejected movement changed native state" }
            check(api.turn(token).revision == turn.revision)
            val accepted = command.copy(requestId = "confirmed", confirmAction = true)
            val receipt = api.execute(token, accepted)
            check(receipt.movement!!.end == risk.destination)
            check(api.execute(token, accepted) == receipt) { "Confirmed movement replayed twice" }
            if (kind == TurnCommandKind.SET_UNIT_DESTINATION) {
                val stop = settler.currentTile
                UnitTurnManager(settler).startTurn()
                player.viewableTiles = game.tileMap.values.toSet()
                settler.doAction()
                check(settler.currentTile === stop && !settler.isMoving()) { "Confirmation leaked into future movement" }
            }
        }
    }

    private fun escorted(root: Path, kind: TurnCommandKind, leader: String) = fixture(root).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val civilian = player.units.getCivUnits().single()
        val warrior = player.units.placeUnitNearTile(civilian.currentTile.position, "Warrior")!!
        check(warrior.currentTile === civilian.currentTile)
        player.viewableTiles = game.tileMap.values.toSet()
        GameplayTestApi(runtime, root.resolve("tls")).use { api ->
            val token = api.playerToken("Rome")
            var turn = api.turn(token)
            val handle = turn.map.units.single { it.name == leader }.handle
            api.execute(token, TurnCommandRequest("escort", turn.epoch, turn.revision,
                TurnCommandKind.SET_UNIT_ORDER, handle, selection = "ESCORT"))
            turn = api.turn(token)
            val destination = TilePosition(2, 2)
            if (kind == TurnCommandKind.SET_UNIT_DESTINATION) {
                val preview = PlayerTurnJson.json.decodeFromString<MovementPreview>(api.tool(token, "movement_preview",
                    obj("target" to handle, "destination" to obj("x" to destination.x, "y" to destination.y))).toString())
                check(preview.escort != null && preview.turns.first().afterMove!!.captureRisk == null)
            } else {
                check(turn.map.units.single { it.handle == handle }.moves.single { it.destination == destination }.afterMove!!.captureRisk == null)
            }
            val receipt = api.execute(token, TurnCommandRequest("move", turn.epoch, turn.revision, kind, handle, destination = destination))
            check(receipt.movement!!.end == destination)
            check(civilian.currentTile === warrior.currentTile && civilian.isEscorting())
        }
    }
}
