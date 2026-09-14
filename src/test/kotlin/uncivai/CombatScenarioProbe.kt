package uncivai

import com.unciv.logic.map.HexCoord
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path
import java.util.UUID

object CombatScenarioProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val checkpoint = root.resolve("checkpoint.json")
        HeadlessRuntime.createDuel(root.resolve("initial"), 412L, combatScenario = true).use { runtime ->
            verifySetup(runtime)
            runtime.executor.enableCheckpoints(checkpoint)
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            verifySetup(runtime)
            val host = PlayerHost(runtime)
            val rome = host.bindPlayer("Rome")
            var turn = readAlerts(rome)
            verifyOffers(turn)
            val veteran = turn.map.units.single { it.combat!!.promotions!!.choices.any { promotion -> promotion.available } }
            val promotion = veteran.combat!!.promotions!!.choices.first { it.available }
            rome.execute(command(turn, TurnCommandKind.PROMOTE_UNIT, veteran.handle, promotion.name))

            turn = rome.turn()
            val reserve = turn.map.units.single { it.health == 65 }
            rome.execute(command(turn, TurnCommandKind.SET_UNIT_ORDER, reserve.handle, "FORTIFY_UNTIL_HEALED"))
            turn = rome.turn()
            val archer = turn.map.units.single { it.name == "Archer" }
            val ranged = archer.combat!!.attacks.first { !it.defenderIsCity }
            val receipt = rome.execute(
                command(turn, TurnCommandKind.ATTACK, archer.handle)
                    .copy(destination = ranged.destination, attackFrom = ranged.attackFrom),
            )
            check(receipt.combat!!.attacked && receipt.combat.damageToDefender > 0)

            turn = rome.turn()
            val city = turn.cities.single { it.name == "Antium" }
            val bombard = city.attacks.first()
            val bombardReceipt = rome.execute(
                command(turn, TurnCommandKind.CITY_ATTACK, city.handle).copy(destination = bombard.destination),
            )
            check(bombardReceipt.combat!!.attacked && bombardReceipt.combat.damageToDefender > 0)
            turn = rome.turn()
            check(turn.canEndTurn) { "Unexpected scenario blocker: ${turn.pending}" }
            rome.execute(command(turn, TurnCommandKind.END_TURN))
            val greece = host.bindPlayer("Greece")
            val greekTurn = readAlerts(greece)
            verifyOffers(greekTurn)
            check(greekTurn.combatReports.isNotEmpty())
            check(runtime.game.victoryData == null && !runtime.game.getCivilization("Greece").isDefeated())
        }
    }

    private fun verifySetup(runtime: HeadlessRuntime) {
        val game = runtime.game
        check(game.tileMap.mapParameters.name == "Combat exercise")
        check(game.tileMap.values.size == 127 && game.tileMap.values.all { it.isLand && !it.isImpassible() })
        check(game.gameParameters.mods.isEmpty() && game.gameParameters.noBarbarians)
        check(game.gameParameters.speed == "Quick" && game.currentPlayer == "Rome")
        check(game.victoryData == null)
        val expected = mapOf("Rome" to HexCoord(-4, 0), "Athens" to HexCoord(4, 0), "Antium" to HexCoord(0, -3), "Sparta" to HexCoord(0, 3))
        for (player in game.civilizations.filter { it.isMajorCiv() }) {
            check(player.isHuman() && player.cities.size == 2 && !player.isDefeated())
            check(player.isAtWarWith(game.getCivilization(if (player.civName == "Rome") "Greece" else "Rome")))
            for (city in player.cities) {
                check(city.location == expected.getValue(city.name))
                check(city.cityConstructions.constructionQueue == listOf("Archer", "Warrior", "Monument"))
                check(if (city.isOriginalCapital) city.health > 45 else city.health == 45)
            }
            check(player.cities.count { it.isOriginalCapital } == 1)
            val units = player.units.getCivUnits().toList()
            check(units.map { it.name }.sorted() == listOf("Archer", "Warrior", "Warrior", "Warrior"))
            check(units.count { it.health == 65 } == 1 && units.count { it.promotions.XP == 10 } == 1)
            check(player.tech.techsToResearch == listOf("Writing", "The Wheel", "Bronze Working"))
            val snapshot = PlayerHost(runtime).bindPlayer(player.civID).turn()
            check(snapshot.scenario == null)
            check(snapshot.map.tiles.size < game.tileMap.values.size) { "Scenario bypassed ordinary fog of war" }
        }
    }

    private fun verifyOffers(turn: PlayerTurnSnapshot) {
        check(turn.phase == "ACTIVE")
        check(turn.map.units.filter { it.name == "Warrior" }.any { it.combat!!.attacks.any { attack -> !attack.defenderIsCity } })
        check(turn.map.units.any { it.combat!!.attacks.any { attack -> attack.defenderIsCity } })
        check(turn.map.units.single { it.name == "Archer" }.combat!!.attacks.isNotEmpty())
        check(turn.cities.any { it.attacks.isNotEmpty() })
        check(turn.map.units.any { it.combat!!.promotions!!.choices.any { promotion -> promotion.available } })
        check(turn.map.units.single { it.health == 65 }.combat!!.orders.any { it.name == "FORTIFY_UNTIL_HEALED" && it.available })
    }

    private fun readAlerts(session: PlayerSession): PlayerTurnSnapshot {
        repeat(10) {
            val turn = session.turn()
            val alert = turn.alerts.firstOrNull { it.canAcknowledge } ?: return turn
            check(alert.content!!.paragraphs.isNotEmpty())
            session.execute(command(turn, TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle))
        }
        error("Unexpected scenario alert count")
    }

    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection).reviewedFor(turn)
}
