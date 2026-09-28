package uncivai.player

import com.unciv.json.json
import com.unciv.logic.city.BuildingYieldPreview
import com.unciv.logic.city.City
import com.unciv.logic.city.CityStats
import com.unciv.logic.civilization.Civilization
import com.unciv.models.ruleset.Building
import com.unciv.models.ruleset.MilestoneType
import com.unciv.models.stats.Stat
import com.unciv.models.stats.Stats
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.security.MessageDigest
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.util.Collections
import java.util.Locale

@Serializable
data class ConstructionProduction(
    val cost: Int,
    val stored: Int,
    val overflow: Int,
    val perTurn: Int,
    val turnsIfStartedNow: Int?,
)

@Serializable
data class ConstructionYieldChange(
    val before: Map<String, String>,
    val after: Map<String, String>,
    val change: Map<String, String>,
)

@Serializable
data class BuildingCompletionEffect(
    val yields: ConstructionYieldChange,
    val maintenanceChange: String,
    val sourceChanges: Map<String, Map<String, String>>,
    val specialistSlots: Map<String, Int>,
    val greatPersonPoints: Map<String, Int>,
    val growth: ConstructionGrowthChange,
)

@Serializable
data class ConstructionGrowthChange(
    val foodPerTurnBefore: Int,
    val foodPerTurnAfter: Int,
    val turnsToGrowthBefore: Int?,
    val turnsToGrowthAfter: Int?,
    val turnsToStarvationBefore: Int?,
    val turnsToStarvationAfter: Int?,
)

@Serializable
data class ConstructionEstimate(
    val production: ConstructionProduction,
    val requiredTechnologies: List<String> = emptyList(),
    val resourceRequirements: Map<String, Int> = emptyMap(),
    val queueUnavailableReasons: List<String>,
    val whileBuilding: ConstructionYieldChange,
    val completion: BuildingCompletionEffect?,
)

/** City-screen production time assuming this item starts now, not after earlier
 * queue entries. Resistance suspends production; a nonpositive rate has no ETA.
 * Later copies of a unit must not reuse the first copy's stored production.
 */
internal fun constructionTurns(city: City, name: String, useStoredProduction: Boolean = true): Int? {
    val queue = city.cityConstructions
    if (city.isInResistance() || queue.productionForConstruction(name) <= 0) return null
    return queue.turnsToConstruction(name, useStoredProduction)
}

/** Conservative cache: any saved native change invalidates it, including other
 * players' actions. Current city statistics are also checked because they are
 * transient. The digest is private implementation state and never serialized.
 * This cache is discarded on restart and on native command execution.
 */
internal class ConstructionEstimateCache {
    private var key: List<Byte>? = null
    private var value: Map<String, Map<String, ConstructionEstimate>> = emptyMap()

    fun clear() {
        key = null
        value = emptyMap()
    }

    fun observe(player: Civilization): Map<String, Map<String, ConstructionEstimate>> {
        val digest = MessageDigest.getInstance("SHA-256")
        digest.update(json().toJson(player.gameInfo).toByteArray(Charsets.UTF_8))
        for (city in player.cities) {
            digest.update(
                (
                    Stat.entries.joinToString(",") { city.cityStats.currentCityStats[it].toRawBits().toString() } +
                        city.cityStats.happinessList.toSortedMap().toString()
                    ).toByteArray(Charsets.UTF_8),
            )
        }
        val current = digest.digest().toList()
        if (key != current) {
            val estimator = ConstructionEstimator(player)
            val estimates = player.cities.associate { it.id to estimator.observe(it) }
            value = Collections.unmodifiableMap(estimates)
            key = current
        }
        return value
    }
}

/** One native batch per observation. All records remain in the permitted snapshot;
 * Python can serve selective reads and refresh their evidence without a second read.
 */
internal class ConstructionEstimator(private val player: Civilization) {
    private val preview = BuildingYieldPreview(player.gameInfo)
    private val decimal = DecimalFormat("0.#", DecimalFormatSymbols(Locale.ROOT))
    private val metrics = Stat.entries.filter { it != Stat.Faith || player.gameInfo.isReligionEnabled() }

