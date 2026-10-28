package uncivai

import com.unciv.Constants
import com.unciv.UncivGame
import com.unciv.logic.GameInfo
import com.unciv.logic.files.UncivFiles
import com.unciv.logic.map.MapSize
import com.unciv.logic.map.MapType
import com.unciv.logic.map.mapgenerator.mapregions.getTileFertility
import com.unciv.logic.map.tile.Tile
import com.unciv.models.ruleset.tile.ResourceType
import com.unciv.models.stats.Stats
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardOpenOption
import java.security.MessageDigest
import kotlin.math.max
import kotlin.random.Random

/** Trusted, offline seed search. Scores inspect the full native map and never enter player observations. */
object MapBalanceSearch {
    private val roster = listOf("Inca", "France", "Greece", "Arabia", "China", "Persia")
    private val json = Json { prettyPrint = true }
    private val compactJson = Json

    @Serializable
    data class Start(
        val civilization: String,
        val x: Int,
        val y: Int,
        val continent: Int,
        val nativeScore: Double,
        val bestSixFood: Double,
        val bestSixProduction: Double,
        val foodTiles2: Int,
        val productionTiles2: Int,
        val workableLand3: Int,
        val freshWater: Boolean,
        val coastal: Boolean,
        val luxuries3: List<String>,
        val earlyStrategics5: List<String>,
        val nearestMajorLandDistance: Int,
        val nearbyCityStates8: Int,
        val naturalWonders3: List<String>,
        val ruinsWithin8: Int,
        val nearestRuinLandDistance: Int,
        val closestRuinShare10: Double,
        val strategics8: List<String>,
        val expansionLand: Double,
        val expansionFertility: Double,
    )

    @Serializable
    data class SeedPair(val mapType: String, val mapSeed: Long, val startingPositionSeed: Long)

    @Serializable
    data class Wonder(
        val name: String,
        val aerialDistanceByCivilization: Map<String, Int>,
        val landAccessByCivilization: Map<String, Int?>,
    )

    @Serializable
    data class Candidate(
        val mapType: String,
        val mapSeed: Long,
        val startingPositionSeed: Long,
        val loss: Double,
        val validGeography: Boolean,
        val wonderSafe: Boolean,
        val ruinSafe: Boolean,
        val comparable: Boolean,
        val wonders: List<Wonder>,
        val continentSplit: List<Int>,
        val nativeScoreSpread: Double,
        val foodSpread: Double,
        val productionSpread: Double,
        val expansionSpread: Double,
        val fertilitySpread: Double,
        val starts: List<Start>,
    )

    @Serializable
    data class MapTile(
        val x: Int,
        val y: Int,
        val terrain: String,
        val features: List<String>,
        val resource: String?,
        val wonder: String?,
        val river: Boolean,
        val continent: Int,
        val improvement: String?,
    )

