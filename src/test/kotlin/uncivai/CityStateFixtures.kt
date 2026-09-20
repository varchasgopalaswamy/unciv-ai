package uncivai

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerCityStateOperations
import com.unciv.logic.civilization.PlayerGreatPersonOperations
import com.unciv.logic.civilization.diplomacy.CityStatePersonality
import com.unciv.logic.civilization.managers.quests.AssignedQuest
import com.unciv.logic.civilization.managers.quests.QuestManager
import com.unciv.logic.files.UncivFiles
import com.unciv.logic.map.mapunit.UnitSettlement
import java.nio.file.Files
import java.nio.file.Path
import kotlin.random.Random

internal object CityStateFixtures {
    val players = listOf("Rome", "Greece", "France")
    val tokens = players.associateWith { it.first().toString().repeat(43) }

    fun create(root: Path, meetAll: Boolean = true): HeadlessRuntime = HeadlessRuntime.createVanillaGame(root, 412L).also { runtime ->
        val game = runtime.game
        game.gameParameters.noBarbarians = true
        game.civilizations.filter { it.isBarbarian }.forEach { civ -> civ.units.getCivUnits().toList().forEach { it.destroy() } }
        for (id in players) {
            val player = game.getCivilization(id)
            val city = checkNotNull(UnitSettlement.tryFoundCity(player.units.getCivUnits().single { it.name == "Settler" }))
            player.addGold(5000)
            for (tech in listOf("Mining", "Bronze Working", "Pottery")) player.tech.addTechnology(tech, false)
            player.tech.techsToResearch = arrayListOf("Writing", "Philosophy", "Theology", "Education")
            city.cityConstructions.constructionQueue = ArrayList(List(12) { "Warrior" })
        }
        for ((index, name) in listOf("Geneva", "Sidon").withIndex()) {
            val minor = Civilization(game.ruleset.nations.getValue(name))
            minor.gameInfo = game
            game.civilizations.add(minor)
            minor.cache.updateState()
            minor.setTransients()
            minor.cityStateFunctions.initCityState(game.ruleset, "Ancient era", players.asSequence().map { game.getCivilization(it).nation }, Random(index))
            minor.cityStatePersonality = CityStatePersonality.Friendly
            minor.tech.addTechnology("Agriculture", false)
            minor.tech.addTechnology("Mining", false)
            minor.tech.addTechnology("Bronze Working", false)
            val reference = game.getCivilization(if (index == 0) "Rome" else "France").getCapital()!!.getCenterTile()
            val site = game.tileMap.values.filter { tile ->
                tile.isLand && !tile.isImpassible() && tile.getOwner() == null &&
                    game.getCities().all { it.getCenterTile().aerialDistanceTo(tile) >= 5 } &&
                    tile.neighbors.count { it.isLand && it.getOwner() == null } >= 5
            }.minWith(compareBy({ reference.aerialDistanceTo(it) }, { it.position.x }, { it.position.y }))
            for (tile in site.getTilesInDistance(2).filter { it.getOwner() == null }) {
                tile.setBaseTerrain(game.ruleset.terrains.getValue("Grassland"))
                tile.setTerrainFeatures(arrayListOf())
                tile.setTileResource(null)
            }
            val city = minor.addCity(site.position)
            city.population.setPopulation(5)
            city.cityConstructions.constructionQueue = ArrayList(List(12) { "Warrior" })
            val resource = city.getTiles().first { !it.isCityCenter() }
            resource.setTileResource("Gems")
            resource.setImprovement(null)
            minor.cache.updateCivResources()
        }
        game.gameParameters.numberOfCityStates = 2
        val geneva = game.getCivilization("Geneva")
        val sidon = game.getCivilization("Sidon")
        for (id in players) {
            val player = game.getCivilization(id)
            for (other in game.civilizations.filter { !it.isBarbarian && it !== player && (meetAll || (id == "Rome" && it === geneva)) }) {
                if (!player.knows(other)) player.diplomacyFunctions.makeCivilizationsMeet(other)
            }
            if (meetAll) for (minor in listOf(geneva, sidon)) for (tile in minor.getCapital()!!.getCenterTile().getTilesInDistance(2)) tile.setExplored(player, true)
        }
        if (meetAll) {
            val rome = game.getCivilization("Rome")
            val merchantTile = geneva.getCapital()!!.getTiles().first { !it.isCityCenter() }
            checkNotNull(rome.units.placeUnitNearTile(merchantTile.position, "Great Merchant"))
            val giftTile = geneva.getCapital()!!.getTiles().first { !it.isCityCenter() && it !== merchantTile }
            checkNotNull(rome.units.placeUnitNearTile(giftTile.position, "Warrior"))
            val france = game.getCivilization("France")
            for (tile in sidon.getCapital()!!.getCenterTile().getTilesAtDistance(2).filter { it.isLand }.take(6)) {
                checkNotNull(france.units.placeUnitNearTile(tile.position, "Giant Death Robot"))
            }
            addQuest(geneva, rome, "Give Gold", "France")
            addQuest(sidon, game.getCivilization("Greece"), "Pledge to Protect", "France")
        }
        for (civ in game.civilizations) {
            civ.cache.updateViewableTiles()
            civ.popupAlerts.clear()
            civ.notifications.clear()
        }
        game.tileMap.mapParameters.name = "City-state live acceptance"
        if (meetAll) verifyPreparedObjectives(runtime)
    }

