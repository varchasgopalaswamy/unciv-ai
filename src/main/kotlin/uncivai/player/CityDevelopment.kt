package uncivai.player

import com.unciv.logic.city.City
import com.unciv.logic.city.StatTreeNode
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerCityOperations
import com.unciv.logic.map.tile.Tile
import com.unciv.models.ruleset.INonPerpetualConstruction
import com.unciv.models.ruleset.PerpetualConstruction
import com.unciv.models.stats.Stat
import com.unciv.models.stats.Stats
import com.unciv.view.GameView
import kotlinx.serialization.Serializable
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.util.Collections
import java.util.Locale
import kotlin.math.roundToInt

/** Amounts retain the UI's precision, rather than exposing underlying float values. */
@Serializable
data class DisplayedYield(val stat: String, val amount: String)

@Serializable
data class YieldBreakdown(val source: String, val values: List<DisplayedYield>, val children: List<YieldBreakdown> = emptyList())

@Serializable
data class CityWorkTile(
    val position: TilePosition,
    val yields: List<DisplayedYield>,
    val worked: Boolean,
    val locked: Boolean,
    val cityCenter: Boolean,
    val blockaded: Boolean,
    val canWork: Boolean,
    val canUnwork: Boolean,
    val canLock: Boolean,
    val canUnlock: Boolean,
)

@Serializable
data class ConstructionProgress(
    val index: Int,
    val name: String,
    val perpetual: Boolean,
    val productionStored: Int?,
    val productionCost: Int?,
    val productionPerTurn: Int?,
    val turns: Int?,
    val canRemove: Boolean,
    val moveToIndices: List<Int>,
)

@Serializable
data class CityDevelopment(
    val position: TilePosition,
    val population: Int,
    val unassignedPopulation: Int,
    val foodStored: Int,
    val foodToNextPopulation: Int,
    val foodPerTurn: Int,
    val turnsToGrowth: Int?,
    val turnsToStarvation: Int?,
    val growing: Boolean,
    val starving: Boolean,
    val avoidGrowth: Boolean,
    val focus: String,
    val inResistance: Boolean,
    val yields: List<DisplayedYield>,
    val baseYieldBreakdown: List<YieldBreakdown>,
    val percentYieldBreakdown: List<YieldBreakdown>,
    val finalYieldBreakdown: List<YieldBreakdown>,
    val tiles: List<CityWorkTile>,
    val constructions: List<ConstructionProgress>,
    val builtBuildings: List<String>,
)

@Serializable
data class TechnologyProgress(val name: String, val researched: Boolean, val scienceStored: Int, val cost: Int, val turns: String, val unlocks: TechnologyUnlocks? = null)

@Serializable
data class ResearchDevelopment(val current: String?, val sciencePerTurn: String, val overflowScience: Int, val technologies: List<TechnologyProgress>)

