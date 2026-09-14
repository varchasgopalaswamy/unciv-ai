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

/** A peaceful Vanilla exercise with every non-city-state Great Person ability reachable. */
internal object GreatPeopleScenarioSetup {
    const val MAP_NAME = "Great People exercise"

    fun create(setup: GameSetupInfo): GameInfo {
        val ruleset = RulesetCache.getComplexRuleset(setup.gameParameters)
        val map = TileMap(8, ruleset).apply {
            mapParameters = setup.mapParameters.apply {
                name = MAP_NAME
                mapSize = MapSize(8)
            }
            addStartingLocation("Rome", this[HexCoord(-5, 0)])
            addStartingLocation("Greece", this[HexCoord(5, 0)])
        }
        val temporary = Files.createTempFile("great-people-exercise-", ".json")
        val game = try {
            Files.writeString(temporary, MapSaver.mapToSavedString(map))
            setup.mapFile = FileHandle(temporary.toFile())
            GameStarter.startNewGame(setup)
        } finally {
            Files.deleteIfExists(temporary)
        }
        val players = listOf("Rome", "Greece").map(game::getCivilization)
        players.forEach { player -> player.units.getCivUnits().toList().forEach { it.destroy() } }
        for (tile in game.tileMap.values) players.forEach { tile.setExplored(it, false) }
        for ((index, player) in players.withIndex()) {
            val sign = if (index == 0) -1 else 1
            val center = HexCoord(sign * 5, 0)
            val city = checkNotNull(UnitSettlement.tryFoundCity(place(game, player, "Settler", center)))
            city.name = if (index == 0) "Rome" else "Athens"
            player.addGold(500 - player.gold)
            for (tech in listOf("Pottery", "Writing", "Mining", "Masonry", "Bronze Working", "Archery", "The Wheel", "Philosophy")) player.tech.addTechnology(tech, false)
            player.tech.techsToResearch = arrayListOf("Mathematics", "Currency")
            player.tech.scienceOfLast8Turns.fill(25)
            city.population.setPopulation(6)
            for (building in listOf("Library", "University", "Granary")) city.cityConstructions.addBuilding(building)
            city.manualSpecialists = true
            city.population.specialistAllocations["Scientist"] = 1
            for (tile in city.getTiles().filterNot { it.isCityCenter() }) tile.setImprovement("Farm", player)
            city.reassignPopulation(resetLocked = true)
            city.cityConstructions.constructionQueue = arrayListOf("National College", "Worker")
            check(city.cityConstructions.canBeHurried())
            player.greatPeople.greatPersonPointsCounter["Great Scientist"] = player.greatPeople.getPointsRequiredForGreatPerson("Great Scientist") - 1
            // One ordinary adoption completes Liberty and grants a free person to choose.
            for (policy in listOf("Liberty", "Collective Rule", "Citizenship", "Republic", "Representation")) {
                player.policies.freePolicies++
                player.policies.adopt(ruleset.policies.getValue(policy))
            }
            player.policies.freePolicies = 1
            player.goldenAges.turnsLeftForCurrentGoldenAge = 0
            // Remove the setup-only policy gifts; this exercise starts with the prepared people.
            player.units.getCivUnits().toList().forEach { it.destroy() }
            fun person(name: String, label: String, x: Int, y: Int) = place(game, player, name, HexCoord(center.x + sign * x, sign * y)).also { it.instanceName = label }
            person("Great Engineer", "Rush engineer", 0, 0)
            person("Great Scientist", "Research scientist", -1, 0)
            person("Great Engineer", "Manufactory engineer", 0, -1)
            person("Great Merchant", "Customs merchant", 1, 0)
            person("Great Artist", "Landmark artist", 0, 1)
            person("Great Artist", "Golden Age artist", -1, -1)
            person("Great General", "Citadel general", 1, 2)
            player.popupAlerts.clear()
            player.notifications.clear()
            city.cityStats.update()
            player.cache.updateCivResources()
            player.cache.updateViewableTiles()
            player.updateStatsForNextTurn()
            check(city.population.getNewSpecialists()["Scientist"] == 1)
        }
        return game
    }

    private fun place(game: GameInfo, player: Civilization, name: String, position: HexCoord): MapUnit {
        val unit = checkNotNull(game.tileMap.placeUnitNearTile(position, name, player))
        check(unit.currentTile.position == position) { "Cannot place scenario $name at $position" }
        unit.currentMovement = unit.getMaxMovement().toFloat()
        return unit
    }
}
