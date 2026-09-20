package uncivai

import com.unciv.logic.civilization.PlayerAirOperations
import com.unciv.logic.civilization.PlayerUnitEconomyOperations
import com.unciv.logic.files.UncivFiles
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.MapUnit
import java.nio.file.Files
import java.nio.file.Path

internal object AirFixtures {
    val players = listOf("Rome", "Greece", "France")
    val tokens = players.associateWith { it.first().toString().repeat(43) }
    val carrierStart = HexCoord(-3, -2)
    val carrierEnd = HexCoord(0, -2)
    val strikeTarget = HexCoord(3, 0)
    val missileTarget = HexCoord(3, 1)
    val dropTarget = HexCoord(-3, 6)
    val nuclearTarget = HexCoord(6, 6)

    fun create(root: Path): HeadlessRuntime = HeadlessRuntime.createVanillaGame(root, 413L).also { runtime ->
        val game = runtime.game
        game.gameParameters.noBarbarians = true
        for (civ in game.civilizations) civ.units.getCivUnits().toList().forEach { it.destroy() }
        for (tile in game.tileMap.values) {
            tile.naturalWonder = null
            tile.setBaseTerrain(game.ruleset.terrains.getValue(if (tile.position.y in -4..-2) "Coast" else "Grassland"))
            tile.setTerrainFeatures(arrayListOf())
            tile.setTileResource(null)
            tile.setImprovement(null)
            tile.setTerrainTransients()
            tile.hasBottomRiver = false
            tile.hasBottomLeftRiver = false
            tile.hasBottomRightRiver = false
        }
        val cities = mapOf(
            "Rome" to listOf(HexCoord(-6, 0), HexCoord(-1, 0)),
            "Greece" to listOf(HexCoord(7, 0), nuclearTarget),
            "France" to listOf(HexCoord(0, 8)),
        )
        for ((id, positions) in cities) {
            val civ = game.getCivilization(id)
            for (position in positions) {
                val city = civ.addCity(position)
                city.population.setPopulation(10)
                city.cityConstructions.constructionQueue = arrayListOf("Market", "Library", "Monument", "Granary")
            }
            for (tech in game.ruleset.technologies.keys.filter { it != "Future Tech" }) civ.tech.addTechnology(tech, false)
            civ.tech.techsToResearch = arrayListOf("Future Tech")
            civ.addGold(20000)
            val capital = civ.getCapital()!!
            for ((index, resource) in listOf("Uranium", "Oil", "Aluminum").withIndex()) {
                val tile = game.tileMap[positions.first().x - index, positions.first().y]
                if (tile.getOwner() !== civ) capital.expansion.takeOwnership(tile)
                tile.setTileResource(resource)
                tile.resourceAmount = 20
                if (!tile.isCityCenter()) tile.setImprovement(if (resource == "Oil") "Oil well" else "Mine", civ)
            }
            civ.cache.updateCivResources()
        }
        add(runtime, "Rome", "Carrier", carrierStart, "Carrier")
        add(runtime, "Rome", "Fighter", HexCoord(-6, 0), "Carrier wing")
        add(runtime, "Rome", "Fighter", HexCoord(-6, 0), "Reserve wing")
        add(runtime, "Rome", "Bomber", HexCoord(-6, 0), "Retired bomber")
        add(runtime, "Rome", "Fighter", HexCoord(-1, 0), "Sweep wing")
        add(runtime, "Rome", "Bomber", HexCoord(-1, 0), "Strike wing")
        add(runtime, "Rome", "Guided Missile", HexCoord(-1, 0), "Guided strike")
        add(runtime, "Rome", "Scout", HexCoord(4, 2), "Forward observer")
        add(runtime, "Greece", "Fighter", HexCoord(7, 0), "Interceptor one")
        add(runtime, "Greece", "Fighter", HexCoord(7, 0), "Interceptor two")
        add(runtime, "Greece", "Infantry", strikeTarget, "Strike target")
        add(runtime, "Greece", "Infantry", missileTarget, "Missile target")
        add(runtime, "Greece", "Infantry", nuclearTarget, "Nuclear target")
        add(runtime, "France", "Paratrooper", HexCoord(0, 8), "Drop team")
        add(runtime, "France", "Nuclear Missile", HexCoord(0, 8), "Nuclear trial")
        add(runtime, "France", "Scout", HexCoord(-4, 5), "Drop observer")
        for (id in players) {
            val civ = game.getCivilization(id)
            for (other in players.filter { it != id }.map(game::getCivilization)) if (!civ.knows(other)) civ.diplomacyFunctions.makeCivilizationsMeet(other)
            for (tile in game.tileMap.values) tile.setExplored(civ, true)
            civ.cache.updateViewableTiles()
            civ.cache.updateCivResources()
            civ.popupAlerts.clear()
            civ.notifications.clear()
        }
        game.tileMap.mapParameters.name = "Air warfare acceptance"
        val rome = game.getCivilization("Rome")
        val wing = rome.units.getCivUnits().single { it.instanceName == "Carrier wing" }
        check(PlayerAirOperations(rome).preview(wing, PlayerAirOperations.Mission.REBASE, game.tileMap[carrierStart])!!.available)
        val reserve = rome.units.getCivUnits().single { it.instanceName == "Reserve wing" }
        check(PlayerUnitEconomyOperations(rome).upgrades(reserve).single { it.targetName == "Jet Fighter" }.available)
        check(players.all { game.getCivilization(it).getCivResourcesByName()["Uranium"]!! >= 18 })
    }

    fun add(runtime: HeadlessRuntime, id: String, name: String, position: HexCoord, label: String): MapUnit {
        val unit = checkNotNull(runtime.game.getCivilization(id).units.placeUnitNearTile(position, name))
        check(unit.currentTile.position == position) { "$label placed at ${unit.currentTile.position}, expected $position" }
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
            check(runtime.game.getCivilization("Rome").units.getCivUnits().count() == 8)
        }
        println("AIR_FIXTURE_READY")
    }
}
