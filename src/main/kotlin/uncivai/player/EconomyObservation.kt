package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import com.unciv.models.ruleset.tile.ResourceType
import com.unciv.models.ruleset.unique.UniqueType
import com.unciv.models.translations.tr
import com.unciv.view.GameView
import kotlinx.serialization.Serializable
import java.util.Collections
import kotlin.math.roundToInt

/** Rounded rows from the empire statistics overview, not underlying fractional engine values. */
@Serializable
data class EconomyAmount(val source: String, val amount: Int)

@Serializable
data class UnitSupply(
    val base: Int,
    val fromCities: Int,
    val fromPopulation: Int,
    val total: Int,
    val inUse: Int,
    val deficit: Int,
    val productionPenaltyPercent: Int,
)

/** Available is the resource overview's net total; pending offers are separate, not consumption. */
@Serializable
data class EconomyResource(
    val name: String,
    val type: String,
    val available: Int,
    val used: Int,
    val imported: Int,
    val exported: Int,
    val offered: Int,
    val unimprovedTiles: Int,
    val citiesDemanding: Int,
    val citiesCelebrating: Int,
    val breakdown: List<EconomyAmount>,
)

@Serializable
data class CityConnection(val name: String, val position: TilePosition, val capital: Boolean, val connectedToCapital: Boolean)

@Serializable
data class GoldenAgeObservation(val turnsRemaining: Int, val storedHappiness: Int, val happinessRequired: Int)

@Serializable
data class EconomyObservation(
    val treasury: Int,
    val goldPerTurn: Int,
    val unitMaintenance: Int,
    val goldBreakdown: List<EconomyAmount>,
    val happiness: Int,
    val happinessBreakdown: List<EconomyAmount>,
    val unitSupply: UnitSupply,
    val resources: List<EconomyResource>,
    val cityConnections: List<CityConnection>,
    val goldenAge: GoldenAgeObservation,
    val development: DevelopmentOpportunities? = null,
)

/** The caller supplies the bound player and holds the executor. No foreign balances or routes escape. */
internal fun observeEconomy(player: Civilization, includeDevelopment: Boolean = true): EconomyObservation {
    val view = GameView(player.gameInfo, player).getCivView(player)
    // Same calculations as StatsOverviewTab; reading never refreshes mutable engine caches.
    val stats = view.getStatMapForNextTurn()
    val happiness = view.getHappinessBreakdown()
    val supply = player.stats
    fun rows(values: Map<String, Float>): List<EconomyAmount> = frozenEconomy(
        values.mapNotNull { (source, amount) ->
            amount.roundToInt().takeIf { it != 0 }?.let { EconomyAmount(source.tr(hideIcons = true), it) }
        }.sortedBy { it.source },
    )
    return EconomyObservation(
        treasury = player.gold,
        goldenAge = GoldenAgeObservation(player.goldenAges.turnsLeftForCurrentGoldenAge, player.goldenAges.storedHappiness, player.goldenAges.happinessRequiredForNextGoldenAge()),
        goldPerTurn = stats.values.map { it.gold }.sum().roundToInt(),
        unitMaintenance = -(stats["Unit upkeep"]?.gold ?: 0f).roundToInt(),
        goldBreakdown = rows(stats.mapValues { it.value.gold }),
        happiness = happiness.values.sum().roundToInt(),
        happinessBreakdown = rows(happiness),
        unitSupply = UnitSupply(
            supply.getBaseUnitSupply(),
            supply.getUnitSupplyFromCities(),
            supply.getUnitSupplyFromPop(),
            supply.getUnitSupply(),
            player.units.getCivUnitsSize(),
            supply.getUnitSupplyDeficit(),
            supply.getUnitSupplyProductionPenalty().toInt(),
        ),
        resources = observeEconomyResources(player),
        development = if (includeDevelopment) observeDevelopmentOpportunities(player) else null,
        cityConnections = frozenEconomy(
            player.cities.sortedBy { it.name }.map { city ->
                CityConnection(city.name, TilePosition(city.location.x, city.location.y), city.isCapital(), city.isConnectedToCapital())
            },
        ),
    )
}

