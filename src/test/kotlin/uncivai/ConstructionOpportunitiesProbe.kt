package uncivai

import com.unciv.json.json
import com.unciv.logic.battle.CityCombatant
import com.unciv.logic.city.CityFlags
import com.unciv.logic.civilization.PlayerOperations
import com.unciv.logic.map.HexCoord
import com.unciv.models.stats.Stat
import uncivai.player.PlayerHost
import uncivai.player.observeCityDevelopment
import uncivai.player.observeProductionConversions
import java.nio.file.Path
import kotlin.math.abs

object ConstructionOpportunitiesProbe {
    fun conversion(root: Path) = SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val city = player.cities.single()
        val operations = PlayerOperations(player)
        check(observeProductionConversions(city, operations.queueableConstructions(city)).isEmpty())
        for (tech in listOf("Writing", "Philosophy", "Education", "Currency")) player.tech.addTechnology(tech, false)
        city.population.setPopulation(6)
        for (building in listOf("Library", "University", "National College", "Workshop", "Market")) city.cityConstructions.addBuilding(building)
        city.cityConstructions.constructionQueue = arrayListOf("Settler")
        city.cityConstructions.productionOverflow = 100
        player.goldenAges.enterGoldenAge()
        city.cityStats.update()
        val raw = json().toJson(game)
        val cityStats = city.cityStats
        val current = cityStats.currentCityStats
        val finalStats = cityStats.finalStatList
        val empire = player.stats.statsForNextTurn
        val owners = game.tileMap.values.associateWith { it.getCity() }
        val projected = observeProductionConversions(city, operations.queueableConstructions(city))
        check(owners.all { (tile, owner) -> tile.getCity() === owner }) { "Conversion preview reassigned live tile ownership" }
        check(projected.keys == setOf("Gold", "Science"))
        check(projected.getValue("Science").baseYieldPerProduction == 0.25f)
        check(projected.getValue("Science").yieldPerTurn.toFloat() > projected.getValue("Science").productionPerTurn.toFloat() / 4)
        check(json().toJson(game) == raw) { "Conversion preview mutated the save" }
        check(city.cityStats === cityStats && cityStats.currentCityStats === current && cityStats.finalStatList === finalStats)
        check(player.stats.statsForNextTurn === empire) { "Conversion preview replaced authoritative empire statistics" }
        check(projected == observeProductionConversions(city, operations.queueableConstructions(city)))
        for (name in listOf("Science", "Gold")) {
            // Compare the hypothetical calculation against an actual native queue change.
            city.cityConstructions.constructionQueue = arrayListOf(name)
            city.cityStats.update()
            val expected = city.cityStats.finalStatList.getValue("Construction")[Stat.valueOf(name)]
            val preview = projected.getValue(name)
            check(abs(preview.yieldPerTurn.toFloat() - expected) <= 0.051f) { "$name: $preview vs $expected" }
            check(abs(preview.productionPerTurn.toFloat() - city.cityStats.currentCityStats.production) <= 0.051f)
            check(observeCityDevelopment(city).constructions.single().productionPerTurn != null)
            // Stored overflow affects completion estimates, never production conversion.
            city.cityConstructions.productionOverflow = 0
            check(observeProductionConversions(city, operations.queueableConstructions(city)).getValue(name) == preview)
        }
        city.setFlag(CityFlags.Resistance, 2)
        city.cityStats.update()
        check(observeProductionConversions(city, operations.queueableConstructions(city)).values.all { it.yieldPerTurn == "0" })
        city.removeFlag(CityFlags.Resistance)
        city.cityConstructions.constructionQueue = arrayListOf("Science")
        city.cityStats.update()
        val workedTiles = city.workedTiles.toSet()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val turn = session.turn()
        repeat(3) { session.turn() }
        check(owners.all { (tile, owner) -> tile.getCity() === owner }) { "Turn observation reassigned live tile ownership" }
        check(city.workedTiles == workedTiles) { "Turn observation changed citizen assignments" }
        val health = city.health
        CityCombatant(city.getCenterTile().getCity()!!).takeDamage(10, null)
        check(city.health == health - 10) { "City combat damaged a detached preview instead of the real city" }
        check(turn.cities.single().productionConversions.getValue("Science") == projected.getValue("Science"))
    }

    fun wonders(root: Path) = SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val capital = player.cities.single()
        val second = player.addCity(HexCoord(0, 5))
        second.cityConstructions.constructionQueue = arrayListOf("Monument")
        val session = PlayerHost(runtime).bindPlayer("Rome")
        check(session.turn().wonderOpportunities.none { it.name == "The Great Library" })
        for (tech in listOf("Writing", "Philosophy", "Education")) player.tech.addTechnology(tech, false)
        for (city in player.cities) city.cityStats.update()
        val raw = json().toJson(game)
        val observed = session.turn().wonderOpportunities
        check(json().toJson(game) == raw) { "Wonder observation mutated the save" }
        check(observed == session.turn().wonderOpportunities)
        check(observed.map { it.name }.distinct().size == observed.size)
        val library = observed.single { it.name == "The Great Library" }
        check(library.kind == "WORLD" && library.benefits.contains("Free Technology"))
        check(library.greatPersonPoints == mapOf("Great Scientist" to 1))
        check(library.cities.size == 2 && library.cities.all { it.canQueue && !it.queued })
        val capitalOption = library.cities.single { it.city == session.turn().cities.first { c -> c.name == capital.name }.handle }
        check(capitalOption.productionCost == game.ruleset.buildings.getValue(library.name).getProductionCost(player, capital))
        check(capitalOption.turns == capital.cityConstructions.turnsToConstruction(library.name))
        check(library.benefits != game.ruleset.buildings.getValue(library.name).quote)
        val oxford = observed.single { it.name == "Oxford University" }
        check(oxford.kind == "NATIONAL" && oxford.benefits.contains("Free Technology"))
        check(oxford.cities.all { !it.canQueue && it.unavailableReasons.isNotEmpty() })
        check(observed.none { it.name == "Sistine Chapel" })
        val rival = game.getCivilization("France").cities.single()
        rival.cityConstructions.inProgressConstructions["The Oracle"] = 60
        rival.cityConstructions.constructionQueue = arrayListOf("The Oracle")
        check(session.turn().wonderOpportunities == observed) { "Rival wonder progress leaked" }
        rival.cityConstructions.addBuilding("The Great Library")
        check(session.turn().wonderOpportunities.none { it.name == "The Great Library" })
        capital.cityConstructions.constructionQueue = arrayListOf("The Oracle")
        check(session.turn().wonderOpportunities.single { it.name == "The Oracle" }.cities.any { it.queued })
        capital.cityConstructions.addBuilding("The Oracle")
        capital.cityConstructions.constructionQueue = arrayListOf("Monument")
        check(session.turn().wonderOpportunities.none { it.name == "The Oracle" })
        for (city in player.cities) {
            city.cityConstructions.addBuilding("Library")
            city.cityConstructions.addBuilding("University")
        }
        check(session.turn().wonderOpportunities.single { it.name == "Oxford University" }.cities.all { it.canQueue })
        player.tech.addTechnology("Acoustics", false)
        val sistine = session.turn().wonderOpportunities.single { it.name == "Sistine Chapel" }
        check(sistine.benefits.contains("25") && sistine.benefits.contains("Culture"))
        check(sistine.greatPersonPoints.isNotEmpty())
    }
}
