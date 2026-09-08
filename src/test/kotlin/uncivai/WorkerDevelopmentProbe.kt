package uncivai

import com.unciv.json.json
import com.unciv.logic.civilization.PlayerWorkerOperations
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.mapunit.WorkerImprovementPicker
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import uncivai.player.observeWorkerDevelopment
import java.nio.file.Path
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.util.Locale
import java.util.UUID
import kotlin.math.roundToInt

object WorkerDevelopmentProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        val checkpoint = root.resolve("worker-checkpoint.json")
        lateinit var lastRequest: TurnCommandRequest
        lateinit var lastReceipt: TurnCommandReceipt
        var workerId = -1
        var remainingTurns = -1
        HeadlessRuntime.createDuel(root.resolve("initial"), 15117).use { runtime ->
            val game = runtime.game
            val workers = mutableMapOf<String, MapUnit>()
            for (name in listOf("Rome", "Greece")) {
                val civ = game.getCivilization(name)
                val settler = civ.units.getCivUnits().single { it.name == "Settler" }
                val city = civ.addCity(settler.currentTile.position, settler)
                settler.destroy()
                val tile = city.getTiles().first { !it.isCityCenter() && it.isLand && it.getUnits().none() }
                tile.baseTerrain = "Grassland"
                tile.setTerrainFeatures(emptyList())
                tile.naturalWonder = null
                tile.tileResource = null
                tile.improvement = null
                tile.setTerrainTransients()
                val worker = civ.units.placeUnitNearTile(tile.position, "Worker")!!
                check(worker.currentTile === tile)
                workers[name] = worker
                civ.tech.addTechnology("Agriculture")
                city.cityStats.update()
            }
            val player = game.getCivilization("Rome")
            val worker = workers.getValue("Rome")
            workerId = worker.id
            val other = workers.getValue("Greece")
            val host = PlayerHost(runtime)
            val rome = host.bindPlayer("Rome")
            val greece = host.bindPlayer("Greece")
            runtime.executor.enableCheckpoints(checkpoint)
            val initialRaw = json().toJson(game)
            val initial = rome.turn()
            check(initialRaw == json().toJson(game)) { "Worker or city query changed persisted game state" }
            val observed = observeWorkerDevelopment(player, worker, true)!!
            check(initial.map.units.single { it.name == "Worker" }.worker == observed)
            check(observeWorkerDevelopment(player, other, true) == null)
            check(observeWorkerDevelopment(player, worker, false)!!.options.none { it.available })
            check(!observeWorkerDevelopment(player, worker, false)!!.canStop)
            val farm = observed.options.single { it.name == "Farm" }
            check(farm.available && farm.turns > 0 && farm.yieldChanges["Food"] == "1")
            val uiPicker = WorkerImprovementPicker(worker)
            for (option in uiPicker.options().filter { it.improvement.name != "Cancel improvement order" }) {
                val stats = worker.currentTile.stats.getStatDiffForImprovement(option.improvement, player, worker.currentTile.getCity())
                stats.add(uiPicker.getMaintenance(option.improvement))
                val decimal = DecimalFormat("0.#", DecimalFormatSymbols(Locale.ROOT))
                val expected = stats.associate { it.key.name to (it.value * 10).roundToInt() }.filterValues { it != 0 }
                    .mapValues { decimal.format(it.value / 10.0) }
                check(observed.options.single { it.name == option.improvement.name }.yieldChanges == expected)
            }
            worker.currentTile.tileResource = game.ruleset.tileResources.getValue("Iron")
            check(observed == observeWorkerDevelopment(player, worker, true)) { "Unrevealed worker resource changed options or yields" }
            worker.currentTile.tileResource = game.ruleset.tileResources.getValue("Uranium")
            check(observed == observeWorkerDevelopment(player, worker, true)) { "Unrevealed worker resource identity leaked" }
            worker.currentTile.tileResource = null
            other.currentTile.startWorkingOnImprovement(game.ruleset.tileImprovements.getValue("Farm"), other.civ, other)
            check(observed == observeWorkerDevelopment(player, worker, true))
            check(initial == rome.turn()) { "Unseen foreign work changed observation or revision" }

            val handle = initial.map.units.single { it.name == "Worker" }.handle
            val foreignHandle = greece.turn().map.units.single { it.name == "Worker" }.handle
            val start = TurnCommandKind.START_WORKER_JOB
            val stop = TurnCommandKind.STOP_WORKER_JOB
            expect(PlayerSessionFailure.UNIT_UNAVAILABLE) { rome.execute(command(rome, start, foreignHandle, "Farm")) }
            expect(PlayerSessionFailure.NOT_YOUR_TURN) { greece.execute(command(greece, start, foreignHandle, "Farm")) }
            expect(PlayerSessionFailure.OPTION_UNAVAILABLE) { rome.execute(command(rome, start, handle, "Academy")) }
            expect(PlayerSessionFailure.INVALID_ARGUMENT) { rome.execute(command(rome, stop, handle, "Farm")) }
            val request = command(rome, start, handle, "Farm")
            val receipt = rome.execute(request)
            check(worker.currentTile.improvementInProgress == "Farm")
            val after = json().toJson(game)
            check(rome.execute(request) == receipt && json().toJson(game) == after)
            expect(PlayerSessionFailure.STALE_REVISION) { rome.execute(request.copy(requestId = "stale-worker")) }
            worker.currentTile.doWorkerTurn(worker)
            val progressing = observeWorkerDevelopment(player, worker, true)!!
            check(progressing.remainingTurns == farm.turns - 1)
            check(progressing.options.single { it.name == "Farm" }.turns == farm.turns - 1)
            rome.execute(command(rome, stop, handle))
            check(worker.currentTile.improvementInProgress == null)
            expect(PlayerSessionFailure.OPTION_UNAVAILABLE) { rome.execute(command(rome, stop, handle)) }
            lastRequest = command(rome, start, handle, "Farm")
            lastReceipt = rome.execute(lastRequest)
            remainingTurns = worker.currentTile.turnsToImprovement
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val player = runtime.game.getCivilization("Rome")
            val worker = player.units.getCivUnits().single { it.id == workerId }
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(worker.currentTile.improvementInProgress == "Farm" && worker.currentTile.turnsToImprovement == remainingTurns)
            val before = json().toJson(runtime.game)
            check(session.execute(lastRequest) == lastReceipt)
            check(before == json().toJson(runtime.game)) { "Retry restarted the persisted worker job" }
            check(PlayerWorkerOperations(player).canStopJob(worker))
        }
    }

    private fun command(session: PlayerSession, kind: TurnCommandKind, handle: String, selection: String? = null): TurnCommandRequest {
        val turn = session.turn()
        return TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, handle, selection)
    }

    private fun expect(reason: PlayerSessionFailure, action: () -> Unit) {
        val failure = runCatching(action).exceptionOrNull()
        check(failure is PlayerSessionException && failure.reason == reason) { "Expected $reason, got $failure" }
    }
}