private fun observeEconomyResources(player: Civilization): List<EconomyResource> {
    val rules = player.gameInfo.ruleset.tileResources
    val detailed = player.detailedCivResources
    fun TradeOffer.isResource() = type in setOf(TradeOfferType.Strategic_Resource, TradeOfferType.Luxury_Resource) &&
        rules[name]?.isStockpiled == false

    // These are this player's agreements. Resource overview's Trade row nets imports and
    // exports; the player's trade screen permits reading their separate amounts as well.
    val ownTrades = player.diplomacy.values.flatMap { it.trades }
    val imports = ownTrades.flatMap { it.theirOffers }.filter { it.isResource() }.groupBy { it.name }
    val exports = ownTrades.flatMap { it.ourOffers }.filter { it.isResource() }.groupBy { it.name }
    // Match ResourcesOverviewTab's TradeOffer extra row: only this player's outgoing offers.
    val offered = player.getKnownCivs().flatMap { other ->
        other.tradeRequests.asSequence().filter { it.requestingCiv == player.civID }
            .flatMap { it.trade.theirOffers.asSequence() }.filter { it.isResource() }
    }.groupBy { it.name }
    // ResourcesOverviewTab also shows unimproved sources in our cities and allied
    // city-states, and luxuries demanded by our own cities. These counts do not reveal
    // foreign major-civilization resources or expose the locations of allied sources.
    val citiesWithVisibleSources = player.cities + player.getKnownCivs()
        .filter { it.isCityState && it.allyCiv == player }.flatMap { it.cities }
    val unimproved = citiesWithVisibleSources.flatMap { it.getTiles().toList() }
        .filter { tile ->
            val resource = tile.tileResource
            resource != null && resource.resourceType != ResourceType.Bonus &&
                player.canSeeResource(resource) && !tile.providesResources(player)
        }.groupBy { it.tileResource!!.name }
    val demands = player.cities.filter { it.demandedResource.isNotEmpty() }.groupBy { it.demandedResource }
    val extraNames = offered.keys + unimproved.keys + demands.keys
    val resources = (
        detailed.map { it.resource } + extraNames.mapNotNull(rules::get) +
            rules.values.filter { it.resourceType == ResourceType.Strategic && player.canSeeResource(it) }
        )
        .filter { it.resourceType != ResourceType.Bonus && !it.isCityWide && !it.hasUnique(UniqueType.NotShownOnWorldScreen, player.state) }
        .distinctBy { it.name }
        .sortedWith(compareBy({ it.resourceType }, { it.name }))
    return frozenEconomy(
        resources.map { resource ->
            val sources = detailed.filter { it.resource.name == resource.name }
            EconomyResource(
                name = resource.name,
                type = resource.resourceType.name,
                available = sources.sumOf { it.amount },
                used = -sources.filter { it.origin != "Trade" && it.amount < 0 }.sumOf { it.amount },
                imported = imports[resource.name].orEmpty().sumOf { it.amount },
                exported = exports[resource.name].orEmpty().sumOf { it.amount },
                offered = offered[resource.name].orEmpty().sumOf { it.amount },
                unimprovedTiles = unimproved[resource.name].orEmpty().size,
                citiesDemanding = demands[resource.name].orEmpty().count { !it.isWeLoveTheKingDayActive() },
                citiesCelebrating = demands[resource.name].orEmpty().count { it.isWeLoveTheKingDayActive() },
                breakdown = frozenEconomy(sources.sortedBy { it.origin }.map { EconomyAmount(it.origin.removeSuffix("+").tr(hideIcons = true), it.amount) }),
            )
        },
    )
}

private fun <T> frozenEconomy(values: List<T>): List<T> = Collections.unmodifiableList(values.toList())
