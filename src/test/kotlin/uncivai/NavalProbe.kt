package uncivai

import com.unciv.json.json
import com.unciv.logic.map.HexCoord
import uncivai.player.MovementPreview
import uncivai.player.MovementPreviewRequest
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

object NavalProbe {
    private val destination = TilePosition(NavalFixtures.routeDestination.x, NavalFixtures.routeDestination.y)

    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args[0])
        try {
            when (args[1]) {
                "movement" -> movement(root)
                "naval" -> naval(root)
                "privacy" -> privacy(root)
                "recovery" -> recovery(root)
                "handoff" -> handoff(root)
                else -> error("Unknown scenario")
            }
            println("NAVAL_${args[1]}_PASS")
        } catch (failure: Throwable) {
            failure.printStackTrace()
            kotlin.system.exitProcess(1)
        }
    }

    private fun request(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection).reviewedFor(turn)

    private fun preview(api: GameplayTestApi, token: String, target: String, tile: TilePosition = destination): MovementPreview = PlayerTurnJson.json.decodeFromString(api.tool(token, "movement_preview", obj("target" to target, "destination" to obj("x" to tile.x, "y" to tile.y))).toString())

    private fun GameplayTestApi.finish(token: String) = finishTurn(token, heartbeat = false)

    private fun movement(root: Path) {
        val checkpoint = root.resolve("checkpoint.json")
        lateinit var order: TurnCommandRequest
        lateinit var receipt: TurnCommandReceipt
        lateinit var handle: String
        NavalFixtures.create(root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls"), NavalFixtures.tokens).use { api ->
                val token = api.playerToken("Rome")
                val turn = api.turn(token)
                handle = turn.map.units.single { it.displayName.contains("Voyager") }.handle
                order = request(turn, TurnCommandKind.SET_UNIT_DESTINATION, handle).copy(destination = destination)
                api.reject(token, order, "OBSERVATION_REQUIRED")
                val native = json().toJson(runtime.game)
                val offered = preview(api, token, handle)
                check(json().toJson(runtime.game) == native && offered.available && offered.estimatedTurns!! > 1)
                check(offered.turns.last().destination == destination)
                check(!preview(api, token, handle, TilePosition(Int.MAX_VALUE, Int.MIN_VALUE)).available)
                check(!preview(api, token, handle, TilePosition(6, 0)).available)
                receipt = api.execute(token, order)
                check(receipt.movement!!.end == offered.turns.first().destination)
                check(receipt.movement!!.orderAfter!!.destination == destination)
                check(receipt.movement!!.end != destination)
                check(api.execute(token, order) == receipt)
                api.reject(token, order.copy(requestId = "old"), "STALE_REVISION")
                api.finish(token)
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            GameplayTestApi(runtime, root.resolve("tls-restored"), NavalFixtures.tokens).use { api ->
                val token = api.playerToken("Rome")
                check(api.execute(token, order) == receipt)
                val waiting = api.turn(token).map.units.single { it.handle == handle }
                check(waiting.movementOrder?.destination == destination)
                api.finish(api.playerToken("Greece"))
                api.finish(api.playerToken("France"))
                val turn = api.turn(token)
                val arrived = turn.map.units.single { it.handle == handle }
                check(arrived.position == destination && arrived.movementOrder == null)
                // Replace a new order, then cancel it even when the first move consumed all movement.
                val replacement = TilePosition(8, -3)
                check(preview(api, token, handle, replacement).available)
                val moved = api.execute(token, request(turn, TurnCommandKind.SET_UNIT_DESTINATION, handle).copy(destination = replacement))
                check(moved.movement!!.orderAfter != null)
                val cancelTurn = api.turn(token)
                val cancelled = api.execute(token, request(cancelTurn, TurnCommandKind.CANCEL_UNIT_MOVEMENT, handle))
                check(cancelled.movement!!.orderBefore != null && cancelled.movement!!.orderAfter == null)
                check(cancelled.movement!!.start == cancelled.movement!!.end)
                check(cancelled.movement!!.movementBefore == cancelled.movement!!.movementAfter)
                api.reject(token, request(api.turn(token), TurnCommandKind.CANCEL_UNIT_MOVEMENT, handle), "OPTION_UNAVAILABLE")
            }
        }
    }

    private fun naval(root: Path) = NavalFixtures.create(root.resolve("initial")).use { runtime ->
        runtime.executor.enableCheckpoints(root.resolve("checkpoint.json"))
        GameplayTestApi(runtime, root.resolve("tls"), NavalFixtures.tokens).use { api ->
            val token = api.playerToken("Rome")
            var turn = api.turn(token)
            val boat = turn.map.units.single { it.name == "Work Boats" }
            check(boat.waterImprovement!!.available && boat.waterImprovement!!.consumesUnit)
            val build = request(turn, TurnCommandKind.CREATE_WATER_IMPROVEMENT, boat.handle, boat.waterImprovement!!.name)
            val built = api.execute(token, build)
            check(built.waterImprovement!!.unitConsumed && built.waterImprovement!!.improvementAfter == "Fishing Boats")
            check(built.waterImprovement!!.availableResourceAfter > built.waterImprovement!!.availableResourceBefore)
            check(api.execute(token, build) == built)
            turn = api.turn(token)
            api.reject(token, request(turn, TurnCommandKind.CREATE_WATER_IMPROVEMENT, boat.handle, "Fishing Boats"), "UNIT_UNAVAILABLE")
            val ship = turn.map.units.single { it.name == "Trireme" }
            val upgrade = ship.economy!!.upgrades.single { it.targetName == "Caravel" }
            check(upgrade.available)
            val rome = runtime.game.getCivilization("Rome")
            rome.addGold(-rome.gold)
            turn = api.turn(token)
            check(!turn.map.units.single { it.handle == ship.handle }.economy!!.upgrades.single { it.targetName == "Caravel" }.available)
            api.reject(token, request(turn, TurnCommandKind.UPGRADE_UNIT, ship.handle, "Caravel").copy(confirmAction = true), "OPTION_UNAVAILABLE")
            rome.addGold(upgrade.goldCost!!)
            turn = api.turn(token)
            val upgraded = api.execute(token, request(turn, TurnCommandKind.UPGRADE_UNIT, ship.handle, "Caravel").copy(confirmAction = true))
            check(upgraded.economy!!.goldChange == -upgrade.goldCost!! && rome.gold == 0)
            turn = api.turn(token)
            val war = api.execute(token, request(turn, TurnCommandKind.DECLARE_WAR, "Greece").copy(confirmWar = true))
            check(war.kind == TurnCommandKind.DECLARE_WAR)
            turn = api.turn(token)
            val frigate = turn.map.units.single { it.name == "Frigate" }
            val target = frigate.combat!!.attacks.first { it.destination == TilePosition(6, -1) }
            val attacked = api.execute(token, request(turn, TurnCommandKind.ATTACK, frigate.handle).copy(destination = target.destination, attackFrom = target.attackFrom))
            check(attacked.combat!!.attacked && attacked.combat!!.damageToDefender > 0)
            check(attacked.combat!!.damageToAttacker == 0)
            api.finish(token)
            api.finish(api.playerToken("Greece"))
            val france = api.playerToken("France")
            val frenchTurn = api.turn(france)
            val settler = frenchTurn.map.units.single { it.name == "Settler" }
            val island = TilePosition(NavalFixtures.settlement.x, NavalFixtures.settlement.y)
            check(preview(api, france, settler.handle, island).available)
            api.execute(france, request(frenchTurn, TurnCommandKind.SET_UNIT_DESTINATION, settler.handle).copy(destination = island))
            repeat(4) {
                if (api.turn(france).map.units.single { it.handle == settler.handle }.position != island) {
                    api.finish(france)
                    api.finish(token)
                    api.finish(api.playerToken("Greece"))
                }
            }
            val landing = api.turn(france)
            check(landing.map.units.single { it.handle == settler.handle }.position == island)
            if (!landing.map.units.single { it.handle == settler.handle }.canFoundCity) {
                api.finish(france)
                api.finish(token)
                api.finish(api.playerToken("Greece"))
            }
            val ready = api.turn(france)
            check(ready.map.units.single { it.handle == settler.handle }.canFoundCity)
            api.execute(france, request(ready, TurnCommandKind.FOUND_CITY, settler.handle))
            check(api.turn(france).cities.size == 2)
        }
    }

    private fun privacy(root: Path) = NavalFixtures.create(root.resolve("initial")).use { runtime ->
        val rome = runtime.game.getCivilization("Rome")
        val voyager = rome.units.getCivUnits().single { it.instanceName == "Voyager" }
        val hidden = runtime.game.tileMap[0, -2]
        hidden.setExplored(rome, false)
        rome.viewableTiles = setOf(voyager.currentTile)
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val turn = session.turn()
        val target = turn.map.units.single { it.displayName.contains("Voyager") }.handle
        val query = MovementPreviewRequest(target, destination)
        val before = session.movementPreview(query)
        hidden.setBaseTerrain(runtime.game.ruleset.terrains.getValue("Mountain"))
        val afterTerrain = session.movementPreview(query)
        check(before == afterTerrain)
        val enemy = NavalFixtures.add(runtime, "Greece", "Caravel", 0, -3, "Unseen ship")
        rome.viewableTiles = setOf(voyager.currentTile)
        check(!enemy.isVisibleTo(rome))
        check(before == session.movementPreview(query))
        check(session.turn().map.tiles.none { it.position == TilePosition(0, -2) })
    }

    private fun recovery(root: Path) {
        for (kind in listOf(TurnCommandKind.SET_UNIT_DESTINATION, TurnCommandKind.CREATE_WATER_IMPROVEMENT)) {
            for (afterReplace in listOf(false, true)) {
                val dir = root.resolve("$kind-$afterReplace")
                val checkpoint = dir.resolve("checkpoint.json")
                var failCommit = false
                lateinit var command: TurnCommandRequest
                NavalFixtures.create(dir.resolve("initial")).use { runtime ->
                    runtime.executor.enableCheckpoints(CheckpointStore(checkpoint, beforeReplace = { if (failCommit && !afterReplace) error("Interrupted write") }, afterReplace = { if (failCommit && afterReplace) error("Lost reply") }))
                    val session = PlayerHost(runtime).bindPlayer("Rome")
                    val turn = session.turn()
                    val unit = turn.map.units.single { if (kind == TurnCommandKind.SET_UNIT_DESTINATION) it.displayName.contains("Voyager") else it.name == "Work Boats" }
                    command = if (kind == TurnCommandKind.SET_UNIT_DESTINATION) {
                        check(session.movementPreview(MovementPreviewRequest(unit.handle, destination)).available)
                        request(turn, kind, unit.handle).copy(destination = destination)
                    } else {
                        request(turn, kind, unit.handle, unit.waterImprovement!!.name)
                    }
                    failCommit = true
                    check(runCatching { session.execute(command) }.isFailure)
                    check(runtime.executor.mutationFailure != null)
                }
                HeadlessRuntime.loadCheckpoint(checkpoint, dir.resolve("restored")).use { runtime ->
                    val session = PlayerHost(runtime).bindPlayer("Rome")
                    val retry = if (afterReplace) {
                        command
                    } else {
                        val fresh = session.turn()
                        if (kind == TurnCommandKind.SET_UNIT_DESTINATION) {
                            val unread = command.copy(revision = fresh.revision)
                            try {
                                session.execute(unread)
                                error("Missing preview was accepted")
                            } catch (failure: PlayerSessionException) {
                                check(failure.reason == PlayerSessionFailure.OBSERVATION_REQUIRED)
                            }
                            check(session.movementPreview(MovementPreviewRequest(command.target!!, destination)).available)
                        }
                        command.copy(requestId = "fresh", revision = fresh.revision)
                    }
                    val receipt = session.execute(retry)
                    check(if (kind == TurnCommandKind.SET_UNIT_DESTINATION) receipt.movement?.orderAfter != null else receipt.waterImprovement?.unitConsumed == true)
                    val saved = Files.readString(checkpoint)
                    val native = json().toJson(runtime.game)
                    check(session.execute(retry) == receipt)
                    check(saved == Files.readString(checkpoint) && native == json().toJson(runtime.game))
                }
            }
        }
    }

    private fun handoff(root: Path) {
        for (afterReplace in listOf(false, true)) {
            val dir = root.resolve(afterReplace.toString())
            val checkpoint = dir.resolve("checkpoint.json")
            var failCommit = false
            lateinit var command: TurnCommandRequest
            NavalFixtures.create(dir.resolve("initial")).use { runtime ->
                runtime.executor.enableCheckpoints(CheckpointStore(checkpoint, beforeReplace = { if (failCommit && !afterReplace) error("Interrupted handoff") }, afterReplace = { if (failCommit && afterReplace) error("Lost handoff reply") }))
                GameplayTestApi(runtime, dir.resolve("tls"), NavalFixtures.tokens).use { api ->
                    val token = api.playerToken("Rome")
                    val turn = api.turn(token)
                    val unit = turn.map.units.single { it.displayName.contains("Voyager") }
                    check(preview(api, token, unit.handle).available)
                    api.execute(token, request(turn, TurnCommandKind.SET_UNIT_DESTINATION, unit.handle).copy(destination = destination))
                    api.finish(token)
                    api.finish(api.playerToken("Greece"))
                }
                val session = PlayerHost(runtime).bindPlayer("France")
                val turn = session.turn()
                command = request(turn, TurnCommandKind.END_TURN)
                failCommit = true
                check(runCatching { session.execute(command) }.isFailure)
                check(runtime.executor.mutationFailure != null)
            }
            HeadlessRuntime.loadCheckpoint(checkpoint, dir.resolve("restored")).use { runtime ->
                val session = PlayerHost(runtime).bindPlayer("France")
                val retry = if (afterReplace) command else request(session.turn(), TurnCommandKind.END_TURN)
                val receipt = session.execute(retry)
                check(session.execute(retry) == receipt)
                val voyager = runtime.game.getCivilization("Rome").units.getCivUnits().single { it.instanceName == "Voyager" }
                check(voyager.currentTile.position == NavalFixtures.routeDestination && !voyager.isMoving())
                check(runtime.game.currentPlayer == "Rome")
            }
        }
    }
}
