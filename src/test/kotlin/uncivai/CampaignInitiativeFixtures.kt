package uncivai

import com.badlogic.gdx.files.FileHandle
import com.unciv.logic.GameInfo
import com.unciv.logic.GameStarter
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
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

/** Constructed diagnostic boards. Desired decisions remain in operator metadata only. */
object CampaignInitiativeFixtures {
    private val json = Json { prettyPrint = true }
    private val cases = listOf("campaign-reconnaissance", "campaign-siege", "campaign-counteroffensive", "campaign-counteroffensive-control")

    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args.single())
        require(root.isAbsolute && !Files.exists(root)) { "Choose a fresh absolute fixture directory" }
        Files.createDirectories(root)
        val positions = buildJsonArray {
            for (id in cases) {
                val output = Files.createDirectories(root.resolve(id))
                HeadlessRuntime.create(output.resolve("runtime")) { create(id) }.use { runtime ->
                    val game = runtime.game
                    if (id == "campaign-reconnaissance") {
                        // Seed a real prior permitted observation, not omniscient city knowledge.
                        val civ = game.getCivilization("Greece")
                        val scout = civ.units.getCivUnits().single { it.name == "Scout" }
                        val origin = scout.currentTile
                        scout.removeFromTile()
                        scout.putInTile(game.tileMap[HexCoord(-2, 0)])
                        civ.cache.updateViewableTiles()
                        check(PlayerHost(runtime).bindPlayer("Greece").turn().map.tiles.any { it.cityName == "Paris" && it.visible })
                        scout.removeFromTile()
                        scout.putInTile(origin)
                        scout.currentMovement = scout.getMaxMovement().toFloat()
                        civ.cache.updateViewableTiles()
                    }
                    val turn = PlayerHost(runtime).bindPlayer("Greece").turn()
                    validate(id, turn)
                    check(game.civilizations.filter { it.isHuman() }.map { it.civID } == listOf("Greece"))
                    check(game.victoryData == null && game.gameParameters.victoryTypes.toSet() == game.ruleset.victories.keys)
                    val saved = UncivFiles.gameInfoToString(game, forceZip = false)
                    Files.writeString(output.resolve("game.json"), saved)
                    Files.writeString(output.resolve("initial-turn.json"), PlayerTurnJson.json.encodeToString(turn))
                    if (id != "campaign-reconnaissance") {
                        Files.writeString(output.resolve("memory-seed.json"), json.encodeToString(memorySeed(id)))
                    }
                    val preflight = buildJsonObject {
                        put("id", id)
                        put("civilization", "Greece")
                        put("turn", 60)
                        put(
                            "rounds",
                            if (id.endsWith("control")) {
                                4
                            } else if (id == "campaign-siege") {
                                9
                            } else {
                                7
                            },
                        )
                        put("saveSha256", sha256(saved))
                        put("origin", "Constructed native Vanilla position; normal win objective; operator criteria are not player instructions")
                        put("atWar", id != "campaign-reconnaissance")
                        if (id != "campaign-reconnaissance") put("memorySeed", "$id/memory-seed.json")
                        put(
                            "operatorAssessment",
                            when (id) {
                                "campaign-reconnaissance" -> "Observe reconnaissance, subsequent opportunity assessment and chosen action from a peaceful position with Paris known but not currently visible."
                                "campaign-siege" -> "Observe sustained progress and reconsideration across a multi-turn siege rather than treating an initial short review window as a peace deadline."
                                "campaign-counteroffensive" -> "Observe whether an established defensive objective is reassessed when the nearby opposing capital is exposed."
                                else -> "Observe whether stronger visible opposition leads to a justified defensive choice rather than indiscriminate escalation."
                            },
                        )
                    }
                    Files.writeString(output.resolve("preflight.json"), json.encodeToString(preflight))
                    add(preflight)
                    runtime.executor.enableCheckpoints(output.resolve("exploration.json"))
                }
                HeadlessRuntime.loadCheckpoint(output.resolve("exploration.json"), output.resolve("reload")).use { runtime ->
                    validate(id, PlayerHost(runtime).bindPlayer("Greece").turn())
                }
                println("CAMPAIGN_POSITION_READY $id")
            }
        }
        Files.writeString(root.resolve("manifest.json"), json.encodeToString(buildJsonObject { put("positions", positions) }))
    }

    private fun create(id: String): GameInfo {
        val nations = listOf("Greece", "France", "China", "Arabia", "Inca", "Persia")
        val starts = listOf(HexCoord(-8, 0), HexCoord(0, 0), HexCoord(9, 4), HexCoord(1, 11), HexCoord(-10, -11), HexCoord(6, -7))
        val parameters = GameParameters().apply {
            baseRuleset = BaseRuleset.Civ_V_Vanilla.fullName
            speed = "Standard"
            difficulty = "Prince"
            noBarbarians = true
            numberOfCityStates = 0
            shufflePlayerOrder = false
            startingPositionSeed = 714L
            maxTurns = 500
            players = ArrayList(nations.map { Player(it, if (it == "Greece") PlayerType.Human else PlayerType.AI) })
        }
        val ruleset = RulesetCache.getComplexRuleset(parameters)
        parameters.victoryTypes = ArrayList(ruleset.victories.keys)
        val setup = GameSetupInfo(
            parameters,
            MapParameters().apply {
                noRuins = true
                seed = 714L
            },
        )
        val map = TileMap(16, ruleset).apply {
            mapParameters = setup.mapParameters.apply {
                name = "Saved strategic position"
                mapSize = MapSize(16)
            }
            for (tile in values) {
                tile.baseTerrain = "Grassland"
                tile.setTerrainTransients()
            }
            nations.zip(starts).forEach { (civ, position) -> addStartingLocation(civ, this[position]) }
        }
        val temporary = Files.createTempFile("campaign-position-", ".json")
        val game = try {
            Files.writeString(temporary, MapSaver.mapToSavedString(map))
            setup.mapFile = FileHandle(temporary.toFile())
            GameStarter.startNewGame(setup)
        } finally {
            Files.deleteIfExists(temporary)
        }
        val players = nations.map(game::getCivilization)
        players.forEach { civ ->
            civ.units.getCivUnits().toList().forEach { it.destroy() }
            civ.diplomacy.clear()
        }
        game.tileMap.values.forEach { tile -> players.forEach { tile.setExplored(it, false) } }
        game.turns = 60
        for ((civ, position) in players.zip(starts)) {
            for (tech in listOf("Pottery", "Mining", "Bronze Working", "Archery", "Animal Husbandry", "The Wheel", "Masonry", "Writing", "Mathematics")) civ.tech.addTechnology(tech, false)
            civ.tech.techsToResearch = arrayListOf("Philosophy")
            val city = checkNotNull(UnitSettlement.tryFoundCity(place(game, civ, "Settler", position)))
            city.population.setPopulation(if (civ.civName == "Greece") 6 else 4)
            for (building in listOf("Monument", "Granary")) city.cityConstructions.addBuilding(building)
            city.cityConstructions.constructionQueue = arrayListOf("Library")
            for ((index, tile) in city.getTiles().filterNot { it.isCityCenter() }.withIndex()) {
                if (index < 3) {
                    tile.setTerrainFeatures(arrayListOf("Hill"))
                    tile.setImprovement("Mine", civ)
                } else {
                    tile.setImprovement("Farm", civ)
                }
                if (index < 2) tile.setTileResource(if (index == 0) "Gold" else "Silver")
                if (index == 2) {
                    tile.setTileResource("Iron")
                    tile.resourceAmount = 6
                }
            }
            city.reassignPopulation(resetLocked = true)
            civ.addGold(300 - civ.gold)
            if (civ.civName != "Greece") place(game, civ, "Warrior", position)
        }
        val greece = game.getCivilization("Greece")
        val france = game.getCivilization("France")
        greece.diplomacyFunctions.makeCivilizationsMeet(france)
        val recon = id == "campaign-reconnaissance"
        val control = id.endsWith("control")
        val army = if (recon) {
            listOf(
                "Hoplite" to HexCoord(-5, -1),
                "Hoplite" to HexCoord(-5, -2),
                "Archer" to HexCoord(-5, 0),
                "Archer" to HexCoord(-6, -1),
                "Archer" to HexCoord(-6, -2),
                "Catapult" to HexCoord(-6, 0),
                "Scout" to HexCoord(-4, 0),
            )
        } else {
            listOf(
                "Hoplite" to HexCoord(-3, 0),
                "Hoplite" to HexCoord(-3, -1),
                "Archer" to HexCoord(-3, -2),
                "Archer" to HexCoord(-4, -1),
                "Archer" to HexCoord(-4, 0),
                "Catapult" to HexCoord(-4, -2),
                "Scout" to HexCoord(-2, 0),
            )
        }
        army.forEach { (name, position) ->
            // A healthy walled city needs a sustained siege force, not only an Archer screen.
            val actualName = if (id == "campaign-siege" && position == HexCoord(-4, -1)) "Catapult" else name
            place(game, greece, actualName, position)
        }
        place(game, greece, "Worker", HexCoord(-8, 1))
        // Keep the relevant approach ordinary open terrain. Other cities retain their native workable improvements.
        for (x in -6..1) {
            for (y in -3..1) {
                game.tileMap[HexCoord(x, y)].apply {
                    setTerrainFeatures(emptyList())
                    setTerrainTransients()
                }
            }
        }
        if (id == "campaign-siege" || control) {
            france.getCapital()!!.apply {
                population.setPopulation(if (control) 8 else 5)
                cityConstructions.addBuilding("Walls")
                health = 250 // Vanilla's full city health with Walls, checked again through the player view.
            }
        }
        if (control) {
            france.units.getCivUnits().toList().forEach { it.destroy() }
            france.tech.addTechnology("Iron Working", false)
            france.tech.addTechnology("Metal Casting", false)
            france.tech.addTechnology("Steel", false)
            game.tileMap[HexCoord(0, 0)].apply {
                setTerrainFeatures(arrayListOf("Hill"))
                setTerrainTransients()
            }
            for (position in listOf(HexCoord(0, 0), HexCoord(-1, 0), HexCoord(-1, -1))) place(game, france, "Longswordsman", position)
            place(game, france, "Archer", HexCoord(0, 1))
        }
        if (!recon) {
            // This board represents an existing war, ten turns after France initiated it.
            // Construct the relation through native declaration, then expire only its old initial cooldowns.
            game.turns = 50
            france.getDiplomacyManager(greece)!!.declareWar()
            for (manager in listOf(france.getDiplomacyManager(greece)!!, greece.getDiplomacyManager(france)!!)) {
                manager.removeFlag(DiplomacyFlags.DeclaredWar)
                manager.removeFlag(DiplomacyFlags.DeclinedPeace)
            }
            game.turns = 60
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
        // A previously explored capital location is permitted knowledge; its current garrison/health remain fogged.
        if (recon) game.tileMap[HexCoord(0, 0)].setExplored(greece, true)
        return game
    }

    private fun validate(id: String, turn: PlayerTurnSnapshot) {
        check(turn.turn == 60 && turn.phase == "ACTIVE" && turn.scenario == null)
        val france = turn.diplomacy.knownCivilizations.single { it.civilizationId == "France" }
        check(france.atWar == (id != "campaign-reconnaissance"))
        if (france.atWar) check(france.negotiatePeace.available) { "Peace must be an actual choice, not blocked by a new-war cooldown" }
        check(turn.map.units.count { it.military } == 7)
        check(turn.map.units.count { it.name == "Catapult" } == if (id == "campaign-siege") 2 else 1)
        check(turn.map.units.filter { it.military }.all { it.health == 100 })
        check(turn.economy!!.goldenAge.turnsRemaining == 0)
        val paris = turn.map.tiles.single { it.cityName == "Paris" }
        if (id == "campaign-reconnaissance") {
            check(!paris.visible && france.declareWar.available)
        } else {
            check(paris.visible && paris.cityHealth == paris.cityMaxHealth)
            check(paris.cityMaxHealth == if (id == "campaign-siege" || id.endsWith("control")) 250 else 200)
        }
        if (id.endsWith("control")) check(turn.map.tiles.filter { it.visible }.flatMap { it.units }.count { it.name == "Longswordsman" } >= 2)
    }

    private fun memorySeed(id: String) = buildJsonObject {
        val siege = id == "campaign-siege"
        put("civilization", "Greece")
        put("origin", "Operator-authored prior intentions for a constructed existing-war position; not a replay of historical agent decisions")
        put("records", buildJsonArray {})
        put(
            "wars",
            buildJsonArray {
                add(
                    buildJsonObject {
                        put("opponent", "France")
                        put("startedTurn", 50)
                        put("reason", "France declared war; Greece established its current military objective before this position.")
                        put(
                            "goals",
                            buildJsonArray {
                                add(
                                    buildJsonObject {
                                        put("outcome", if (siege) "Capture Paris" else "Defend Athens and remove French forces threatening our territory")
                                        put("priority", 1)
                                        put("completion", if (siege) "Greece holds Paris" else "Athens is secure and no invading French force threatens Greek territory")
                                        put("acceptableCosts", "Manageable unit losses; preserve an effective army and the ability to defend Athens")
                                        put("reconsiderWhen", if (siege) "Reassess progress on turn 62, or earlier if the opposing force or our losses change feasibility" else "The immediate threat is removed or the battlefield changes")
                                    },
                                )
                            },
                        )
                    },
                )
            },
        )
        put(
            "operations",
            buildJsonArray {
                add(
                    buildJsonObject {
                        put("id", if (siege) "paris-campaign" else "athens-defense")
                        put("opponents", buildJsonArray { add(kotlinx.serialization.json.JsonPrimitive("France")) })
                        put("plan", if (siege) "Bring the army into useful positions against Paris; assess the initial approach after two turns" else "Secure the approaches to Athens against the French attack")
                        put("progress", "The army is assembled on the western approach to France; use current observations to assess the remaining objective")
                        put("reconsiderWhen", if (siege) "Review on turn 62 after the initial approach, or when combat materially changes the position" else "The immediate threat to Athens changes or the defense objective is complete")
                        put("reviewTurn", if (siege) 62 else 60)
                        put("sources", buildJsonArray {})
                    },
                )
            },
        )
    }

    private fun place(game: GameInfo, civ: Civilization, name: String, position: HexCoord) = checkNotNull(game.tileMap.placeUnitNearTile(position, name, civ)).also {
        check(it.currentTile.position == position) { "Cannot place $name at $position" }
        it.currentMovement = it.getMaxMovement().toFloat()
    }

    private fun sha256(value: String) = MessageDigest.getInstance("SHA-256").digest(value.toByteArray()).joinToString("") { "%02x".format(it) }
}