    /** Fixture checks only; ordinary native quests may require earning Gold first. */
    private fun verifyPreparedObjectives(runtime: HeadlessRuntime) {
        val game = runtime.game
        val rome = game.getCivilization("Rome")
        val greece = game.getCivilization("Greece")
        val france = game.getCivilization("France")
        val geneva = game.getCivilization("Geneva")
        val sidon = game.getCivilization("Sidon")
        check(players.all { game.getCivilization(it).gold >= 5000 })
        val operations = PlayerCityStateOperations(rome)
        check(operations.options(geneva).single { it.name == "giftGold1000" }.available)
        check(operations.options(geneva).any { it.improvement == "Mine" && it.gold == -200 })
        check(rome.units.getCivUnits().any { operations.giftOption(it)?.available == true })
        val merchant = rome.units.getCivUnits().single { it.name == "Great Merchant" }
        check(PlayerGreatPersonOperations(rome).options(merchant).any { it.kind.name == "ConductTradeMission" && it.available })
        check(geneva.cityStateFunctions.otherCivCanPledgeProtection(rome))
        check(sidon.cityStateFunctions.otherCivCanPledgeProtection(greece))
        check(sidon.cityStateFunctions.getTributeWillingness(france) >= 0)
        check(geneva.questManager.getAssignedQuestsFor(rome).any { it.questName == "Give Gold" })
        check(sidon.questManager.getAssignedQuestsFor(greece).any { it.questName == "Pledge to Protect" })
    }

    fun addQuest(minor: Civilization, player: Civilization, name: String, data1: String = "", data2: String = "") {
        val field = QuestManager::class.java.getDeclaredField("assignedQuests").apply { isAccessible = true }

        @Suppress("UNCHECKED_CAST")
        val assigned = field.get(minor.questManager) as ArrayList<AssignedQuest>
        assigned.add(AssignedQuest(minor.gameInfo, minor.gameInfo.ruleset.quests.getValue(name), minor, player, minor.gameInfo.turns, data1, data2))
    }

    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args.single())
        require(root.isAbsolute && !Files.exists(root))
        Files.createDirectories(root)
        create(root.resolve("fixture-data")).use { runtime ->
            Files.writeString(root.resolve("initial-game.json"), UncivFiles.gameInfoToString(runtime.game, forceZip = false))
            runtime.executor.enableCheckpoints(root.resolve("exploration.json"))
        }
        HeadlessRuntime.loadCheckpoint(root.resolve("exploration.json"), root.resolve("reload-check")).use { runtime ->
            check(runtime.game.civilizations.count { it.isCityState } == 2)
            verifyPreparedObjectives(runtime)
        }
        println("CITY-STATE-FIXTURE: $root")
    }
}