    @JvmStatic
    fun main(args: Array<String>) {
        require(args.size in 2..7) { "output-directory candidate-count [first-map-seed] [placement-stream-seed] [map-type|both] [fixed-placement-seed] [seed-pairs-json]" }
        val root = Path.of(args[0]).toAbsolutePath()
        val count = args[1].toInt().also { require(it > 0) }
        val firstMapSeed = args.getOrNull(2)?.toLong() ?: 1L
        val streamSeed = args.getOrNull(3)?.toLong() ?: 20261027L
        val types = args.getOrNull(4)?.takeUnless { it == "both" }?.let(::listOf) ?: listOf(MapType.pangaea, MapType.twoContinents)
        val fixedPlacementSeed = args.getOrNull(5)?.toLong()
        val seedPairs = args.getOrNull(6)?.let { json.decodeFromString<List<SeedPair>>(Files.readString(Path.of(it))) }
        require(types.all { it == MapType.pangaea || it == MapType.twoContinents })
        Files.createDirectories(root)
        Files.writeString(root.resolve("settings.json"), json.encodeToString(obj(
            "civilizations" to roster, "mapSize" to "Medium", "cityStates" to 12,
            "difficulty" to "Prince", "speed" to "Standard", "startingEra" to "Ancient era",
            "baseRuleset" to "Civ V - Vanilla", "candidateCount" to (seedPairs?.size ?: types.size * count),
            "firstMapSeed" to firstMapSeed, "placementStreamSeed" to streamSeed,
            "fixedStartingPositionSeed" to fixedPlacementSeed, "seedPairsFile" to args.getOrNull(6),
            "engineRevision" to System.getProperty("uncivai.engineRevision"),
            "engineWorkingTree" to "Uses the selected checkout, including local changes; record its diff for exact reproduction.",
            "method" to "Native start quality; independent top-six food/production potentials within radius 2; resources within radius 3/5/8; walkable expansion within 10 steps; neighbor spacing; city-state access; fresh water; natural-wonder access and ancient-ruin races. Heuristic comparison, not equal win probabilities.",
        )))
        HeadlessRuntime.createVanillaGame(root.resolve("runtime"), cityStates = 12, agentPlayers = roster).use {
            for (type in types) {
                val random = Random(streamSeed)
                var best: Candidate? = null
                val pairs = seedPairs?.filter { it.mapType == type }
                Files.newBufferedWriter(root.resolve("${slug(type)}-candidates.jsonl")).use { log ->
                    repeat(pairs?.size ?: count) { index ->
                        val mapSeed = pairs?.get(index)?.mapSeed ?: if (index == 0 && firstMapSeed == 1L) 19238721L else firstMapSeed + index
                        val placementSeed = pairs?.get(index)?.startingPositionSeed ?: fixedPlacementSeed ?: if (index == 0 && firstMapSeed == 1L) 20261013L else random.nextLong(1L, Long.MAX_VALUE)
                        fun reject(reason: String) {
                            Files.writeString(root.resolve("${slug(type)}-rejected.jsonl"), compactJson.encodeToString(obj(
                                "mapSeed" to mapSeed, "startingPositionSeed" to placementSeed, "reason" to reason,
                            )) + "\n", StandardOpenOption.CREATE, StandardOpenOption.APPEND)
                        }
                        val game = try {
                            create(mapSeed, placementSeed, type)
                        } catch (failure: Exception) {
                            reject("${failure.javaClass.simpleName}: ${failure.message}")
                            return@repeat
                        }
                        if (game.civilizations.count { it.isCityState } != 12 || game.civilizations
                                .filter { !it.isBarbarian && !it.isSpectator() }
                                .any { civ -> civ.units.getCivUnits().count { it.baseUnit.isCityFounder() } != 1 }) {
                            reject("Native generation did not place exactly one starting settler for each major and all twelve city-states")
                            return@repeat
                        }
                        val candidate = evaluate(game)
                        log.appendLine(compactJson.encodeToString(candidate))
                        if (best == null || candidate.loss < best!!.loss) {
                            best = candidate
                            export(root, game, candidate)
                            println("BEST $type map=$mapSeed start=$placementSeed loss=${candidate.loss} scores=${candidate.starts.map { it.nativeScore }} expansion=${candidate.starts.map { it.expansionLand }} split=${candidate.continentSplit}")
                        }
                        if ((index + 1) % 50 == 0) {
                            log.flush()
                            println("PROGRESS $type ${index + 1}/$count best=${best!!.loss}")
                        }
                    }
                }
                println("SELECTED " + compactJson.encodeToString(best))
                best?.let { verifyControllers(root, it) }
            }
        }
    }

    private fun create(mapSeed: Long, placementSeed: Long, type: String, native: String? = null): GameInfo =
        VanillaGameSetup.create(
            mapSeed, cityStates = 12, builtInAI = listOfNotNull(native),
            agentPlayers = if (native == null) roster else roster.filter { it != native }.reversed(), size = MapSize.Medium,
            options = VanillaOptions(mapType = type, speed = "Standard", startingPositionSeed = placementSeed),
        ).also { UncivGame.Current.gameInfo = it }

    private fun landDistances(start: Tile): Map<Tile, Int> {
        val distances = hashMapOf(start to 0)
        val queue = ArrayDeque<Tile>().apply { add(start) }
        while (queue.isNotEmpty()) {
            val tile = queue.removeFirst()
            for (neighbor in tile.neighbors) {
                if (!neighbor.isLand || neighbor.isImpassible() || neighbor in distances) continue
                distances[neighbor] = distances.getValue(tile) + 1
                queue.add(neighbor)
            }
        }
        return distances
    }

