package uncivai

import com.unciv.logic.map.HexCoord
import uncivai.player.PlayerHost
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path

/** Exercise actual native orders through the player boundary, including durable replay. */
object UnitAutomationProbe {
    fun run(root: Path) {
        val checkpoint = root.resolve("checkpoint.json")
        SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
            val player = runtime.game.getCivilization("Rome")
            player.units.getCivUnits().toList().forEach { it.destroy() }
            val scout = player.units.placeUnitNearTile(HexCoord(2, 0), "Scout")!!
            scout.currentMovement = 0f
            val worker = player.units.placeUnitNearTile(HexCoord(1, 0), "Worker")!!
            player.tech.addTechnology("Agriculture", false)
            player.cities.single().expansion.takeOwnership(worker.currentTile)
            worker.currentTile.setTileResource(runtime.game.ruleset.tileResources.getValue("Wheat"))
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                val token = api.playerToken("Rome")
                var turn = api.turn(token)
                val explorer = turn.map.units.single { it.name == "Scout" }
                check(explorer.combat!!.orders.single { it.name == "EXPLORE" }.available)
                val explore = TurnCommandRequest("explore", turn.epoch, turn.revision, TurnCommandKind.SET_UNIT_ORDER, explorer.handle, "EXPLORE")
                val receipt = api.execute(token, explore)
                check(api.execute(token, explore) == receipt)
                turn = api.turn(token)
                check(turn.map.units.single { it.name == "Scout" }.combat!!.order!!.action == "Explore")
                val builder = turn.map.units.single { it.name == "Worker" }
                check(builder.combat!!.orders.single { it.name == "AUTOMATE" }.available)
                val automate = TurnCommandRequest("automate", turn.epoch, turn.revision, TurnCommandKind.SET_UNIT_ORDER, builder.handle, "AUTOMATE")
                val automated = api.execute(token, automate)
                check(api.execute(token, automate) == automated)
                val after = api.turn(token).map.units.single { it.name == "Worker" }
                check(after.combat!!.order!!.automated && !after.idle)
                check(worker.currentTile.improvementInProgress == "Farm")
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("Rome")
            var turn = session.turn()
            val scout = turn.map.units.single { it.name == "Scout" }
            check(scout.combat!!.order!!.action == "Explore")
            check(scout.combat.orders.single { it.name == "STOP_EXPLORATION" }.available)
            session.execute(TurnCommandRequest("stop-explore", turn.epoch, turn.revision, TurnCommandKind.SET_UNIT_ORDER, scout.handle, "STOP_EXPLORATION"))
            turn = session.turn()
            val worker = turn.map.units.single { it.name == "Worker" }
            check(worker.combat!!.order!!.automated)
            session.execute(TurnCommandRequest("stop-automate", turn.epoch, turn.revision, TurnCommandKind.SET_UNIT_ORDER, worker.handle, "STOP_AUTOMATION"))
            turn = session.turn()
            check(turn.map.units.none { it.combat!!.order!!.automated || it.combat.order.action == "Explore" })
        }
    }
}
