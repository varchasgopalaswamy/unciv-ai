package uncivai

import com.unciv.json.json
import com.unciv.logic.city.CityFlags
import com.unciv.models.stats.Stat
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import java.nio.file.Path
import kotlin.math.abs

object BuildingEstimatesProbe {
    fun saved(root: Path, fixtures: Path) = HeadlessRuntime.load(fixtures.resolve("strategy_positions/slow-economy.json"), root.resolve("game")).use { runtime ->
        val player = runtime.game.getCivilization("France")
        val before = json().toJson(runtime.game)
        val session = PlayerHost(runtime).bindPlayer("France")
        val start = System.nanoTime()
        val turn = session.turn()
        val firstMillis = (System.nanoTime() - start) / 1_000_000
        val repeatedAt = System.nanoTime()
        val repeated = session.turn()
        val repeatedMillis = (System.nanoTime() - repeatedAt) / 1_000_000
        check(turn == repeated && json().toJson(runtime.game) == before)
        for (city in turn.cities) check(city.constructionEstimates === repeated.cities.single { it.handle == city.handle }.constructionEstimates)
        val orleans = turn.cities.single { it.name == "Orleans" }
        val library = orleans.constructionEstimates.getValue("Library")
        val granary = orleans.constructionEstimates.getValue("Granary")
        check(granary.completion!!.yields.change.getValue("Food").toFloat() > 0)
        // Unhappiness limits the extra food, and both 0.75 and 1.25 round to 1
        // native stored food. A positive fractional gain need not speed growth.
        check(granary.completion.growth.foodPerTurnBefore == 1 && granary.completion.growth.foodPerTurnAfter == 1)
        check(granary.completion.growth.turnsToGrowthBefore == 9 && granary.completion.growth.turnsToGrowthAfter == 9)
        check(orleans.development!!.population == 1)
        check(library.completion!!.yields.change.getValue("Science") == "0")
        check(player.cities.size == turn.cities.size)
        println("building estimates: saved turn ${turn.turn}, ${turn.cities.size} cities, ${firstMillis}ms first/${repeatedMillis}ms repeated observation; Orleans population=${orleans.development.population}, Library science=${library.completion.yields.change.getValue("Science")}, turns=${library.production.turnsIfStartedNow}; Granary food=${granary.completion.yields.change["Food"]}, turns=${granary.production.turnsIfStartedNow}, growth=${granary.completion.growth}")
    }