    internal fun evaluate(game: GameInfo, majorStartTiles: Map<String, Tile>? = null, minorStartTiles: List<Tile>? = null): Candidate {
        val majors = roster.map(game::getCivilization)
        fun start(civ: com.unciv.logic.civilization.Civilization) =
            civ.units.getCivUnits().single { it.baseUnit.isCityFounder() }.getTile()
        val centers = majors.map { majorStartTiles?.getValue(it.civID) ?: start(it) }
        val minorCenters = minorStartTiles ?: game.civilizations.filter { it.isCityState }.map(::start)
        val distances = centers.map(::landDistances)
        val ruins = game.tileMap.values.filter { it.tileImprovement?.isAncientRuinsEquivalent() == true }
        val ruinShares = DoubleArray(majors.size)
        for (ruin in ruins) {
            val ds = distances.map { it[ruin] ?: 999 }
            val closest = ds.min()
            if (closest > 10) continue
            val owners = ds.indices.filter { ds[it] == closest }
            for (owner in owners) ruinShares[owner] += 1.0 / owners.size
        }
        val wonders = game.tileMap.values.filter { it.isNaturalWonder() }.groupBy { it.naturalWonder!! }.map { (name, tiles) ->
            Wonder(name,
                roster.indices.associate { index -> roster[index] to tiles.minOf { centers[index].aerialDistanceTo(it) } },
                roster.indices.associate { index -> roster[index] to tiles.flatMap { it.neighbors.toList() }
                    .mapNotNull { distances[index][it] }.minOrNull() },
            )
        }
        // Avoid first-discovery gold and permanent healing swings entirely. Also reject close
        // capital wonders and a clear, early overland monopoly on an ordinary natural wonder.
        val wonderSafe = wonders.none { wonder ->
            val access = wonder.landAccessByCivilization.values.filterNotNull().sorted()
            wonder.name in listOf("El Dorado", "Fountain of Youth") ||
                wonder.aerialDistanceByCivilization.values.min() < 5 ||
                (access.firstOrNull()?.let { it <= 8 } == true &&
                    (access.getOrNull(1) ?: 999) - access.first() >= 4)
        }
        val land = game.tileMap.values.filter { it.isLand && !it.isImpassible() }
        val territory = DoubleArray(majors.size)
        val fertility = DoubleArray(majors.size)
        // Nearby expansion room: walkable tiles within ten land steps, shared at equal-distance ties.
        // Exclude prospective city sites within the native minimum distance of city-state starts.
        val cityDistance = game.ruleset.modOptions.constants.minimalCityDistance
        for (tile in land) {
            if (minorCenters.any { it.aerialDistanceTo(tile) <= cityDistance }) continue
            val ds = distances.map { it[tile] ?: Int.MAX_VALUE }
            val closest = ds.min()
            if (closest > 10) continue
            val owners = ds.indices.filter { ds[it] == closest }
            for (owner in owners) {
                territory[owner] += 1.0 / owners.size
                fertility[owner] += max(0, tile.getTileFertility(true)).toDouble() / owners.size
            }
        }
        val starts = majors.mapIndexed { index, civ ->
            val center = centers[index]
            val tiles2 = center.getTilesInDistance(2).filter { it != center && !it.isImpassible() }.toList()
            val yields = tiles2.map { it.stats.getTileStats(civ) }
            val tiles3 = center.getTilesInDistance(3).toList()
            val resources5 = center.getTilesInDistance(5).mapNotNull { it.tileResource }.toList()
            Start(
                civ.civID, center.position.x, center.position.y, center.getContinent(),
                center.stats.getTileStartScore(Stats.DefaultCityCenterMinimum).toDouble(),
                yields.map { it.food.toDouble() }.sortedDescending().take(6).sum(),
                yields.map { it.production.toDouble() }.sortedDescending().take(6).sum(),
                yields.count { it.food >= 2 }, yields.count { it.production >= 2 },
                tiles3.count { it.isLand && !it.isImpassible() },
                center.isAdjacentTo(Constants.freshWater), center.isAdjacentToCoast(),
                tiles3.mapNotNull { it.tileResource }.filter { it.resourceType == ResourceType.Luxury }.map { it.name }.distinct().sorted(),
                resources5.filter { it.name in listOf("Iron", "Horses") }.map { it.name }.distinct().sorted(),
                centers.indices.filter { it != index }.mapNotNull { distances[index][centers[it]] }.minOrNull() ?: 999,
                minorCenters.count { (distances[index][it] ?: 999) <= 8 },
                tiles3.mapNotNull { it.naturalWonder }.distinct().sorted(),
                ruins.count { (distances[index][it] ?: 999) <= 8 },
                ruins.minOfOrNull { distances[index][it] ?: 999 } ?: 999,
                ruinShares[index],
                center.getTilesInDistance(8).mapNotNull { it.tileResource }
                    .filter { it.resourceType == ResourceType.Strategic }.map { it.name }.distinct().sorted().toList(),
                territory[index], fertility[index],
            )
        }
        val split = centers.groupingBy { it.getContinent() }.eachCount().values.sorted()
        val validGeography = if (game.tileMap.mapParameters.type == MapType.pangaea) split == listOf(6) else split == listOf(3, 3)
        val ruinSafe = starts.all { it.nearestRuinLandDistance >= 3 && it.ruinsWithin8 >= 1 && it.closestRuinShare10 >= 0.5 } &&
            starts.maxOf { it.ruinsWithin8 } - starts.minOf { it.ruinsWithin8 } <= 2 &&
            starts.maxOf { it.closestRuinShare10 } - starts.minOf { it.closestRuinShare10 } <= 1.5
        val scoreSpread = spread(starts.map { it.nativeScore })
        val foodSpread = spread(starts.map { it.bestSixFood })
        val prodSpread = spread(starts.map { it.bestSixProduction })
        val landSpread = spread(starts.map { it.expansionLand })
        val fertilitySpread = spread(starts.map { it.expansionFertility })
        val worstSpread = maxOf(scoreSpread, foodSpread, prodSpread, landSpread, fertilitySpread)
        val comparable = validGeography && wonderSafe && ruinSafe && worstSpread <= 0.35 && scoreSpread <= 0.25 &&
            starts.all { it.nativeScore >= 55 && it.luxuries3.size >= 2 && it.earlyStrategics5.isNotEmpty() }
        val loss = worstSpread * 0.5 + scoreSpread * 0.30 + foodSpread * 0.15 + prodSpread * 0.10 +
            landSpread * 0.20 + fertilitySpread * 0.15 +
            spread(starts.map { it.nearestMajorLandDistance.toDouble() }) * 0.05 +
            spread(starts.map { it.nearbyCityStates8.toDouble() }) * 0.025 +
            spread(starts.map { it.luxuries3.size.toDouble() }) * 0.025 +
            starts.count { !it.freshWater } * 0.05 +
            starts.sumOf { max(0.0, 60.0 - it.nativeScore) / 60.0 * 0.15 } +
            starts.count { it.luxuries3.isEmpty() } * 0.5 +
            starts.count { it.earlyStrategics5.isEmpty() } * 0.08 +
            starts.count { it.naturalWonders3.isNotEmpty() } * 0.15 +
            starts.sumOf { max(0.0, 35.0 - it.expansionLand) / 35.0 * 0.2 } +
            (if (wonderSafe) 0.0 else 2.0) +
            (if (ruinSafe) 0.0 else 2.0) +
            (if (comparable) 0.0 else 5.0) +
            if (validGeography) 0.0 else 10.0
        return Candidate(game.tileMap.mapParameters.type, game.tileMap.mapParameters.seed,
            checkNotNull(game.gameParameters.startingPositionSeed), loss, validGeography, wonderSafe, ruinSafe, comparable, wonders, split,
            scoreSpread, foodSpread, prodSpread, landSpread, fertilitySpread, starts)
    }

