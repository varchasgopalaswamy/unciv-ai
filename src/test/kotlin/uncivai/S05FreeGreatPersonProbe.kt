package uncivai

import com.unciv.Constants
import com.unciv.json.json
import kotlinx.serialization.json.jsonObject
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** Every free-person option, native counters and recovery use player_command. */
object S05FreeGreatPersonProbe {
    @JvmStatic fun main(args: Array<String>) {
        val saves = Path.of(args[0])
        val root = Path.of(args[1])
        for ((file, player) in listOf("ordinary-vanilla" to "Mongolia", "ordinary" to "The Maya")) {
            everyOption(saves.resolve("$file.json"), root.resolve(file), player)
        }
        maya(saves.resolve("maya.json"), root.resolve("maya"))
        for (file in listOf("no-city", "empty-maya-pool")) {
            HeadlessRuntime.load(saves.resolve("$file.json"), root.resolve(file)).use { runtime ->
                val session = PlayerHost(runtime).bindPlayer("The Maya")
                val turn = session.turn()
                check(turn.freeGreatPeople == 1 && turn.greatPersonOptions.isEmpty())
                reject(PlayerSessionFailure.OPTION_UNAVAILABLE) { session.execute(choice(session, "Great Scientist")) }
                check(session.turn().freeGreatPeople == 1)
            }
        }
        blocked(saves.resolve("blocked-placement.json"), root.resolve("blocked"))
        println("S05-VERIFIED")
    }

    private fun everyOption(save: Path, data: Path, humanName: String) {
        val (names, water) = HeadlessRuntime.load(save, data.resolve("discover")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer(humanName)
            val before = raw(runtime.game)
            val turn = session.turn()
            check(raw(runtime.game) == before && turn == session.turn())
            check(turn.freeGreatPeople == 1 && turn.greatPersonOptions.isNotEmpty())
            check(turn.greatPersonOptions == turn.greatPersonOptions.distinct().sorted())
            if (humanName == "Mongolia") check("Khan" in turn.greatPersonOptions && "Great General" !in turn.greatPersonOptions)
            turn.greatPersonOptions to turn.greatPersonOptions.filter { runtime.game.ruleset.units.getValue(it).isWaterUnit }.toSet()
        }
        for ((i, name) in names.withIndex()) {
            val source = if (name in water) prepareCoastalSave(save, data.resolve("coast-$i.json"), data.resolve("prepare-$i"), humanName) else save
            HeadlessRuntime.load(source, data.resolve("option-$i")).use { runtime ->
                val human = runtime.game.getCivilization(humanName)
                val session = PlayerHost(runtime).bindPlayer(humanName)
                check(session.turn().greatPersonOptions == names)
                val beforeIds = human.units.getCivUnits().map { it.id }.toSet()
                val counters = Triple(human.policies.freePolicies, human.tech.freeTechs, human.greatPeople.mayaLimitedFreeGP)
                val request = choice(session, name)
                val pool = Executors.newFixedThreadPool(2)
                val receipt = try {
                    val receipts = pool.invokeAll(List(2) { Callable { session.execute(request) } }).map { it.get(30, TimeUnit.SECONDS) }
                    check(receipts.distinct().size == 1)
                    receipts.first()
                } finally {
                    pool.shutdownNow()
                    check(pool.awaitTermination(5, TimeUnit.SECONDS))
                }
                val added = human.units.getCivUnits().filter { it.id !in beforeIds }.toList()
                check(added.size == 1 && added.single().name == name && added.single().civ === human)
                check(added.single().currentTile.getUnits().any { it === added.single() })
                check(human.greatPeople.freeGreatPeople == 0)
                check(Triple(human.policies.freePolicies, human.tech.freeTechs, human.greatPeople.mayaLimitedFreeGP) == counters)
                check(PlayerHost(runtime).bindPlayer(humanName).execute(request) == receipt)
                reject(PlayerSessionFailure.REQUEST_ID_CONFLICT) { session.execute(request.copy(selection = "Conflicting option")) }
                check(session.turn().greatPersonOptions.isEmpty())
            }
        }
    }

