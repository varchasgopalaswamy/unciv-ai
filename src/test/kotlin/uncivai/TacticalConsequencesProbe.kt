package uncivai

import com.unciv.json.json
import com.unciv.logic.map.HexCoord
import uncivai.player.MovementPreviewRequest
import uncivai.player.PlayerHost
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path

object TacticalConsequencesProbe {
    @JvmStatic fun main(args: Array<String>) {
        try {
            when (args[1]) {
                "combat" -> combat(Path.of(args[0]))
                "lethal" -> lethal(Path.of(args[0]))
                "movement" -> movement(Path.of(args[0]))
                "privacy" -> privacy(Path.of(args[0]))
                "civilian" -> CivilianMovementProbe.run(Path.of(args[0]))
                else -> error("Unknown case")
            }
        } catch (failure: Throwable) {
            failure.printStackTrace()
            kotlin.system.exitProcess(1)
        }
    }

    private fun combat(root: Path) = SettlementPreviewProbe.fixture(root).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        player.units.getCivUnits().toList().forEach { it.destroy() }
        val scout = player.units.placeUnitNearTile(HexCoord(1, 0), "Scout")!!
        scout.health = 24
        val enemy = game.getCivilization("Greece")
        enemy.units.placeUnitNearTile(HexCoord(2, 0), "Warrior")!!
        player.diplomacyFunctions.makeCivilizationsMeet(enemy)
        player.getDiplomacyManager(enemy)!!.declareWar()
        player.popupAlerts.clear()
        player.viewableTiles = game.tileMap.values.toSet()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val before = json().toJson(game)
        val turn = session.turn()
        val unit = turn.map.units.single()
        val attack = unit.combat!!.attacks.first { it.attackFrom == unit.position }
        val consequences = attack.consequences!!
        check(consequences.attacker.current == 24)
        check(consequences.attacker.remainingMin == 0 && consequences.attacker.destructionPossible)
        check(consequences.defender.remainingMin > 0 && !consequences.defender.destructionPossible)
        check(consequences.attackFrom.ownMilitary.isEmpty()) { "The attacker was counted as its own cover" }
        check(consequences.attackFrom.visibleHostiles.single().position == attack.destination)
        check(consequences.mayAdvanceTo!!.position == attack.destination)
        check(before == json().toJson(game)) { "A consequence query changed native state" }
        // Risk remains advisory: a native-legal attack can still be ordered.
        val result = session.execute(TurnCommandRequest("risk", turn.epoch, turn.revision, TurnCommandKind.ATTACK, unit.handle, destination = attack.destination, attackFrom = attack.attackFrom))
        check(result.combat!!.attacked)
        // Ranged fire can reduce a city to one HP; a melee unit can capture it.
        enemy.cities.single().health = 5
        player.units.placeUnitNearTile(HexCoord(-5, 0), "Archer")!!
        player.units.placeUnitNearTile(HexCoord(-6, 0), "Warrior")!!
        player.viewableTiles = game.tileMap.values.toSet()
        val cityOffers = session.turn().map.units.filter { it.name in setOf("Archer", "Warrior") }
        for (owned in cityOffers) {
            val offer = owned.combat!!.attacks.first { it.defenderIsCity }.consequences!!
            check(offer.defender.remainingMin == 1 && !offer.defender.destructionPossible)
            check(offer.capturePossible == (owned.name == "Warrior"))
        }
    }

    private fun movement(root: Path) = SettlementPreviewProbe.fixture(root).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val settler = player.units.getCivUnits().single()
        settler.removeFromTile()
        settler.putInTile(game.tileMap[0, 3])
        settler.currentMovement = 2f
        player.units.placeUnitNearTile(HexCoord(0, 4), "Warrior")!!
        val archer = player.units.placeUnitNearTile(HexCoord(1, 0), "Archer")!!
        game.tileMap[2, 0].setTerrainFeatures(listOf("Hill"))
        game.tileMap[2, 0].setTerrainTransients()
        val enemy = game.getCivilization("Greece")
        enemy.units.placeUnitNearTile(HexCoord(3, 0), "Warrior")!!
        enemy.units.placeUnitNearTile(HexCoord(2, 5), "Warrior")!!
        player.diplomacyFunctions.makeCivilizationsMeet(enemy)
        player.getDiplomacyManager(enemy)!!.declareWar()
        player.popupAlerts.clear()
        player.viewableTiles = game.tileMap.values.toSet()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val beforeDetails = json().toJson(game)
        var turn = session.turn()
        check(beforeDetails == json().toJson(game)) { "Prospective founding checks changed the game" }
        val handle = turn.map.units.single { it.name == "Archer" }.handle
        val move = turn.map.units.single { it.handle == handle }.moves.single { it.destination == TilePosition(2, 0) }
        check(move.afterMove!!.movementRemaining == 0f && move.afterMove.attack == "NO_MOVEMENT")
        check(move.afterMove.context.visibleHostiles.any { it.position == TilePosition(3, 0) })
        val nativeBefore = json().toJson(game)
        val preview = session.movementPreview(MovementPreviewRequest(handle, move.destination))
        check(preview.turns.first().afterMove == move.afterMove)
        check(nativeBefore == json().toJson(game))
        session.execute(TurnCommandRequest("move", turn.epoch, turn.revision, TurnCommandKind.MOVE_UNIT, handle, destination = move.destination))
        check(!archer.hasMovement() && session.turn().map.units.single { it.handle == handle }.combat!!.attacks.isEmpty())
        // The same native path can leave enough movement to found on a known site.
        player.viewableTiles = game.tileMap.values.toSet()
        turn = session.turn()
        val civilian = turn.map.units.single { it.name == "Settler" }
        val site = civilian.moves.single { it.destination == TilePosition(0, 4) }
        check(site.afterMove!!.movementRemaining == 1f && site.afterMove.foundCity == "AVAILABLE")
        check(site.afterMove.context.ownMilitary.any { it.distance == 0 && it.name == "Warrior" })
        check(site.afterMove.context.visibleHostiles.any { it.position == TilePosition(2, 5) })
        val future = session.movementPreview(MovementPreviewRequest(civilian.handle, TilePosition(0, 6)))
        check(future.turns.size > 1)
        check(future.turns.last().afterMove!!.foundCity == "RECHECK_ON_ARRIVAL")
        session.execute(TurnCommandRequest("settler-move", turn.epoch, turn.revision, TurnCommandKind.MOVE_UNIT, civilian.handle, destination = site.destination))
        check(session.turn().map.units.single { it.handle == civilian.handle }.canFoundCity)
    }

    private fun lethal(root: Path) {
        for (barbarian in listOf(true, false)) {
            SettlementPreviewProbe.fixture(root.resolve(if (barbarian) "brute" else "tied")).use { runtime ->
                val game = runtime.game
                val player = game.getCivilization("Rome")
                player.units.getCivUnits().toList().forEach { it.destroy() }
                val warrior = player.units.placeUnitNearTile(HexCoord(4, 0), "Warrior")!!
                val enemy = if (barbarian) game.civilizations.single { it.isBarbarian } else game.getCivilization("Greece")
                val defender = enemy.units.placeUnitNearTile(HexCoord(5, 0), if (barbarian) "Brute" else "Warrior")!!
                warrior.health = if (barbarian) 1 else 10
                defender.health = if (barbarian) 12 else 10
                if (!barbarian) {
                    player.diplomacyFunctions.makeCivilizationsMeet(enemy)
                    player.getDiplomacyManager(enemy)!!.declareWar()
                }
                player.popupAlerts.clear()
                player.viewableTiles = game.tileMap.values.toSet()
                val session = PlayerHost(runtime).bindPlayer("Rome")
                val nativeBefore = json().toJson(game)
                val turn = session.turn()
                val unit = turn.map.units.single()
                val attack = unit.combat!!.attacks.single { it.attackFrom == unit.position }
                val consequences = attack.consequences!!
                check(nativeBefore == json().toJson(game)) { "Preview changed native state" }
                check(consequences.attacker.remainingMax > 0 || consequences.defender.remainingMax > 0) {
                    "Preview falsely promises mutual destruction: $consequences"
                }
                if (!barbarian) {
                    check(consequences.attacker.remainingMin == 0 && consequences.attacker.remainingMax == 1)
                    check(consequences.defender.remainingMin == 0 && consequences.defender.remainingMax == 1)
                }

                val command = TurnCommandRequest("lethal", turn.epoch, turn.revision, TurnCommandKind.ATTACK, unit.handle, destination = attack.destination, attackFrom = attack.attackFrom)
                val receipt = session.execute(command)
                val outcome = receipt.combat!!
                check(outcome.attacked && outcome.resolution == "Completed")
                check(warrior.isDestroyed xor defender.isDestroyed)
                check(warrior.health + defender.health == 1)
                check(outcome.damageToAttacker in attack.minDamageToAttacker..attack.maxDamageToAttacker)
                check(outcome.damageToDefender in attack.minDamageToDefender..attack.maxDamageToDefender)
                check(warrior.health in consequences.attacker.remainingMin..consequences.attacker.remainingMax)
                check(defender.health in consequences.defender.remainingMin..consequences.defender.remainingMax)
                check(outcome.attackerDestroyed == warrior.isDestroyed && outcome.defenderDestroyed == defender.isDestroyed)
                // Death may remove vision; a completed receipt still reports the confirmed outcome.
                if (warrior.isDestroyed) {
                    check(!defender.isVisibleTo(player))
                    check(outcome.defenderHealth == null && !outcome.defenderDestroyed)
                }
                val nativeAfter = json().toJson(game)
                check(session.execute(command) == receipt)
                check(json().toJson(game) == nativeAfter) { "Receipt replay repeated the attack" }
            }
        }
    }

    private fun privacy(root: Path) = SettlementPreviewProbe.fixture(root).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val hidden = game.tileMap[2, 5]
        val visible = game.tileMap.values.filter { it !== hidden }.toSet()
        player.viewableTiles = visible
        hidden.setExplored(player, false)
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val handle = session.turn().map.units.single().handle
        val request = MovementPreviewRequest(handle, TilePosition(0, 4))
        val before = session.movementPreview(request).turns.map { it.afterMove }
        val enemy = game.getCivilization("Greece")
        player.diplomacyFunctions.makeCivilizationsMeet(enemy)
        player.getDiplomacyManager(enemy)!!.declareWar()
        player.popupAlerts.clear()
        enemy.units.placeUnitNearTile(hidden.position, "Warrior")!!
        player.viewableTiles = visible
        hidden.setExplored(player, false)
        val after = session.movementPreview(request).turns.map { it.afterMove }
        check(after == before) { "A hidden enemy changed endpoint advice" }
        check(after.last()!!.context.unseenTiles > 0)
        player.viewableTiles = game.tileMap.values.toSet()
        check(session.movementPreview(request).turns.last().afterMove!!.context.visibleHostiles.any { it.position == TilePosition(2, 5) })
    }
}
