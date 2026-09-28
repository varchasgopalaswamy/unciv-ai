package uncivai.player

import com.unciv.logic.city.City
import com.unciv.logic.city.CityStats
import com.unciv.logic.civilization.Civilization
import com.unciv.models.ruleset.PerpetualConstruction
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.util.Locale

/** Current assignments and modifiers, not a forecast of future growth or citizen reassignment. */
@Serializable
data class ProductionConversion(
    val stat: String,
    val productionPerTurn: String,
    val baseYieldPerProduction: Float,
    val yieldPerTurn: String,
)

@Serializable
data class WonderCityOption(
    val city: String,
    val canQueue: Boolean,
    val queued: Boolean,
    val productionCost: Int,
    val productionStored: Int,
    val productionPerTurn: Int,
    val turns: Int?,
    val unavailableReasons: List<String>,
)

@Serializable
data class WonderOpportunity(
    val name: String,
    val kind: String,
    val benefits: String,
    val greatPersonPoints: Map<String, Int>,
    val specialistSlots: Map<String, Int>,
    val cities: List<WonderCityOption>,
)

internal fun observeProductionConversions(city: City, options: List<String>): Map<String, ProductionConversion> {
    val projects = (options + city.cityConstructions.constructionQueue).distinct().sorted().mapNotNull {
        PerpetualConstruction.perpetualConstructionsMap[it] as? PerpetualConstruction.StatConversion
    }
    if (projects.isEmpty()) return emptyMap()
    // Only statistics are detached. City.setTransients reassigns real map tiles to that city
    // and must never be called on a preview city sharing the live map.
    val preview = CityStats(city)
    preview.statsFromTiles = city.cityStats.statsFromTiles.clone()
    val decimal = DecimalFormat("0.#", DecimalFormatSymbols(Locale.ROOT))
    return projects.associate { project ->
        preview.update(project, updateTileStats = false, updateCivStats = false)
        project.name to ProductionConversion(
            stat = project.stat.name,
            productionPerTurn = decimal.format(preview.currentCityStats.production),
            baseYieldPerProduction = preview.getStatConversionRate(project.stat),
            yieldPerTurn = decimal.format(preview.finalStatList["Construction"]?.get(project.stat) ?: 0f),
        )
    }
}

/** Native city-screen entries, including visible unmet prerequisites; never rival build progress. */
internal fun observeWonderOpportunities(
    player: Civilization,
    state: TurnSessionState,
    options: Map<String, List<String>>,
): List<WonderOpportunity> = player.gameInfo.ruleset.buildings.values.filter { it.isAnyWonder() }.sortedBy { it.name }.mapNotNull { wonder ->
    val cities = player.cities.sortedBy { state.cityHandle(it.id) }.mapNotNull cityOption@{ city ->
        val queue = city.cityConstructions
        val queued = wonder.name in queue.constructionQueue
        if (!queued && !wonder.shouldBeDisplayed(queue)) return@cityOption null
        val canQueue = wonder.name in options.getValue(city.id)
        WonderCityOption(
            city = state.cityHandle(city.id),
            canQueue = canQueue,
            queued = queued,
            productionCost = wonder.getProductionCost(player, city),
            productionStored = queue.getWorkDone(wonder.name),
            productionPerTurn = queue.productionForConstruction(wonder.name),
            turns = constructionTurns(city, wonder.name),
            unavailableReasons = wonder.getRejectionReasons(queue).filter { it.shouldShow }.map { it.errorMessage.tr() }.distinct().sorted().toList(),
        )
    }
    if (cities.isEmpty()) return@mapNotNull null
    WonderOpportunity(
        name = wonder.name,
        kind = if (wonder.isWonder) "WORLD" else "NATIONAL",
        benefits = wonder.getShortDescription(),
        greatPersonPoints = wonder.greatPersonPoints.toSortedMap(),
        specialistSlots = wonder.newSpecialists().toSortedMap(),
        cities = cities,
    )
}
