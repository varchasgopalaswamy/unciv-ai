package uncivai

import com.badlogic.gdx.files.FileHandle
import com.unciv.logic.GameInfo
import com.unciv.logic.GameStarter
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.files.MapSaver
import com.unciv.logic.files.UncivFiles
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.MapParameters
import com.unciv.logic.map.MapSize
import com.unciv.logic.map.TileMap
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.models.metadata.BaseRuleset
import com.unciv.models.metadata.GameParameters
import com.unciv.models.metadata.GameSetupInfo
import com.unciv.models.metadata.Player
import com.unciv.models.ruleset.RulesetCache
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import java.nio.file.Files
import java.nio.file.Path
import java.security.MessageDigest

/** Operator-authored positions, not instructions: subsequent play uses ordinary native rules. */
object DominationPositionFixtures {
    private val json = Json { prettyPrint = true }
    private val cases = listOf(
        "greece-build-up",
        "persia-ready",
        "persia-strong-defense",
        "persia-opportunity",
        "greece-damaged-capital",
        "greece-hill-fortress",
    )

    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args.single())
        require(root.isAbsolute && !Files.exists(root)) { "Choose a fresh absolute fixture directory" }
        Files.createDirectories(root)
        val positions = buildJsonArray {
            for (id in cases) {
                val civilization = if (id.startsWith("greece-")) "Greece" else "Persia"
                val output = root.resolve(id)
                Files.createDirectories(output)
                HeadlessRuntime.create(output.resolve("runtime")) { create(civilization, id) }.use { runtime ->
                    val game = runtime.game
                    val turn = PlayerHost(runtime).bindPlayer(civilization).turn()
                    check(game.currentPlayer == civilization && game.turns == 40)
                    check(game.civilizations.filter { it.isHuman() }.map { it.civID } == listOf(civilization))
                    check(game.victoryData == null && game.gameParameters.victoryTypes.toSet() == game.ruleset.victories.keys)
                    check(turn.scenario == null)
                    check(turn.diplomacy.knownCivilizations.single { it.civilizationId == "France" }.let { !it.atWar && it.declareWar.available })
                    check(turn.map.tiles.any { it.cityName == "Paris" && it.visible }) { "Target must be currently visible" }
                    check(turn.map.units.count { it.military } == if (id == "greece-build-up") 3 else 5)
                    validateVariation(id, turn)
                    val savedGame = UncivFiles.gameInfoToString(game, forceZip = false)
                    Files.writeString(output.resolve("game.json"), savedGame)
                    Files.writeString(output.resolve("initial-turn.json"), PlayerTurnJson.json.encodeToString(turn))
                    val preflight = buildJsonObject {
                        put("id", id)
                        put("civilization", civilization)
                        put("turn", game.turns)
                        put("rounds", 3)
                        put("saveSha256", sha256(savedGame))
                        put("origin", "Constructed native Vanilla position; no historical private memory or social ledger")
                        put("reviewTiming", "Play turn 40, review the round-40 boundary, then play turns 41 and 42")
                    }
                    Files.writeString(output.resolve("preflight.json"), json.encodeToString(preflight))
                    add(preflight)
                    runtime.executor.enableCheckpoints(output.resolve("exploration.json"))
                }
                HeadlessRuntime.loadCheckpoint(output.resolve("exploration.json"), output.resolve("reload")).use { runtime ->
                    val turn = PlayerHost(runtime).bindPlayer(civilization).turn()
                    check(turn.phase == "ACTIVE" && turn.map.tiles.any { it.cityName == "Paris" && it.visible })
                    check(turn.diplomacy.knownCivilizations.single { it.civilizationId == "France" }.declareWar.available)
                    validateVariation(id, turn)
                }
                println("DOMINATION_POSITION_READY $id")
            }
        }
        Files.writeString(root.resolve("manifest.json"), json.encodeToString(buildJsonObject { put("positions", positions) }))
    }

    private fun create(subject: String, id: String): GameInfo {
        val nations = listOf(subject, "France", "China", "Arabia", "Inca", if (subject == "Greece") "Persia" else "Greece")
        val starts = listOf(HexCoord(-6, 0), HexCoord(0, 0), HexCoord(8, 3), HexCoord(0, 10), HexCoord(-9, -10), HexCoord(4, -7))
        val parameters = GameParameters().apply {
            baseRuleset = BaseRuleset.Civ_V_Vanilla.fullName
            speed = "Standard"
            difficulty = "Prince"
            noBarbarians = true
            numberOfCityStates = 0
            shufflePlayerOrder = false
            startingPositionSeed = 412L
            maxTurns = 500
            players = ArrayList(nations.map { Player(it, if (it == subject) PlayerType.Human else PlayerType.AI) })
        }
        val ruleset = RulesetCache.getComplexRuleset(parameters)
        parameters.victoryTypes = ArrayList(ruleset.victories.keys)
        val setup = GameSetupInfo(
            parameters,
            MapParameters().apply {
                noRuins = true
                seed = 412L
            },
        )
        val map = TileMap(14, ruleset).apply {
            mapParameters = setup.mapParameters.apply {
                name = "Saved strategic position"
                mapSize = MapSize(14)
            }
            for (tile in values) {
                tile.baseTerrain = "Grassland"
                tile.setTerrainTransients()
            }
            nations.zip(starts).forEach { (civ, position) -> addStartingLocation(civ, this[position]) }
        }
        val temporary = Files.createTempFile("strategic-position-", ".json")
        val game = try {
            Files.writeString(temporary, MapSaver.mapToSavedString(map))
            setup.mapFile = FileHandle(temporary.toFile())
            GameStarter.startNewGame(setup)
        } finally {
            Files.deleteIfExists(temporary)
        }
        val players = nations.map(game::getCivilization)
        players.forEach { player ->
            player.units.getCivUnits().toList().forEach { it.destroy() }
            player.diplomacy.clear()
        }
        game.tileMap.values.forEach { tile -> players.forEach { tile.setExplored(it, false) } }
        game.turns = 40
        for ((player, position) in players.zip(starts)) {
            for (tech in listOf("Pottery", "Mining", "Bronze Working", "Archery", "Animal Husbandry", "The Wheel", "Masonry", "Writing")) player.tech.addTechnology(tech, false)
            player.tech.techsToResearch = arrayListOf("Philosophy")
            val settler = place(game, player, "Settler", position)
            val city = checkNotNull(UnitSettlement.tryFoundCity(settler))
            city.population.setPopulation(if (player.civName == subject) 5 else 3)
            city.cityConstructions.addBuilding("Monument")
            city.cityConstructions.addBuilding("Granary")
            city.cityConstructions.constructionQueue = arrayListOf("Library")
            // Comparable basic economies; the subject has room to finance an early campaign.
            for ((index, tile) in city.getTiles().filterNot { it.isCityCenter() }.withIndex()) {
                if (index < 3) {
                    tile.setTerrainFeatures(arrayListOf("Hill"))
                    tile.setImprovement("Mine", player)
                } else {
                    tile.setImprovement("Farm", player)
                }
                if (index < 2) tile.setTileResource(if (index == 0) "Gold" else "Silver")
            }
            city.reassignPopulation(resetLocked = true)
            player.addGold(250 - player.gold)
            if (player.civName != subject) place(game, player, "Warrior", position)
        }
        val player = game.getCivilization(subject)
        val france = game.getCivilization("France")
        player.diplomacyFunctions.makeCivilizationsMeet(france)
        if (id == "greece-build-up") {
            place(game, player, "Hoplite", HexCoord(-2, 0))
            place(game, player, "Archer", HexCoord(-3, -1))
            place(game, player, "Warrior", HexCoord(-5, 0))
        } else if (subject == "Greece") {
            for (position in listOf(HexCoord(-2, 0), HexCoord(-2, -1), HexCoord(-3, -2))) place(game, player, "Hoplite", position)
            for (position in listOf(HexCoord(-2, -2), HexCoord(-3, -1))) place(game, player, "Archer", position)
        } else {
            player.goldenAges.enterGoldenAge()
            for (position in listOf(HexCoord(-2, 0), HexCoord(-2, -1), HexCoord(-3, -2))) place(game, player, "Persian Immortal", position)
            for (position in listOf(HexCoord(-3, -1), HexCoord(-4, -1))) place(game, player, "Archer", position)
        }
        place(game, player, "Worker", HexCoord(-6, 1))
        if (id == "persia-opportunity") {
            // Prepared damage from earlier fighting. Native healing and all
            // later diplomatic/combat actions proceed normally after startup.
            france.getCapital()!!.health = 50
            france.units.getCivUnits().single().health = 20
        }
        if (id == "greece-damaged-capital") {
            france.getCapital()!!.health = 35
            france.units.getCivUnits().single().health = 30
            // This army has ordinary two-point movement rather than Persia's
            // Golden Age movement. Keep the short western approaches open.
            for (position in listOf(HexCoord(-1, 0), HexCoord(-1, -1))) {
                game.tileMap[position].apply {
                    setTerrainFeatures(emptyList())
                    setImprovement("Farm", france)
                    setTerrainTransients()
                }
            }
        }
        if (id == "persia-strong-defense" || id == "greece-hill-fortress") {
            france.units.getCivUnits().toList().forEach { it.destroy() }
            france.tech.addTechnology("Iron Working", false)
            france.getCapital()!!.apply {
                population.setPopulation(6)
                cityConstructions.addBuilding("Walls")
                val iron = getTiles().first { it.improvement == "Mine" }
                iron.setTileResource("Iron")
                iron.resourceAmount = 6
            }
            for (position in listOf(HexCoord(0, 0), HexCoord(-1, 0), HexCoord(0, 1), HexCoord(-1, -1))) place(game, france, "Swordsman", position)
            place(game, france, "Archer", HexCoord(1, 1))
        }
        if (id == "greece-hill-fortress") {
            game.tileMap[HexCoord(0, 0)].apply {
                setTerrainFeatures(arrayListOf("Hill"))
                setTerrainTransients()
            }
            game.tileMap[HexCoord(-1, -1)].apply {
                setTerrainFeatures(arrayListOf("Hill", "Forest"))
                setImprovement(null, france)
                setTerrainTransients()
            }
            // Leave a sightline to the city and its defenders. The agent can
            // assess the fortification rather than receiving an unseen target.
            game.tileMap[HexCoord(-1, 0)].apply {
                setTerrainFeatures(emptyList())
                setImprovement("Farm", france)
                setTerrainTransients()
            }
        }
        players.forEach { civ ->
            civ.popupAlerts.clear()
            civ.notifications.clear()
            civ.units.getCivUnits().forEach { it.updateVisibleTiles(updateCivViewableTiles = false) }
            civ.cities.forEach {
                it.reassignPopulation(resetLocked = true)
                it.cityStats.update()
            }
            civ.cache.updateCivResources()
            civ.cache.updateViewableTiles()
            civ.updateStatsForNextTurn()
        }
        return game
    }

    private fun validateVariation(id: String, turn: PlayerTurnSnapshot) {
        if (id !in setOf("greece-damaged-capital", "greece-hill-fortress")) return
        check(turn.economy!!.goldenAge.turnsRemaining == 0)
        check(turn.map.units.count { it.name == "Hoplite" } == 3)
        check(turn.map.units.count { it.name == "Archer" } == 2)
        check(turn.map.units.filter { it.military }.all { it.health == 100 && it.movement == "2/2" })
        val capital = turn.map.tiles.single { it.cityName == "Paris" && it.visible }
        val visibleDefenders = turn.map.tiles.filter { it.visible }.flatMap { it.units }.filter { it.civilization == "France" && it.military }
        if (id == "greece-damaged-capital") {
            check(capital.cityHealth == 35 && capital.cityMaxHealth == 200)
            check(visibleDefenders.single().let { it.name == "Warrior" && it.health == 30 })
        } else {
            check(capital.cityHealth == capital.cityMaxHealth && capital.cityMaxHealth == 250)
            check("Hill" in capital.features.orEmpty())
            check(visibleDefenders.count { it.name == "Swordsman" } >= 3)
        }
    }

    private fun place(game: GameInfo, player: Civilization, name: String, position: HexCoord) = checkNotNull(game.tileMap.placeUnitNearTile(position, name, player)).also {
        check(it.currentTile.position == position) { "Cannot place $name at $position" }
        it.currentMovement = it.getMaxMovement().toFloat()
    }

    private fun sha256(text: String) = MessageDigest.getInstance("SHA-256").digest(text.toByteArray()).joinToString("") { "%02x".format(it) }
}
