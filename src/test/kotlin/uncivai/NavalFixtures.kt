package uncivai

import com.unciv.logic.civilization.PlayerUnitEconomyOperations
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.civilization.PlayerWaterImprovementOperations
import com.unciv.logic.files.UncivFiles
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.MapUnit
import java.nio.file.Files
import java.nio.file.Path

internal object NavalFixtures {
    val players = listOf("Rome", "Greece", "France")
    val tokens = players.associateWith { it.first().toString().repeat(43) }
    val routeDestination = HexCoord(1, -2)
    val settlement = HexCoord(-4, 7)

    fun create(root: Path): HeadlessRuntime = HeadlessRuntime.createVanillaGame(root, 412L).also { runtime ->
        val game = runtime.game
        game.gameParameters.noBarbarians = true
        for (civ in game.civilizations) civ.units.getCivUnits().toList().forEach { it.destroy() }
        for (tile in game.tileMap.values) {
            tile.naturalWonder = null
            tile.setBaseTerrain(game.ruleset.terrains.getValue("Coast"))
            tile.setTerrainFeatures(arrayListOf())
            tile.setTileResource(null)
            tile.setImprovement(null)
            tile.setTerrainTransients()
            tile.hasBottomRiver = false
            tile.hasBottomLeftRiver = false
            tile.hasBottomRightRiver = false
        }
        val capitals = listOf(HexCoord(-8, 0), HexCoord(7, 0), HexCoord(0, 8))
        fun land(position: HexCoord) = game.tileMap[position].setBaseTerrain(game.ruleset.terrains.getValue("Grassland"))
        for ((id, position) in players.zip(capitals)) {
            land(position)
            land(HexCoord(position.x - 1, position.y))
            val player = game.getCivilization(id)
            val city = player.addCity(position)
            city.population.setPopulation(5)
            city.cityConstructions.constructionQueue = arrayListOf("Market", "Library", "Monument", "Granary")
            player.addGold(5000)
            for (tech in listOf("Pottery", "Mining", "Bronze Working", "Iron Working", "Sailing", "Optics", "Compass", "Astronomy", "Navigation")) player.tech.addTechnology(tech, false)
            player.tech.techsToResearch = arrayListOf("Writing", "Philosophy", "Theology", "Education")
            val iron = game.tileMap[position.x - 1, position.y]
            iron.setTileResource("Iron")
            iron.resourceAmount = 6
            iron.setImprovement("Mine", player)
            player.cache.updateCivResources()
        }
        land(settlement)
        val rome = game.getCivilization("Rome")
        val whales = game.tileMap[-7, 0]
        whales.setTileResource("Whales")
        rome.cache.updateCivResources()
        add(runtime, "Rome", "Work Boats", -7, 0, "Resource crew")
        add(runtime, "Rome", "Trireme", -8, -1, "Upgrade ship")
        add(runtime, "Rome", "Caravel", -7, -2, "Voyager")
        add(runtime, "Rome", "Frigate", 5, 0, "Coastal battery")
        add(runtime, "Greece", "Trireme", 6, -1, "Defender")
        add(runtime, "Greece", "Work Boats", 7, 1, "Greek resource crew")
        game.tileMap[7, 1].setTileResource("Fish")
        add(runtime, "France", "Settler", 0, 8, "Overseas settlers")
        add(runtime, "France", "Caravel", 1, 8, "Survey ship")
        val enemyFish = game.tileMap[8, 0]
        enemyFish.setTileResource("Fish")
        enemyFish.setImprovement("Fishing Boats", game.getCivilization("Greece"))
        for (id in players) {
            val player = game.getCivilization(id)
            for (other in players.filter { it != id }.map(game::getCivilization)) if (!player.knows(other)) player.diplomacyFunctions.makeCivilizationsMeet(other)
            for (tile in game.tileMap.values) tile.setExplored(player, true)
            player.cache.updateViewableTiles()
            player.cache.updateCivResources()
            player.popupAlerts.clear()
            player.notifications.clear()
        }
        game.tileMap.mapParameters.name = "Naval gameplay acceptance"
        val workBoat = rome.units.getCivUnits().single { it.name == "Work Boats" }
        check(PlayerWaterImprovementOperations(rome).option(workBoat)?.available == true)
        val upgrade = rome.units.getCivUnits().single { it.name == "Trireme" }
        check(PlayerUnitEconomyOperations(rome).upgrades(upgrade).single { it.targetName == "Caravel" }.available)
        val voyager = rome.units.getCivUnits().single { it.instanceName == "Voyager" }
        check(PlayerUnitOperations(rome).route(voyager, game.tileMap[routeDestination])!!.turns.size > 1)
        check(players.all { game.getCivilization(it).gold >= 5000 })
    }

    fun add(runtime: HeadlessRuntime, id: String, name: String, x: Int, y: Int, label: String): MapUnit {
        val player = runtime.game.getCivilization(id)
        val unit = checkNotNull(player.units.placeUnitNearTile(HexCoord(x, y), name))
        check(unit.currentTile.position == HexCoord(x, y)) { "Cannot place $name at $x,$y: ${unit.currentTile.position}" }
        unit.instanceName = label
        return unit
    }

    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args.single())
        require(root.isAbsolute && !Files.exists(root))
        create(root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(root.resolve("exploration.json"))
            Files.writeString(root.resolve("initial-game.json"), UncivFiles.gameInfoToString(runtime.game, forceZip = false))
        }
        HeadlessRuntime.loadCheckpoint(root.resolve("exploration.json"), root.resolve("preflight")).use { runtime ->
            check(runtime.game.getCivilization("Rome").units.getCivUnits().count() == 4)
        }
        println("NAVAL_FIXTURE_READY")
    }
}
