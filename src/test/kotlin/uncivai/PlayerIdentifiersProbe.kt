package uncivai

import com.unciv.json.json
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.civilization.PopupAlert
import kotlinx.serialization.Serializable
import uncivai.player.FreeGreatPersonSnapshot
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path

@Serializable
private data class IdentifierRecovery(
    val snapshot: PlayerTurnSnapshot,
    val decision: FreeGreatPersonSnapshot,
    val request: TurnCommandRequest,
    val receipt: TurnCommandReceipt,
)

object PlayerIdentifiersProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        if (args[1] == "prepare") prepare(root) else restore(root)
    }

    private fun prepare(root: Path) {
        HeadlessRuntime.createDuel(root.resolve("initial"), 787L).use { runtime ->
            val game = runtime.game
            for (name in listOf("Rome", "Greece")) {
                val civilization = game.getCivilization(name)
                game.currentPlayer = name
                game.currentPlayerCiv = civilization
                check(PlayerUnitOperations(civilization).tryFoundCity(civilization.units.getCivUnits().single { it.name == "Settler" }))
                civilization.tech.addTechnology("Pottery", false)
                civilization.popupAlerts.clear()
                civilization.popupAlerts.add(PopupAlert(AlertType.TechResearched, "Pottery"))
            }
            val roman = game.getCivilization("Rome")
            val greek = game.getCivilization("Greece")
            game.currentPlayer = roman.civID
            game.currentPlayerCiv = roman
            roman.greatPeople.freeGreatPeople = 1
            runtime.executor.enableCheckpoints(root.resolve("checkpoint.json"))
            val host = PlayerHost(runtime)
            val rome = host.bindPlayer("Rome")
            val greece = host.bindPlayer("Greece")
            val initial = rome.turn()
            check(initial.epoch.matches(Regex("g[0-9a-z]{1,13}")))
            check(initial.revision == "rev1" && initial.turnReview!!.id == "rev1")
            check(initial.cities.single().handle == "city1")
            check(initial.map.units.single().handle == "unit1")
            check(initial.alerts.single().handle == "alert1")
            check(greece.turn().cities.single().handle == "city1")
            check(greece.turn().map.units.single().handle == "unit1")
            check(greece.turn().alerts.single().handle == "alert1")
            check(rome.cities().cities.single().handle == "city1")
            check(host.bindPlayer("Rome").cities() == rome.cities())
            check(rome.freeGreatPersonChoice().decision!!.handle == "decision1")
            check(host.bindPlayer("Rome").freeGreatPersonChoice() == rome.freeGreatPersonChoice())

            // Unseen rival observations and objects do not advance our counters or revision.
            check(!roman.knows(greek))
            greek.popupAlerts.add(PopupAlert(AlertType.WonderBuilt, "Stonehenge"))
            check(greece.turn().alerts.last().handle == "alert2")
            check(rome.turn() == initial)

            // Reproduce the failed gameplay action with a short reference and a native save check.
            check("Granary" in initial.cities.single().constructionOptions)
            val request = command(initial, "t0a1", TurnCommandKind.QUEUE_CONSTRUCTION, "city1", "Granary")
            val receipt = rome.execute(request)
            check(roman.cities.single().cityConstructions.constructionQueue == listOf("Granary"))
            check(greek.cities.single().cityConstructions.constructionQueue.isEmpty())
            check(rome.execute(request) == receipt)
            reject(runtime, PlayerSessionFailure.CITY_UNAVAILABLE) {
                rome.execute(command(rome.turn(), "raw-city", TurnCommandKind.RENAME_CITY, greek.cities.single().id, "Wrong city"))
            }

            replaceAlert(runtime, rome, "alert1", "alert2")
            roman.units.getCivUnits().single().destroy()
            rome.turn()
            checkNotNull(game.tileMap.placeUnitNearTile(roman.cities.single().location, "Scout", roman))
            check(rome.turn().map.units.single().handle == "unit2")
            reject(runtime, PlayerSessionFailure.UNIT_UNAVAILABLE) {
                rome.execute(command(rome.turn(), "retired-unit", TurnCommandKind.SET_UNIT_ORDER, "unit1", "SLEEP"))
            }
            val expected = IdentifierRecovery(rome.turn(), rome.freeGreatPersonChoice(), request, receipt)
            Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(expected))
        }
    }

    private fun restore(root: Path) {
        val expected = PlayerTurnJson.json.decodeFromString<IdentifierRecovery>(Files.readString(root.resolve("expected.json")))
        HeadlessRuntime.loadCheckpoint(root.resolve("checkpoint.json"), root.resolve("restored")).use { runtime ->
            val rome = PlayerHost(runtime).bindPlayer("Rome")
            check(rome.turn() == expected.snapshot) { "Restart changed a player reference" }
            check(rome.freeGreatPersonChoice() == expected.decision) { "Restart replaced a pending decision handle" }
            check(rome.cities().cities.single().handle == "city1")
            val before = json().toJson(runtime.game)
            check(rome.execute(expected.request) == expected.receipt)
            check(json().toJson(runtime.game) == before) { "Restart repeated production" }
            replaceAlert(runtime, rome, "alert2", "alert3")
            val roman = runtime.game.getCivilization("Rome")
            roman.units.getCivUnits().single().destroy()
            rome.turn()
            checkNotNull(runtime.game.tileMap.placeUnitNearTile(roman.cities.single().location, "Worker", roman))
            check(rome.turn().map.units.single().handle == "unit3")
            val greekName = runtime.game.getCivilization("Greece").cities.single().name
            rome.execute(command(rome.turn(), "rename", TurnCommandKind.RENAME_CITY, "city1", "Compact Roma"))
            check(roman.cities.single().name == "Compact Roma")
            check(runtime.game.getCivilization("Greece").cities.single().name == greekName) { "An overlapping alias crossed player ownership" }
        }
        HeadlessRuntime.createDuel(root.resolve("new-game"), 787L).use { runtime ->
            reject(runtime, PlayerSessionFailure.STALE_EPOCH) {
                PlayerHost(runtime).bindPlayer("Rome").execute(expected.request)
            }
        }
    }

    private fun replaceAlert(runtime: HeadlessRuntime, player: PlayerSession, old: String, next: String) {
        val before = player.turn()
        check(before.alerts.single().handle == old)
        player.execute(command(before, "ack-$old", TurnCommandKind.ACKNOWLEDGE_ALERT, old))
        runtime.game.getCivilization("Rome").popupAlerts.add(PopupAlert(AlertType.TechResearched, "Pottery"))
        val after = player.turn()
        check(after.alerts.single().handle == next)
        reject(runtime, PlayerSessionFailure.OPTION_UNAVAILABLE) {
            player.execute(command(after, "stale-$old", TurnCommandKind.ACKNOWLEDGE_ALERT, old))
        }
    }

    private fun command(turn: PlayerTurnSnapshot, id: String, kind: TurnCommandKind, target: String, selection: String? = null) = TurnCommandRequest(id, turn.epoch, turn.revision, kind, target, selection)

    private fun reject(runtime: HeadlessRuntime, reason: PlayerSessionFailure, block: () -> Any) {
        val before = json().toJson(runtime.game)
        val failure = runCatching(block).exceptionOrNull()
        check(failure is PlayerSessionException && failure.reason == reason) { "Expected $reason, got $failure" }
        check(json().toJson(runtime.game) == before) { "A rejected reference changed the game" }
    }
}