/** Called only for an owned city, while holding the game executor. No engine collections escape. */
internal fun observeCityDevelopment(city: City, active: Boolean = true): CityDevelopment {
    val player = city.civ
    val operations = PlayerCityOperations(player)
    val stats = city.cityStats
    val shownStats = Stat.entries.filter { it != Stat.Faith || player.gameInfo.isReligionEnabled() }
    // DetailedStatsPopup merges happiness into both the base and final sections. Clone first:
    // its display-only merge must never mutate authoritative city statistics during observation.
    val base = stats.baseStatTree.clone()
    val final = stats.finalStatList.mapValues { it.value.clone() }.toMutableMap()
    for ((source, happiness) in stats.happinessList) {
        val node = base.children.getOrPut(source) { StatTreeNode() }
        if (node.totalStats.happiness == 0f) node.setInnerStat(Stat.Happiness, happiness)
        val values = final.getOrPut(source) { Stats() }
        if (values.happiness == 0f) values.happiness = happiness
    }
    val queue = city.cityConstructions
    val constructionView = GameView(player.gameInfo, player).getCityView(city).constructions
    return CityDevelopment(
        position = TilePosition(city.location.x, city.location.y),
        population = city.population.population,
        unassignedPopulation = city.population.getFreePopulation(),
        foodStored = city.population.foodStored,
        foodToNextPopulation = city.population.getFoodToNextPopulation(),
        foodPerTurn = city.foodForNextTurn(),
        turnsToGrowth = city.population.getNumTurnsToNewPopulation(),
        turnsToStarvation = city.population.getNumTurnsToStarvation(),
        growing = city.isGrowing(),
        starving = city.isStarving(),
        avoidGrowth = city.avoidGrowth,
        focus = city.getCityFocus().name,
        inResistance = city.isInResistance(),
        yields = frozen(shownStats.map { stat -> DisplayedYield(stat.name, (if (stat == Stat.Happiness) stats.happinessList.values.sum() else stats.currentCityStats[stat]).roundToInt().toString()) }),
        baseYieldBreakdown = observeTree(base, shownStats),
        percentYieldBreakdown = observeTree(stats.statPercentBonusTree, shownStats),
        finalYieldBreakdown = frozen(final.toSortedMap().filterValues { !it.isEmpty() }.map { (source, values) -> YieldBreakdown(source, displayed(values, shownStats)) }),
        tiles = frozen(
            city.getWorkableTiles().filter { it.isVisible(player) }
                .sortedWith(compareBy({ it.position.x }, { it.position.y })).map { tile ->
                    CityWorkTile(
                        position = TilePosition(tile.position.x, tile.position.y),
                        yields = displayed(tile.stats.getTileStats(city, player), shownStats, integral = true),
                        worked = city.isWorked(tile),
                        locked = city.isWorked(tile) && tile.isLocked(),
                        cityCenter = tile.isCityCenter(),
                        blockaded = tile.isBlockaded(),
                        canWork = active && operations.canSetWorkedTile(city, tile, true),
                        canUnwork = active && operations.canSetWorkedTile(city, tile, false),
                        canLock = active && operations.canSetTileLocked(city, tile, true),
                        canUnlock = active && operations.canSetTileLocked(city, tile, false),
                    )
                }.toList(),
        ),
        constructions = frozen(
            queue.constructionQueue.mapIndexed { index, name ->
                val construction = constructionView.getConstruction(name)
                val nonperpetual = construction as? INonPerpetualConstruction
                val first = queue.isFirstConstructionOfItsKind(index, name)
                ConstructionProgress(
                    index = index,
                    name = name,
                    perpetual = construction is PerpetualConstruction,
                    productionStored = nonperpetual?.let { if (first) queue.getWorkDone(name) else 0 },
                    productionCost = nonperpetual?.getProductionCost(player, city),
                    productionPerTurn = queue.productionForConstruction(name),
                    turns = nonperpetual?.let { constructionTurns(city, name, first) },
                    canRemove = active && operations.canRemoveConstruction(city, index),
                    moveToIndices = frozen(queue.constructionQueue.indices.filter { active && operations.canMoveConstruction(city, index, it) }),
                )
            },
        ),
        builtBuildings = frozen(queue.getBuiltBuildings().map { it.name }.sorted().toList()),
    )
}

internal fun observeResearchDevelopment(player: Civilization): ResearchDevelopment = ResearchDevelopment(
    current = player.tech.currentTechnologyName(),
    sciencePerTurn = player.stats.statsForNextTurn.science.roundToInt().toString(),
    overflowScience = player.tech.getOverflowScience(),
    technologies = frozen(
        player.gameInfo.ruleset.technologies.keys.sorted().map { name ->
            TechnologyProgress(
                name,
                player.tech.isResearched(name),
                player.tech.researchOfTech(name),
                player.tech.costOfTech(name),
                player.tech.turnsToTech(name),
                if (name in player.tech.techsToResearch || player.tech.canBeResearched(name)) technologyUnlocks(player, name) else null,
            )
        },
    ),
)

/** Live yields only on visible tiles. The engine's general tile stats do not honor remembered improvements. */
internal fun observeTileYields(player: Civilization, tile: Tile): List<DisplayedYield>? {
    if (!tile.isVisible(player)) return null
    // World-screen TileInfoTable truncates; city-screen tile details round instead.
    return frozen(tile.stats.getTileStats(player).map { (stat, amount) -> DisplayedYield(stat.name, amount.toInt().toString()) })
}

private fun observeTree(tree: StatTreeNode, stats: List<Stat>): List<YieldBreakdown> = frozen(
    tree.children.toSortedMap().filterValues { !it.totalStats.isEmpty() }.map { (source, child) ->
        YieldBreakdown(source, displayed(child.totalStats, stats), observeTree(child, stats))
    },
)

private fun displayed(values: Stats, stats: List<Stat>, integral: Boolean = false): List<DisplayedYield> {
    val decimal = DecimalFormat("0.#", DecimalFormatSymbols(Locale.ROOT))
    return frozen(stats.filter { values[it] != 0f }.map { stat -> DisplayedYield(stat.name, if (integral) values[stat].roundToInt().toString() else decimal.format(values[stat])) })
}

private fun <T> frozen(values: Collection<T>): List<T> = Collections.unmodifiableList(ArrayList(values))
