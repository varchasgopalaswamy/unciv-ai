package uncivai

import com.unciv.json.json
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import java.nio.file.Path
import kotlin.math.ceil
import kotlin.math.roundToInt

object MechanicsOverviewProbe {
    fun run(root: Path) = SettlementPreviewProbe.fixture(root.resolve("game")).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val city = player.cities.single()
        player.tech.addTechnology("Pottery", false)
        player.tech.techsToResearch = arrayListOf("Writing", "Philosophy")
        player.policies.freePolicies = 1
        player.policies.adopt(game.ruleset.policies.getValue("Tradition"))
        player.policies.storedCulture = 10
        city.population.setPopulation(4)
        city.cityConstructions.addBuilding("University")
        city.population.specialistAllocations["Scientist"] = 1
        city.cityStats.update()
        val before = json().toJson(game)
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val turn = session.turn()
        check(turn.cities.single().health == city.health)
        check(turn.cities.single().maxHealth == com.unciv.logic.battle.CityCombatant(city).getMaxHealth())
        check(turn.cities.single().defensiveStrength == com.unciv.logic.battle.CityCombatant(city).getDefendingStrength(null))
        check(json().toJson(game) == before) { "Mechanics summaries mutated the game" }
        val research = turn.research!!
        check(research.current == "Writing")
        check(research.sciencePerTurn == player.stats.statsForNextTurn.science.roundToInt().toString())
        check(research.technologies.single { it.name == "Writing" }.unlocks!!.buildings.contains("Library"))
        check(research.technologies.single { it.name == "Writing" }.turns == player.tech.turnsToTech("Writing"))
        val culture = turn.culture!!
        check(culture.branches.single().name == "Tradition" && culture.branches.single().remaining.isNotEmpty())
        check(culture.nextPolicyCost == player.policies.getCultureNeededForNextPolicy())
        check(culture.culturePerTurn == player.stats.statsForNextTurn.culture.toInt())
        check(culture.turnsToNextPolicy == ceil((culture.nextPolicyCost - culture.storedCulture).toDouble() / culture.culturePerTurn).toInt())
        val scientist = turn.greatPeople.single { it.name == "Great Scientist" }
        check(scientist.pointsPerTurn > 0 && scientist.threshold == player.greatPeople.getPointsRequiredForGreatPerson(scientist.name))
        check(scientist.pointsPerTurn == turn.cities.sumOf { it.management!!.greatPeople.single { gp -> gp.name == scientist.name }.pointsPerTurn })
        GameplayTestApi(runtime, root.resolve("tls")).use { api ->
            val served = api.turn(api.playerToken("Rome"))
            check(served.culture == culture && served.greatPeople == turn.greatPeople)
        }
        java.nio.file.Files.writeString(root.resolve("mechanics-turn.json"), PlayerTurnJson.json.encodeToString(turn))
    }

    fun expansion(root: Path) = SettlementPreviewProbe.fixture(root.resolve("game")).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val spices = game.tileMap[0, 4]
        spices.setTileResource(game.ruleset.tileResources.getValue("Spices"))
        spices.setTerrainFeatures(listOf("Forest"))
        spices.setTerrainTransients()
        val fish = game.tileMap[1, 4]
        fish.setBaseTerrain(game.ruleset.terrains.getValue("Coast"))
        fish.setTileResource(game.ruleset.tileResources.getValue("Fish"))
        fish.setTerrainTransients()
        val iron = game.tileMap[0, 5]
        iron.setTileResource(game.ruleset.tileResources.getValue("Iron"))
        iron.resourceAmount = 4
        player.viewableTiles = game.tileMap.values.toSet()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val before = json().toJson(game)
        var sites = session.turn().settlement!!
        check(json().toJson(game) == before) { "Expansion projection mutated native state" }
        check(sites.candidates.flatMap { it.resources }.none { it.name == "Iron" })
        val plantation = sites.candidates.flatMap { it.resources }.first { it.name == "Spices" && it.distance > 0 }
        check(plantation.type == "Luxury" && plantation.owner == null && plantation.happinessIfConnected!! > 0)
        check(plantation.improvements.single { it.name == "Plantation" }.missingTechnologies == listOf("Calendar", "Mining"))
        val underCity = sites.candidates.single { it.position.x == 0 && it.position.y == 4 }.resources.single { it.name == "Spices" && it.distance == 0 }
        check(underCity.connectedByFounding == false && underCity.builder == null)
        check(underCity.improvements.single().missingTechnologies == listOf("Calendar") && underCity.improvements.single().firstJob == null)
        check(sites.candidates.flatMap { it.resources }.first { it.name == "Fish" }.builder == "Work Boats")
        check(sites.recommendations.size == 3)
        for ((index, position) in sites.recommendations.withIndex()) {
            val center = game.tileMap[position.x, position.y]
            check(sites.recommendations.drop(index + 1).all { center.aerialDistanceTo(game.tileMap[it.x, it.y]) > game.ruleset.modOptions.constants.minimalCityDistance })
        }
        check(sites.candidates.filter { it.position in sites.recommendations }.all { it.happiness!!.populationCosts.size == 3 })

        player.tech.addTechnology(game.ruleset.tileResources.getValue("Iron").revealedBy!!, false)
        player.viewableTiles = game.tileMap.values.toSet()
        sites = session.turn().settlement!!
        check(sites.candidates.flatMap { it.resources }.any { it.name == "Iron" && it.amount == 4 && it.type == "Strategic" })
        player.viewableTiles = player.cities.single().getTiles().toSet()
        val remembered = session.turn().settlement!!
        check(remembered.happinessBaseline!!.populationCosts.first().cityUnhappiness > 0)
        check(remembered.happinessBaseline!!.buildingCatalogue.single { it.name == "Colosseum" }.missingTechnologies.contains("Construction"))
        spices.setTileResource(game.ruleset.tileResources.getValue("Gold Ore"))
        game.getCivilization("France").cities.single().expansion.takeOwnership(iron)
        player.viewableTiles = player.cities.single().getTiles().toSet()
        check(session.turn().settlement == remembered) { "Hidden resources or ownership leaked into advice" }
        check(remembered.candidates.filter { it.position in remembered.recommendations }.any { it.happiness == null })

        // Resource work prerequisites follow our new research without rereading hidden tiles.
        player.tech.addTechnology("Calendar", false)
        val learned = session.turn().settlement!!.candidates.flatMap { it.resources }.first { it.name == "Spices" && it.distance > 0 }
        check(learned.improvements.single { it.name == "Plantation" }.missingTechnologies == listOf("Mining"))
        check(learned.observedTurn == plantation.observedTurn)
    }
}