    private fun maya(save: Path, data: Path) {
        val checkpoint = data.resolve("checkpoint.json")
        lateinit var first: TurnCommandRequest
        lateinit var receipt: uncivai.player.TurnCommandReceipt
        HeadlessRuntime.load(save, data.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            val human = runtime.game.getCivilization("The Maya")
            val manager = human.greatPeople
            val session = PlayerHost(runtime).bindPlayer("The Maya")
            check(session.turn().greatPersonOptions == listOf("Great Engineer", "Great Scientist"))
            val stale = choice(session, "Great Engineer")
            val before = raw(runtime.game)
            reject(PlayerSessionFailure.OPTION_UNAVAILABLE) { session.execute(choice(session, "Worker")) }
            check(raw(runtime.game) == before)
            first = choice(session, "Great Scientist")
            receipt = session.execute(first)
            check(manager.freeGreatPeople == 1 && manager.mayaLimitedFreeGP == 0)
            check("Great Scientist" !in manager.longCountGPPool && "Great Engineer" in manager.longCountGPPool)
            reject(PlayerSessionFailure.STALE_REVISION) { session.execute(stale) }
            session.execute(choice(session, "Great Engineer"))
            check(manager.freeGreatPeople == 0 && "Great Engineer" in manager.longCountGPPool)
            val greek = runtime.game.getCivilization("Greece")
            runtime.game.currentPlayer = greek.civID
            runtime.game.currentPlayerCiv = greek
            val after = raw(runtime.game)
            check(session.execute(first) == receipt && raw(runtime.game) == after)
            runtime.executor.commitCheckpoint()
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, data.resolve("restored")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("The Maya")
            val before = raw(runtime.game)
            check(session.execute(first) == receipt && raw(runtime.game) == before)
        }
    }

    private fun blocked(save: Path, data: Path) = HeadlessRuntime.load(save, data).use { runtime ->
        val human = runtime.game.getCivilization("The Maya")
        val session = PlayerHost(runtime).bindPlayer("The Maya")
        check(session.turn().greatPersonOptions == listOf("Great Scientist"))
        fun gameplayState() = kotlinx.serialization.json.Json.parseToJsonElement(raw(runtime.game)).jsonObject - "lastUnitId"
        val before = gameplayState()
        reject(PlayerSessionFailure.PLACEMENT_FAILED) { session.execute(choice(session, "Great Scientist")) }
        // Failed native placement can consume a unit ID, but no gameplay state.
        check(gameplayState() == before)
        check(human.greatPeople.freeGreatPeople == 1 && human.greatPeople.mayaLimitedFreeGP == 1)
        human.cities.single().getCenterTile().civilianUnit!!.destroy()
        session.execute(choice(session, "Great Scientist"))
        check(human.greatPeople.freeGreatPeople == 0 && human.greatPeople.mayaLimitedFreeGP == 0)
        check("Great Scientist" !in human.greatPeople.longCountGPPool)
    }

    private fun choice(session: PlayerSession, name: String): TurnCommandRequest {
        val turn = session.turn()
        return TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, TurnCommandKind.CHOOSE_FREE_GREAT_PERSON, selection = name)
    }

    private fun reject(reason: PlayerSessionFailure, operation: () -> Any?) {
        val failure = runCatching(operation).exceptionOrNull()
        check(failure is PlayerSessionException && failure.reason == reason) { "Expected $reason, got $failure" }
        check(failure.cause == null)
    }

    private fun raw(game: com.unciv.logic.GameInfo) = json().toJson(game)

    private fun prepareCoastalSave(
        source: Path,
        target: Path,
        data: Path,
        humanName: String,
    ): Path {
        HeadlessRuntime.load(source, data).use { runtime ->
            val civilization = runtime.game.getCivilization(humanName)
            val center = checkNotNull(civilization.getCapital()).getCenterTile()
            val coast = center.neighbors.first { tile ->
                !tile.isCityCenter() && tile.getUnits().none()
            }
            coast.setTileResource(null)
            coast.improvement = null
            coast.setTerrainFeatures(emptyList())
            coast.baseTerrain = Constants.coast
            coast.setTerrainTransients()
            Files.createDirectories(target.parent)
            Files.writeString(target, raw(runtime.game))
        }
        return target
    }
}
