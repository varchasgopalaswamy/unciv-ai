package uncivai

import com.badlogic.gdx.files.FileHandle
import com.unciv.logic.GameInfo
import com.unciv.logic.GameStarter
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.files.MapSaver
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.MapSize
import com.unciv.logic.map.TileMap
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.models.metadata.GameSetupInfo
import com.unciv.models.ruleset.RulesetCache
import java.nio.file.Files

/** A fixed operator-created battlefield; every subsequent action uses ordinary game rules. */
internal object CombatScenarioSetup {
    fun create(setup: GameSetupInfo): GameInfo {
        val ruleset = RulesetCache.getComplexRuleset(setup.gameParameters)
        val map = TileMap(6, ruleset).apply {
            mapParameters = setup.mapParameters.apply {
                name = "Combat exercise"
                mapSize = MapSize(6)
            }
            addStartingLocation("Rome", this[HexCoord(-4, 0)])
            addStartingLocation("Greece", this[HexCoord(4, 0)])
        }
        val temporary = Files.createTempFile("unciv-combat-scenario-", ".json")
        val game = try {
            Files.writeString(temporary, MapSaver.mapToSavedString(map))
            setup.mapFile = FileHandle(temporary.toFile())
            GameStarter.startNewGame(setup)
        } finally {
            Files.deleteIfExists(temporary)
        }
        val rome = game.getCivilization("Rome")
        val greece = game.getCivilization("Greece")
        val players = listOf(rome, greece)
        // Starter placement is intentionally replaced before the scenario has any player viewers.
        players.forEach { player -> player.units.getCivUnits().toList().forEach { it.destroy() } }
        for (tile in game.tileMap.values) players.forEach { tile.setExplored(it, false) }
        foundCity(game, rome, "Rome", HexCoord(-4, 0))
        foundCity(game, greece, "Athens", HexCoord(4, 0))
        foundCity(game, rome, "Antium", HexCoord(0, -3), health = 45)
        foundCity(game, greece, "Sparta", HexCoord(0, 3), health = 45)
        rome.diplomacyFunctions.makeCivilizationsMeet(greece)
        rome.getDiplomacyManager(greece)!!.declareWar()

        place(game, rome, "Warrior", HexCoord(0, 2)).promotions.XP = 10
        place(game, rome, "Warrior", HexCoord(0, 0))
        place(game, rome, "Archer", HexCoord(-1, 0))
        place(game, rome, "Warrior", HexCoord(-4, -1)).health = 65
        place(game, greece, "Warrior", HexCoord(0, -2)).promotions.XP = 10
        place(game, greece, "Warrior", HexCoord(1, 0))
        place(game, greece, "Archer", HexCoord(1, 1))
        place(game, greece, "Warrior", HexCoord(4, 1)).health = 65

        players.forEach { player ->
            player.addGold(500)
            for (technology in listOf("Pottery", "Archery", "Mining")) player.tech.addTechnology(technology, false)
            player.tech.techsToResearch = arrayListOf("Writing", "The Wheel", "Bronze Working")
            for (city in player.cities) {
                city.cityConstructions.constructionQueue.clear()
                for (construction in listOf("Archer", "Warrior", "Monument")) city.cityConstructions.addToQueue(construction)
                city.cityStats.update()
            }
            player.units.getCivUnits().forEach { it.updateVisibleTiles(updateCivViewableTiles = false) }
            player.cache.updateViewableTiles()
        }
        return game
    }

    private fun foundCity(game: GameInfo, player: Civilization, name: String, position: HexCoord, health: Int? = null) {
        val settler = place(game, player, "Settler", position)
        val city = checkNotNull(UnitSettlement.tryFoundCity(settler)) { "Cannot found scenario city $name at $position" }
        city.name = name
        if (health != null) city.health = health
    }

    private fun place(game: GameInfo, player: Civilization, name: String, position: HexCoord): MapUnit {
        val unit = checkNotNull(game.tileMap.placeUnitNearTile(position, name, player))
        check(unit.currentTile.position == position) { "Cannot place scenario $name at $position" }
        unit.currentMovement = unit.getMaxMovement().toFloat()
        return unit
    }
}
