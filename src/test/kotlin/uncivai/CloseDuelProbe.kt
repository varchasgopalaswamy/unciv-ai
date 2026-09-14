package uncivai

import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path
import java.util.UUID

object CloseDuelProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        for (seed in listOf(1L, 42L, 412L, -7L, Long.MAX_VALUE)) {
            val checkpoint = root.resolve("$seed/checkpoint.json")
            HeadlessRuntime.createDuel(root.resolve("$seed/initial"), seed, closeStarts = true).use { runtime ->
                val players = listOf("Rome", "Greece").map(runtime.game::getCivilization)
                val settlers = players.map { player -> player.units.getCivUnits().single { it.name == "Settler" } }
                val scouts = players.map { player -> player.units.getCivUnits().single { it.name == "Scout" } }
                check(settlers[0].currentTile.aerialDistanceTo(settlers[1].currentTile) == 5)
                check(scouts[0].currentTile.aerialDistanceTo(scouts[1].currentTile) == 1)
                for (player in players) {
                    check(player.isHuman() && player.cities.isEmpty())
                    check(player.units.getCivUnits().map { it.name }.sorted().toList() == listOf("Scout", "Settler", "Warrior"))
                }
                runtime.executor.enableCheckpoints(checkpoint)
                val host = PlayerHost(runtime)
                for (player in players) {
                    val session = host.bindPlayer(player.civID)
                    val initial = session.turn()
                    check(initial.turn == 0)
                    check(initial.scenario == null)
                    check(initial.diplomacy.knownCivilizations.size == 1)
                    check(initial.alerts.any { it.kind == "FirstContact" && it.content != null })
                    check(initial.map.tiles.size < runtime.game.tileMap.values.size) { "Setup revealed the whole map" }
                    settleAndEnd(session)
                    check(player.cities.size == 1)
                }
                check(players[0].cities.single().getCenterTile().aerialDistanceTo(players[1].cities.single().getCenterTile()) == 5)
                check(runtime.game.turns == 1 && runtime.game.currentPlayer == "Rome")
            }
            HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("$seed/restored")).use { runtime ->
                for (name in listOf("Rome", "Greece")) {
                    val player = runtime.game.getCivilization(name)
                    check(player.cities.size == 1 && player.units.getCivUnits().count { it.name == "Scout" } == 1)
                    check(player.popupAlerts.none { it.type.name == "FirstContact" })
                    check(PlayerHost(runtime).bindPlayer(name).turn().scenario == null)
                }
            }
        }
    }

    private fun settleAndEnd(session: PlayerSession) {
        repeat(20) {
            val turn = session.turn()
            val alert = turn.alerts.firstOrNull { it.canAcknowledge }
            val settler = turn.map.units.firstOrNull { it.canFoundCity }
            val city = turn.cities.firstOrNull { it.constructionQueue.isEmpty() }
            fun command(kind: TurnCommandKind, target: String? = null, selection: String? = null, queue: List<String> = emptyList()) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection, queue).reviewedFor(turn)
            val request = when {
                alert != null -> command(TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle)
                settler != null -> command(TurnCommandKind.FOUND_CITY, settler.handle)
                turn.researchQueue.isEmpty() -> command(TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf(turn.researchOptions.first()))
                city != null -> command(TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, city.constructionOptions.first())
                turn.canEndTurn -> command(TurnCommandKind.END_TURN)
                else -> error("Unexpected starting blocker: ${turn.pending}")
            }
            session.execute(request)
            if (request.kind == TurnCommandKind.END_TURN) return
        }
        error("Starting turn did not finish")
    }
}