    fun observe(city: City): Map<String, ConstructionEstimate> {
        val queue = city.cityConstructions
        val rules = player.gameInfo.ruleset
        // Victory prerequisites are public rules, even before their technologies
        // unlock the city production menu. Keep their native costs available for
        // long-term planning without making a blocked construction queueable.
        val victoryRequirements = player.gameInfo.getEnabledVictories().values.flatMap { victory ->
            victory.requiredSpaceshipPartsAsCounter.filter { (name, count) ->
                player.victoryManager.currentsSpaceshipParts[name] < count
            }.keys + victory.milestoneObjects.filter {
                it.type in setOf(MilestoneType.BuiltBuilding, MilestoneType.BuildingBuiltGlobally) &&
                    !it.hasBeenCompletedBy(player)
            }.map { it.params[0] }
        }.toSet()
        val choices = (rules.buildings.values.asSequence() + rules.units.values.asSequence())
            .filter { it.name in queue.constructionQueue || it.shouldBeDisplayed(queue) || it.name in victoryRequirements }
            .sortedBy { it.name }
        val during = CityStats(city)
        during.statsFromTiles = city.cityStats.statsFromTiles.clone()
        val before = totals(city.cityStats)
        return freeze(
            choices.associate { construction ->
                during.update(construction, updateTileStats = false, updateCivStats = false)
                val rate = queue.productionForConstruction(construction.name)
                construction.name to ConstructionEstimate(
                    production = ConstructionProduction(
                        construction.getProductionCost(player, city),
                        queue.getWorkDone(construction.name),
                        queue.productionOverflow,
                        rate,
                        constructionTurns(city, construction.name),
                    ),
                    requiredTechnologies = Collections.unmodifiableList(construction.requiredTechs().sorted().toList()),
                    resourceRequirements = freeze(construction.getResourceRequirementsPerTurn(city.state)),
                    queueUnavailableReasons = Collections.unmodifiableList(
                        construction.getRejectionReasons(queue)
                            .map { if (it.shouldShow) it.errorMessage.tr() else "Construction requirements are not met" }.distinct().sorted().toList(),
                    ),
                    whileBuilding = difference(before, totals(during)),
                    completion = (construction as? Building)?.takeUnless { queue.isBuilt(it.name) }?.let { building ->
                        val compared = preview.compare(city, building)
                        val sources = (compared.before.sources.keys + compared.after.sources.keys).sorted().associateWith { source ->
                            changes(compared.before.sources[source] ?: Stats(), compared.after.sources[source] ?: Stats())
                        }.filterValues { it.isNotEmpty() }
                        BuildingCompletionEffect(
                            difference(compared.before.total, compared.after.total, includeCoreZeroes = true),
                            number(compared.after.buildingMaintenance - compared.before.buildingMaintenance),
                            freeze(sources),
                            freeze(building.newSpecialists().toSortedMap()),
                            freeze(building.greatPersonPoints.toSortedMap()),
                            ConstructionGrowthChange(
                                compared.before.foodPerTurn,
                                compared.after.foodPerTurn,
                                compared.before.turnsToGrowth,
                                compared.after.turnsToGrowth,
                                compared.before.turnsToStarvation,
                                compared.after.turnsToStarvation,
                            ),
                        )
                    },
                )
            },
        )
    }

    private fun totals(stats: CityStats) = stats.currentCityStats.clone().also { it.happiness = stats.happinessList.values.sum() }
    private fun number(value: Float) = if (kotlin.math.abs(value) < 0.05f) "0" else decimal.format(value)
    private fun values(stats: Stats) = freeze(metrics.associate { it.name to number(stats[it]) })
    private fun changes(before: Stats, after: Stats, includeCoreZeroes: Boolean = false) = freeze(
        metrics.mapNotNull { stat ->
            val core = stat in setOf(Stat.Science, Stat.Food, Stat.Production, Stat.Gold)
            number(after[stat] - before[stat]).takeUnless { it == "0" && !(includeCoreZeroes && core) }?.let { amount -> stat.name to amount }
        }.toMap(),
    )
    private fun difference(before: Stats, after: Stats, includeCoreZeroes: Boolean = false) = ConstructionYieldChange(values(before), values(after), changes(before, after, includeCoreZeroes))
    private fun <K, V> freeze(map: Map<K, V>): Map<K, V> = Collections.unmodifiableMap(LinkedHashMap(map))
}