    fun run(root: Path) = SettlementPreviewProbe.fixture(root.resolve("game")).use { runtime ->
        val game = runtime.game
        // The settlement fixture paints over generated rivers directly. Use the
        // native editor operation here to clear its previously cached adjacency.
        for (tile in game.tileMap.values) {
            for (neighbor in tile.neighbors) {
                tile.setConnectedByRiver(neighbor, true)
                tile.setConnectedByRiver(neighbor, false)
            }
        }
        val player = game.getCivilization("Rome")
        val city = player.cities.single()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val first = session.turn().cities.single()
        check("Library" !in first.constructionEstimates)
        // Locked victory requirements still have native planning costs, without
        // becoming legal construction options or needing a technology grant.
        for (name in listOf("Apollo Program", "Utopia Project", "United Nations", "SS Engine", "SS Booster", "SS Cockpit", "SS Stasis Chamber")) {
            val estimate = first.constructionEstimates.getValue(name)
            val construction: com.unciv.models.ruleset.INonPerpetualConstruction =
                game.ruleset.buildings[name] ?: game.ruleset.units.getValue(name)
            check(estimate.production.cost == construction.getProductionCost(player, city))
            check(estimate.requiredTechnologies == construction.requiredTechs().sorted().toList())
            check(estimate.queueUnavailableReasons.isNotEmpty())
            check(name !in first.constructionOptions)
        }
        for (tech in listOf("Pottery", "Writing", "Philosophy", "Currency", "Metal Casting", "Education")) {
            player.tech.addTechnology(tech, false)
        }
        city.population.setPopulation(6)
        city.cityConstructions.constructionQueue = arrayListOf("Science")
        city.cityStats.update()
        val raw = json().toJson(game)
        val owners = game.tileMap.values.associateWith { it.getCity() }
        val liveStats = city.cityStats.currentCityStats
        val started = System.nanoTime()
        val turn = session.turn()
        val elapsed = (System.nanoTime() - started) / 1_000_000
        val estimates = turn.cities.single().constructionEstimates
        val library = estimates.getValue("Library")
        val effect = checkNotNull(library.completion)
        check(effect.yields.change.getValue("Science") == "3")
        check(effect.maintenanceChange == "1" && effect.yields.change.getValue("Gold") == "-1")
        check(library.whileBuilding.change.getValue("Science").toFloat() < 0)
        check(library.production.turnsIfStartedNow == city.cityConstructions.turnsToConstruction("Library"))
        check(estimates.getValue("University").queueUnavailableReasons.isNotEmpty())
        check(estimates.getValue("University").completion!!.specialistSlots.getValue("Scientist") == 2)
        check(estimates.any { (name, estimate) -> name in game.ruleset.units && estimate.completion == null })
        check(json().toJson(game) == raw)
        check(city.cityStats.currentCityStats === liveStats)
        check(owners.all { (tile, owner) -> tile.getCity() === owner })
        check(estimates === session.turn().cities.single().constructionEstimates)
        check(runCatching { (estimates as MutableMap).clear() }.exceptionOrNull() is UnsupportedOperationException)
        check(runCatching { (effect.yields.change as MutableMap).clear() }.exceptionOrNull() is UnsupportedOperationException)
        GameplayTestApi(runtime, root.resolve("tls")).use { api ->
            check(api.turn(api.playerToken("Rome")).cities.single().constructionEstimates == estimates)
        }
        val rival = game.getCivilization("France").cities.single()
        val rivalQueue = rival.cityConstructions.constructionQueue
        val rivalWork = rival.cityConstructions.inProgressConstructions.toMap()
        rival.cityConstructions.constructionQueue = arrayListOf("The Great Library")
        rival.cityConstructions.inProgressConstructions["The Great Library"] = 50
        check(session.turn().cities.single().constructionEstimates == estimates)
        rival.cityConstructions.constructionQueue = rivalQueue
        rival.cityConstructions.inProgressConstructions.clear()
        rival.cityConstructions.inProgressConstructions.putAll(rivalWork)

        // Compare completion at exactly the assignments declared by the estimate.
        val worked = city.workedTiles.toHashSet()
        val specialists = city.population.specialistAllocations.clone()
        city.cityConstructions.constructionQueue = arrayListOf("Nothing")
        city.cityConstructions.addBuilding("Library")
        city.workedTiles = worked
        city.population.specialistAllocations.clear()
        city.population.specialistAllocations.putAll(specialists)
        city.cityStats.update()
        for (stat in listOf(Stat.Science, Stat.Food, Stat.Production, Stat.Gold)) {
            check(abs(effect.yields.after.getValue(stat.name).toFloat() - city.cityStats.currentCityStats[stat]) < 0.051f) {
                "$stat: preview=${effect.yields.after}, actual=${city.cityStats.currentCityStats}; worked=$worked, specialists=$specialists, sources=${effect.sourceChanges}, actualSources=${city.cityStats.finalStatList}"
            }
        }
        check("Library" !in session.turn().cities.single().constructionEstimates)
        val university = session.turn().cities.single().constructionEstimates.getValue("University")
        check(university.queueUnavailableReasons.isEmpty())
        city.population.setPopulation(2)
        city.cityStats.update()
        val smaller = session.turn().cities.single().constructionEstimates.getValue("University")
        check(smaller.completion!!.yields.change.getValue("Science").toFloat() < university.completion!!.yields.change.getValue("Science").toFloat())
        city.setFlag(CityFlags.Resistance, 2)
        city.cityConstructions.constructionQueue = arrayListOf("University", "Warrior", "Warrior")
        city.cityStats.update()
        val resistant = session.turn()
        check(resistant.cities.single().constructionEstimates.getValue("University").production.turnsIfStartedNow == null)
        check(resistant.cities.single().development!!.constructions.all { it.turns == null })
        check(resistant.wonderOpportunities.flatMap { it.cities }.filter { it.city == resistant.cities.single().handle }.all { it.turns == null })
        city.removeFlag(CityFlags.Resistance)
        city.cityConstructions.inProgressConstructions["Warrior"] = 5
        city.cityStats.update()
        val resumed = session.turn().cities.single()
        check(resumed.development!!.constructions.first().turns == resumed.constructionEstimates.getValue("University").production.turnsIfStartedNow)
        val warriors = resumed.development.constructions.filter { it.name == "Warrior" }
        check(warriors[0].productionStored == 5 && warriors[1].productionStored == 0)
        check(warriors[0].turns == resumed.constructionEstimates.getValue("Warrior").production.turnsIfStartedNow)
        println("building estimates: ${estimates.size} choices, ${elapsed}ms full observation, ${PlayerTurnJson.json.encodeToString(turn).length} snapshot characters")
    }
}