    private fun spread(values: List<Double>): Double =
        (values.max() - values.min()) / max(1.0, values.average())

    private fun slug(type: String) = type.lowercase().replace(' ', '-')

    private fun mapTiles(game: GameInfo) = game.tileMap.values.map { tile ->
        MapTile(tile.position.x, tile.position.y, tile.baseTerrain, tile.terrainFeatures.toList(),
            tile.resource, tile.naturalWonder, tile.isAdjacentToRiver(), tile.getContinent(), tile.improvement)
    }

    private fun verifyControllers(root: Path, candidate: Candidate) {
        val original = create(candidate.mapSeed, candidate.startingPositionSeed, candidate.mapType)
        val expected = mapTiles(original)
        val starts = evaluate(original).starts.map { Triple(it.civilization, it.x, it.y) }
        for (native in roster) {
            val reassigned = create(candidate.mapSeed, candidate.startingPositionSeed, candidate.mapType, native)
            check(mapTiles(reassigned) == expected) { "Map changed when $native was assigned native AI" }
            check(evaluate(reassigned).starts.map { Triple(it.civilization, it.x, it.y) } == starts) {
                "Starting positions changed when $native was assigned native AI"
            }
        }
        Files.writeString(root.resolve("${slug(candidate.mapType)}-verification.json"), json.encodeToString(obj(
            "mapSeed" to candidate.mapSeed, "startingPositionSeed" to candidate.startingPositionSeed,
            "deterministicRegeneration" to true, "nativeCivilizationsChecked" to roster,
            "mapAndCivilizationStartsPreserved" to true, "externalTurnOrderReversed" to true,
        )))
    }

    private fun export(root: Path, game: GameInfo, candidate: Candidate) {
        val stem = slug(candidate.mapType)
        Files.writeString(root.resolve("$stem-best.json"), json.encodeToString(candidate))
        val mapJson = json.encodeToString(mapTiles(game))
        Files.writeString(root.resolve("$stem-tiles.json"), mapJson)
        Files.writeString(root.resolve("$stem-map.sha256"), MessageDigest.getInstance("SHA-256")
            .digest(mapJson.toByteArray()).joinToString("") { "%02x".format(it) } + "\n")
        Files.writeString(root.resolve("$stem-game.json"), UncivFiles.gameInfoToString(game, forceZip = false))
        Files.writeString(root.resolve("$stem-city-states.json"), json.encodeToString(game.civilizations.filter { it.isCityState }.map { civ ->
            val tile = civ.units.getCivUnits().single { it.baseUnit.isCityFounder() }.getTile()
            obj("civilization" to civ.civID, "x" to tile.position.x, "y" to tile.position.y)
        }))
    }
}
