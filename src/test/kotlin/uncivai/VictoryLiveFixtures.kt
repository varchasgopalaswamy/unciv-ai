package uncivai

import com.unciv.logic.files.UncivFiles
import com.unciv.logic.map.tile.RoadStatus
import java.nio.file.Files
import java.nio.file.Path

/** Initial positions only. The live scheduler and models supply every subsequent decision. */
object VictoryLiveFixtures {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        require(root.isAbsolute && !Files.exists(root)) { "Choose a new absolute fixture directory" }
        for (kind in listOf("spaceship", "election")) {
            val directory = root.resolve(kind)
            Files.createDirectories(directory)
            VictoryDecisionsProbe.create(directory.resolve("fixture-data")).use { runtime ->
                val game = runtime.game
                game.gameParameters.noBarbarians = true
                game.tileMap.mapParameters.name = "Victory live acceptance: $kind"
                if (kind == "spaceship") {
                    val rome = game.getCivilization("Rome")
                    val capital = rome.getCapital()!!
                    val site = capital.getCenterTile().getTilesAtDistance(5).first { tile ->
                        tile.isLand && !tile.isImpassible() && tile.getOwner() == null &&
                            game.getCities().all { it.getCenterTile().aerialDistanceTo(tile) >= 5 }
                    }
                    val second = rome.addCity(site.position)
                    for (city in listOf(capital, second)) {
                        val fields = city.getCenterTile().getTilesInDistance(3)
                            .filter { !it.isCityCenter() && it.getOwner() == null }.toList()
                        fields.forEach { city.expansion.takeOwnership(it) }
                        for ((index, tile) in city.getTiles().filter { !it.isCityCenter() }.withIndex()) {
                            tile.setBaseTerrain(game.ruleset.terrains.getValue("Grassland"))
                            tile.setTerrainFeatures(if (index < 8) arrayListOf() else arrayListOf("Hill"))
                            tile.setTileResource(null)
                            tile.setImprovement(if (index < 8) "Farm" else "Mine", rome)
                            tile.setRoadStatus(RoadStatus.Railroad, rome)
                        }
                        city.getCenterTile().setRoadStatus(RoadStatus.Railroad, rome)
                        city.population.setPopulation(20)
                        city.avoidGrowth = true
                        for (building in listOf("Granary", "Hospital", "Workshop", "Factory", "Nuclear Plant", "Spaceship Factory", "Colosseum", "Theatre", "Stadium")) {
                            city.cityConstructions.addBuilding(building)
                        }
                    }
                    val mines = rome.cities.flatMap { it.getTiles().toList() }.filter { it.improvement == "Mine" }
                    for ((tile, resource) in mines.take(3).zip(listOf("Aluminum", "Coal", "Uranium"))) {
                        tile.setTileResource(resource)
                        tile.resourceAmount = if (resource == "Aluminum") 4 else 2
                    }
                    for ((tile, resource) in mines.drop(3).take(4).zip(listOf("Gold", "Silver", "Gems", "Marble"))) {
                        tile.setTileResource(resource)
                        if (resource == "Marble") tile.setImprovement("Quarry", rome)
                    }
                    capital.cityConstructions.addBuilding("Apollo Program")
                    capital.cityConstructions.addBuilding("Ironworks")
                    // A native road connection, with no supplied part production or units.
                    for (tile in capital.getCenterTile().getTilesInDistance(6).filter { it.isLand && !it.isImpassible() && (it.getOwner() == null || it.getOwner() === rome) }) {
                        tile.setRoadStatus(RoadStatus.Railroad, rome)
                    }
                } else {
                    VictoryDecisionsProbe.arrangeVote(runtime)
                }
                for (id in listOf("Rome", "Greece", "France")) {
                    val player = game.getCivilization(id)
                    for (technology in game.ruleset.technologies.keys.filterNot { it == "Future Tech" }) player.tech.addTechnology(technology, false)
                    player.tech.techsToResearch = arrayListOf("Future Tech")
                    player.addGold(10000 - player.gold)
                    for (city in player.cities) {
                        city.cityConstructions.constructionQueue = arrayListOf("Gold")
                        city.reassignPopulation(resetLocked = true)
                        city.cityStats.update()
                    }
                    for (other in game.getAliveMajorCivs().filter { it !== player }) {
                        if (!player.knows(other)) player.diplomacyFunctions.makeCivilizationsMeet(other)
                    }
                    player.cache.updateCivResources()
                    player.cache.updateViewableTiles()
                    player.updateStatsForNextTurn()
                    player.popupAlerts.clear()
                    player.notifications.clear()
                    check(player.victoryManager.currentsSpaceshipParts.isEmpty())
                    check(player.units.getCivUnits().none { it.name.startsWith("SS ") })
                    check(player.cities.all { city -> game.ruleset.victories.getValue("Scientific").requiredSpaceshipParts.all { city.cityConstructions.getWorkDone(it) == 0 } })
                }
                Files.writeString(directory.resolve("initial-game.json"), UncivFiles.gameInfoToString(game, forceZip = false))
                runtime.executor.enableCheckpoints(directory.resolve("exploration.json"))
                for (city in game.getCivilization("Rome").cities) println("$kind: ${city.name}: ${city.cityStats.currentCityStats}")
                println("Prepared $kind: ${directory.resolve("exploration.json")}")
            }
            HeadlessRuntime.loadCheckpoint(directory.resolve("exploration.json"), directory.resolve("reload-check")).use { runtime ->
                check(runtime.game.currentPlayer == "Rome" && runtime.game.victoryData == null)
            }
        }
    }
}
