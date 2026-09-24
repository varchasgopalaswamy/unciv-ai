package uncivai

import com.unciv.json.json
import com.unciv.logic.civilization.PlayerCityOperations
import com.unciv.logic.civilization.PlayerOperations
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.models.stats.Stat
import com.unciv.view.GameView
import uncivai.player.PlayerHost
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import uncivai.player.observeCityDevelopment
import uncivai.player.observeResearchDevelopment
import uncivai.player.observeTileYields
import java.nio.file.Path
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.util.Locale
import kotlin.math.roundToInt

object CityDevelopmentProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        HeadlessRuntime.createDuel(Path.of(args.single()).resolve("data"), 412L).use { runtime ->
            val game = runtime.game
            val player = game.getCivilization("Rome")
            val units = PlayerUnitOperations(player)
            check(units.tryFoundCity(player.units.getCivUnits().single { it.name == "Settler" }))
            val city = player.cities.single()
            val operations = PlayerOperations(player)
            check(operations.tryQueueConstruction(city, "Worker"))
            check(operations.tryQueueConstruction(city, "Worker"))
            check(operations.trySetResearchQueue(listOf("Pottery")))
            city.cityConstructions.inProgressConstructions["Worker"] = 7
            city.population.foodStored = 3
            city.cityStats.update()
            val raw = json().toJson(game)
            val observed = observeCityDevelopment(city)
            val research = observeResearchDevelopment(player)
            check(json().toJson(game) == raw) { "Observation mutated the saved game" }
            check(observed == observeCityDevelopment(city))
            val view = GameView(game, player).getCityView(city)
            check(observed.population == view.getPopulationCount())
            check(observed.foodStored == view.getFoodStored())
            check(observed.foodToNextPopulation == view.getFoodToNextPopulation())
            check(observed.turnsToGrowth == view.getNumTurnsToNewPopulation())
            for (value in observed.yields) {
                val stat = Stat.valueOf(value.stat)
                val expected = if (stat == Stat.Happiness) view.getHappinessList().values.sum() else view.getCurrentCityStats()[stat]
                check(value.amount == expected.roundToInt().toString())
            }
            val decimal = DecimalFormat("0.#", DecimalFormatSymbols(Locale.ROOT))
            for (entry in observed.finalYieldBreakdown) {
                for (value in entry.values) {
                    val stat = Stat.valueOf(value.stat)
                    val expected = if (stat == Stat.Happiness && view.getFinalStatList()[entry.source]?.happiness in listOf(null, 0f)) {
                        view.getHappinessList().getValue(entry.source)
                    } else {
                        view.getFinalStatList().getValue(entry.source)[stat]
                    }
                    check(value.amount == decimal.format(expected))
                }
            }
            check(observed.constructions.map { it.productionStored } == listOf(7, 0))
            check(observed.constructions[0].turns == view.constructions.turnsToConstruction("Worker", true))
            check(observed.constructions[1].turns == view.constructions.turnsToConstruction("Worker", false))
            check(research.current == "Pottery")
            val pottery = research.technologies.single { it.name == "Pottery" }
            check(pottery.cost == player.tech.costOfTech("Pottery") && pottery.turns == player.tech.turnsToTech("Pottery"))
            player.tech.addTechnology("Pottery")
            check(operations.trySetResearchQueue(listOf("Mining")))
            player.tech.techsInProgress["Mining"] = 20
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val choices = session.researchDetails(listOf("Calendar"))
            check(choices.current == "Mining" && choices.researchQueue == listOf("Mining"))
            check(choices.technologies.single().prerequisiteQueue == listOf("Calendar"))
            for ((index, queue) in listOf(listOf("Calendar"), listOf("Mining", "Calendar")).withIndex()) {
                val turn = session.turn()
                session.execute(TurnCommandRequest("research-$index", turn.epoch, turn.revision, TurnCommandKind.SET_RESEARCH_QUEUE, queue = queue))
                check(player.tech.techsToResearch == queue)
                check(player.tech.currentTechnologyName() == queue.first())
                check(player.tech.researchOfTech("Mining") == 20)
                check(!player.tech.isResearched("Mining"))
            }
            check(choices.researchQueue == listOf("Mining")) { "Research detail queue changed after mutation" }
            val cityOperations = PlayerCityOperations(player)
            // Research changes can reassign citizens. Inspect the current choice
            // before testing mutation and detachment of the returned snapshot.
            val beforeUnwork = observeCityDevelopment(city)
            val worked = beforeUnwork.tiles.first { it.canUnwork }
            val tile = game.tileMap[com.unciv.logic.map.HexCoord(worked.position.x, worked.position.y)]
            check(cityOperations.trySetWorkedTile(city, tile, false))
            check(beforeUnwork != observeCityDevelopment(city))
            check(beforeUnwork.tiles.first { it.position == worked.position }.worked) { "Returned DTO changed after mutation" }

            val hidden = game.tileMap.tileList.first { !it.isExplored(player) }
            hidden.setExplored(player, true)
            player.setLastSeenImprovement(hidden.position, "Farm")
            check(observeTileYields(player, hidden) == null)
            hidden.improvement = "Mine"
            check(observeTileYields(player, hidden) == null)
            val visible = city.getCenterTile()
            val beforeResource = observeTileYields(player, visible)
            visible.tileResource = game.ruleset.tileResources.getValue("Uranium")
            check(observeTileYields(player, visible) == beforeResource) { "Unrevealed resource changed yields" }

            // The world tile information panel truncates fractional yields.
            val tileView = GameView(game, player).tileMapView.getTile(visible)
            val terrain = game.ruleset.terrains.getValue(visible.baseTerrain)
            terrain.food += 10.75f
            val shown = tileView.getTileStats(GameView(game, player).civView)
            check(shown.food != shown.food.toInt().toFloat())
            for (value in observeTileYields(player, visible)!!) check(value.amount == shown[Stat.valueOf(value.stat)].toInt().toString())
            terrain.food -= 10.75f
            game.currentPlayer = "Greece"
            game.currentPlayerCiv = game.getCivilization("Greece")
            val waiting = observeCityDevelopment(city)
            check(waiting.tiles.none { it.canWork || it.canUnwork || it.canLock || it.canUnlock })
            check(waiting.constructions.none { it.canRemove || it.moveToIndices.isNotEmpty() })
        }
    }
}
