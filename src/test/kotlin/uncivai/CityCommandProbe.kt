package uncivai

import com.unciv.json.json
import com.unciv.logic.civilization.PlayerUnitOperations
import kotlinx.serialization.Serializable
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

@Serializable
private data class CityCommandRecovery(val request: TurnCommandRequest, val receipt: TurnCommandReceipt, val snapshot: PlayerTurnSnapshot)

object CityCommandProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[1])
        if (args[0] == "recover") {
            val expected = PlayerTurnJson.json.decodeFromString<CityCommandRecovery>(Files.readString(root.resolve("expected.json")))
            HeadlessRuntime.loadCheckpoint(root.resolve("game.json"), root.resolve("recovered")).use { runtime ->
                val player = PlayerHost(runtime).bindPlayer("Rome")
                check(player.turn() == expected.snapshot) { "Restart changed city observations or handles" }
                val before = json().toJson(runtime.game)
                check(player.execute(expected.request) == expected.receipt)
                check(json().toJson(runtime.game) == before) { "Restart repeated queue removal" }
            }
            return
        }
        HeadlessRuntime.createDuel(root.resolve("data"), 787L).use { runtime ->
            val game = runtime.game
            // Trusted fixture setup creates both cities without advancing a turn.
            for (name in listOf("Rome", "Greece")) {
                val civilization = game.getCivilization(name)
                game.currentPlayer = name
                game.currentPlayerCiv = civilization
                check(PlayerUnitOperations(civilization).tryFoundCity(civilization.units.getCivUnits().single { it.name == "Settler" }))
            }
            val civilization = game.getCivilization("Rome")
            game.currentPlayer = "Rome"
            game.currentPlayerCiv = civilization
            runtime.executor.enableCheckpoints(root.resolve("game.json"))
            val host = PlayerHost(runtime)
            val rome = host.bindPlayer("Rome")
            val greece = host.bindPlayer("Greece")
            val city = civilization.cities.single()
            val cityHandle = rome.turn().cities.single().handle
            val foreignHandle = greece.turn().cities.single().handle
            fun command(kind: TurnCommandKind, destination: TilePosition? = null, worked: Boolean? = null, locked: Boolean? = null, index: Int? = null, toIndex: Int? = null, selection: String? = null) = rome.turn().let { TurnCommandRequest(UUID.randomUUID().toString(), it.epoch, it.revision, kind, cityHandle, selection, destination = destination, worked = worked, locked = locked, index = index, toIndex = toIndex) }
            val initial = rome.turn()
            check(initial.cities.single().development != null && initial.research != null)
            val workedTile = initial.cities.single().development!!.tiles.first { it.canUnwork }
            val unwork = command(TurnCommandKind.SET_WORKED_TILE, workedTile.position, worked = false)
            expect(PlayerSessionFailure.CITY_UNAVAILABLE) { rome.execute(unwork.copy(target = foreignHandle)) }
            expect(PlayerSessionFailure.NOT_YOUR_TURN) {
                greece.execute(unwork.copy(epoch = greece.turn().epoch, revision = greece.turn().revision, target = foreignHandle))
            }
            for (position in listOf(TilePosition(Int.MAX_VALUE, Int.MIN_VALUE), TilePosition(-99999, 99999))) {
                expect(PlayerSessionFailure.OPTION_UNAVAILABLE) { rome.execute(unwork.copy(destination = position)) }
            }
            val hidden = game.tileMap.tileList.first { !it.isExplored(civilization) }
            val hiddenPosition = TilePosition(hidden.position.x, hidden.position.y)
            expect(PlayerSessionFailure.OPTION_UNAVAILABLE) { rome.execute(unwork.copy(destination = hiddenPosition)) }
            hidden.improvement = "Mine"
            check(rome.turn() == initial) { "Hidden tile affected city options or revision" }
            expect(PlayerSessionFailure.OPTION_UNAVAILABLE) { rome.execute(unwork.copy(destination = hiddenPosition)) }
            for (invalid in listOf(unwork.copy(worked = null), unwork.copy(locked = true), unwork.copy(index = 0), unwork.copy(selection = "anything"))) {
                expect(PlayerSessionFailure.INVALID_ARGUMENT) { rome.execute(invalid) }
            }
            val firstReceipt = rome.execute(unwork)
            check(rome.turn().cities.single().development!!.unassignedPopulation == initial.cities.single().development!!.unassignedPopulation + 1)
            check(rome.execute(unwork) == firstReceipt)
            expect(PlayerSessionFailure.REQUEST_ID_CONFLICT) { rome.execute(unwork.copy(worked = true)) }
            expect(PlayerSessionFailure.STALE_REVISION) { rome.execute(unwork.copy(requestId = "stale-city")) }
            val workTile = rome.turn().cities.single().development!!.tiles.first { it.canWork && it.position != workedTile.position }
            rome.execute(command(TurnCommandKind.SET_WORKED_TILE, workTile.position, worked = true))
            rome.execute(command(TurnCommandKind.SET_TILE_LOCKED, workTile.position, locked = true))
            check(rome.turn().cities.single().development!!.tiles.single { it.position == workTile.position }.locked)
            rome.execute(command(TurnCommandKind.SET_TILE_LOCKED, workTile.position, locked = false))
            check(!rome.turn().cities.single().development!!.tiles.single { it.position == workTile.position }.locked)

            rome.execute(command(TurnCommandKind.QUEUE_CONSTRUCTION, selection = "Worker"))
            rome.execute(command(TurnCommandKind.QUEUE_CONSTRUCTION, selection = "Scout"))
            rome.execute(command(TurnCommandKind.QUEUE_CONSTRUCTION, selection = "Worker"))
            val oldQueue = city.cityConstructions.constructionQueue.toList()
            check(oldQueue == listOf("Worker", "Scout", "Worker"))
            val reorder = command(TurnCommandKind.MOVE_CONSTRUCTION, index = 1, toIndex = 0)
            val moveReceipt = rome.execute(reorder)
            check(city.cityConstructions.constructionQueue == listOf("Scout", "Worker", "Worker"))
            check(rome.execute(reorder) == moveReceipt)
            val validRemove = command(TurnCommandKind.REMOVE_CONSTRUCTION, index = 2)
            for (invalid in listOf(validRemove.copy(index = -1), validRemove.copy(index = null), validRemove.copy(toIndex = 0))) {
                expect(PlayerSessionFailure.INVALID_ARGUMENT) { rome.execute(invalid) }
            }
            val raw = json().toJson(game)
            for (index in listOf(3, 1000000)) {
                expect(PlayerSessionFailure.OPTION_UNAVAILABLE) { rome.execute(validRemove.copy(index = index)) }
                expect(PlayerSessionFailure.OPTION_UNAVAILABLE) { rome.execute(command(TurnCommandKind.MOVE_CONSTRUCTION, index = 0, toIndex = index)) }
            }
            check(json().toJson(game) == raw) { "Rejected index changed game state" }
            val removeReceipt = rome.execute(validRemove)
            check(city.cityConstructions.constructionQueue == listOf("Scout", "Worker"))
            check(rome.execute(validRemove) == removeReceipt)
            runtime.executor.pause()
            val paused = rome.turn().cities.single().development!!
            check(paused.tiles.none { it.canWork || it.canUnwork || it.canLock || it.canUnlock })
            check(paused.constructions.none { it.canRemove || it.moveToIndices.isNotEmpty() })
            runtime.executor.resume()
            Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(CityCommandRecovery(validRemove, removeReceipt, rome.turn())))
        }
    }

    private fun expect(expected: PlayerSessionFailure, block: () -> Unit) {
        try {
            block()
            error("Expected $expected")
        } catch (failure: PlayerSessionException) {
            check(failure.reason == expected) { "Expected $expected, got ${failure.reason}" }
        }
    }
}
