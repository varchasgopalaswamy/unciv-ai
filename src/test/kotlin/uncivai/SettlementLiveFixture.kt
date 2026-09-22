package uncivai

import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.files.UncivFiles
import com.unciv.logic.map.HexCoord
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import java.nio.file.Files
import java.nio.file.Path

/** A standing uncertain destination, a scout, and an available settler; no prescribed replacement site. */
object SettlementLiveFixture {
    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args.single())
        require(root.isAbsolute && !Files.exists(root))
        SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
            val game = runtime.game
            val rome = game.getCivilization("Rome")
            for (name in listOf("Greece", "France")) {
                game.getCivilization(name).playerType = PlayerType.AI
                game.gameParameters.players.single { it.chosenCiv == name }.playerType = PlayerType.AI
            }
            val settler = rome.units.getCivUnits().single()
            settler.removeFromTile()
            settler.putInTile(game.tileMap[0, 2])
            settler.action = "moveTo 4,0"
            rome.units.placeUnitNearTile(HexCoord(4, 2), "Scout")!!.also {
                check(it.currentTile.position == HexCoord(4, 2))
            }
            game.getCivilization("France").addCity(HexCoord(6, 0)).cityConstructions.constructionQueue = arrayListOf("Monument")
            for (civ in game.civilizations) {
                civ.cache.updateViewableTiles()
                civ.popupAlerts.clear()
                civ.notifications.clear()
            }
            check(!game.tileMap[6, 0].isVisible(rome))
            game.tileMap.mapParameters.name = "Settlement destination acceptance"
            val turn = PlayerHost(runtime).bindPlayer("Rome").turn()
            check(turn.map.units.single { it.name == "Settler" }.movementOrder!!.settlement!!.siteStatus == "UNKNOWN")
            runtime.executor.enableCheckpoints(root.resolve("exploration.json"))
            Files.writeString(root.resolve("initial-game.json"), UncivFiles.gameInfoToString(game, forceZip = false))
            Files.writeString(root.resolve("initial-turn.json"), PlayerTurnJson.json.encodeToString(turn))
        }
        HeadlessRuntime.loadCheckpoint(root.resolve("exploration.json"), root.resolve("preflight")).use { runtime ->
            val turn = PlayerHost(runtime).bindPlayer("Rome").turn()
            check(turn.map.units.single { it.name == "Settler" }.movementOrder!!.settlement!!.siteStatus == "UNKNOWN")
        }
        println("SETTLEMENT_FIXTURE_READY")
    }
}
