package uncivai.player

import com.unciv.logic.city.SettlementBudget
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.map.tile.Tile
import com.unciv.models.ruleset.tile.ResourceType
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable

@Serializable
data class ExpansionImprovement(val name: String, val missingTechnologies: List<String>, val firstJob: String?)

@Serializable
data class ExpansionResource(
    val name: String,
    val type: String,
    val position: TilePosition,
    val distance: Int,
    val amount: Int?,
    val owner: String?,
    val ownership: String,
    val observedTurn: Int,
    val withinExistingCityRange: List<String>,
    val currentlyAvailable: Boolean,
    val happinessIfConnected: Float?,
    val builder: String?,
    val improvements: List<ExpansionImprovement>,
    val connectedByFounding: Boolean? = null,
)

internal fun expansionResources(player: Civilization, center: Tile, state: TurnSessionState): List<ExpansionResource> = center.getTilesInDistance(player.gameInfo.ruleset.modOptions.constants.cityWorkRange).mapNotNull { tile ->
    val position = TilePosition(tile.position.x, tile.position.y)
    val known = state.settlementKnowledge[position] ?: return@mapNotNull null
    val resource = known.resource?.let { player.gameInfo.ruleset.tileResources[it] } ?: return@mapNotNull null
    ExpansionResource(
        resource.name, resource.resourceType.name, position, center.aerialDistanceTo(tile), known.resourceAmount,
        known.owner,
        when (known.owner) {
            null -> "UNCLAIMED"
            player.civName -> "OWN"
            else -> "FOREIGN"
        },
        known.observedTurn,
        player.cities.filter { it.getCenterTile().aerialDistanceTo(tile) <= it.getWorkRange() }.map { state.cityHandle(it.id) }.sorted(),
        player.hasResource(resource.name),
        if (resource.resourceType == ResourceType.Luxury) player.stats.getAdditionalLuxuryHappiness(resource) else null,
        if (tile === center) null else player.getEquivalentUnit(if (known.land) "Worker" else "Work Boats").name,
        if (tile === center) {
            resource.getImprovements().sorted().mapNotNull { name ->
                player.gameInfo.ruleset.tileImprovements[name]?.takeIf { it.turnsToBuild != -1 }?.let { improvement ->
                    ExpansionImprovement(name, listOfNotNull(improvement.techRequired).filterNot(player.tech::isResearched), null)
                }
            }
        } else {
            known.resourceImprovements.map { ExpansionImprovement(it.name, it.missingTechnologies.filterNot(player.tech::isResearched), it.firstJob) }
        },
        if (tile === center) resource.canConnectUnderCity(player) else null,
    )
}.sortedWith(compareBy({ it.type }, { it.name }, { it.position.x }, { it.position.y })).toList()

@Serializable
data class SettlementPopulationCost(val population: Int, val cityUnhappiness: Float, val populationUnhappiness: Float)

@Serializable
data class SettlementHappinessBuilding(val name: String, val happiness: Float, val productionCost: Int, val baseMaintenance: Int, val missingTechnologies: List<String>, val requirements: List<String>)

@Serializable
data class SettlementHappiness(
    val populationCosts: List<SettlementPopulationCost>,
    val buildings: List<SettlementHappinessBuilding>,
    val basis: String = "Native city/population unhappiness under current civilization modifiers, at starting population and after growth. These are cost components, not net founding outcomes: starting/free buildings, connected luxuries, specialists and other happiness effects are excluded. Building amounts are independent passive contributions for an empty city; requirements and technology still apply. Existing-city production cannot estimate a new city's completion time.",
)

@Serializable
data class ExpansionBuildingRule(
    val name: String,
    val baseHappiness: Float,
    val productionCost: Int,
    val baseMaintenance: Int,
    val missingTechnologies: List<String>,
    val requiredBuilding: String?,
    val requiredNearbyImprovedResources: List<String>,
    val effectsAndConditions: List<String>,
)

@Serializable
data class ExpansionHappinessBaseline(
    val referencePosition: TilePosition,
    val populationCosts: List<SettlementPopulationCost>,
    val buildingCatalogue: List<ExpansionBuildingRule>,
    val basis: String = "Illustrative empty, unconnected city at the known reference tile, using current civilization modifiers. Costs exclude free buildings, luxuries and other happiness effects. The catalogue shows base building happiness and rules, not verified availability or policy/religion bonuses. Site-specific effects can differ; acquiring and improving a luxury and constructing buildings take time.",
)

internal fun expansionHappinessBaseline(player: Civilization): ExpansionHappinessBaseline? {
    // Cost components use a known home tile, not an unseen prospective site.
    val reference = player.getCapital()?.getCenterTile() ?: player.units.getCivUnits().map { it.currentTile }.firstOrNull { it.isLand }
        ?: return null
    val initial = player.gameInfo.ruleset.eras.getValue(player.gameInfo.gameParameters.startingEra).settlerPopulation
    val buildings = player.gameInfo.ruleset.buildings.values.filter {
        it.happiness > 0 && !it.isAnyWonder() && (it.uniqueTo == null || player.matchesFilter(it.uniqueTo!!)) && player.getEquivalentBuilding(it) == it
    }.sortedBy { it.name }.map { building ->
        ExpansionBuildingRule(
            building.name,
            building.happiness,
            building.getProductionCost(player, null),
            building.maintenance,
            building.requiredTechs().filterNot(player.tech::isResearched).sorted().toList(),
            building.requiredBuilding?.let { player.getEquivalentBuilding(it).name },
            building.requiredNearbyImprovedResources.orEmpty(),
            building.uniqueObjects.filterNot { it.isHiddenToUsers() }.map { it.getDisplayText().tr() },
        )
    }
    return ExpansionHappinessBaseline(
        TilePosition(reference.position.x, reference.position.y),
        SettlementBudget(player).costs(reference.position, listOf(initial, initial + 1, initial + 4)).map { SettlementPopulationCost(it.population, it.cityUnhappiness, it.populationUnhappiness) },
        buildings,
    )
}

internal fun settlementHappiness(player: Civilization, position: TilePosition): SettlementHappiness? {
    val tile = player.gameInfo.tileMap[position.x, position.y]
    // Site-dependent uniques and building prerequisites must not inspect hidden surroundings.
    if (tile.getTilesInDistance(player.gameInfo.ruleset.modOptions.constants.cityWorkRange).any { !it.isVisible(player) && it.getOwner() !== player }) return null
    val initial = player.gameInfo.ruleset.eras.getValue(player.gameInfo.gameParameters.startingEra).settlerPopulation
    val budget = SettlementBudget(player)
    return SettlementHappiness(
        budget.costs(tile.position, listOf(initial, initial + 1, initial + 4)).map { SettlementPopulationCost(it.population, it.cityUnhappiness, it.populationUnhappiness) },
        budget.buildings(tile.position, initial).map { SettlementHappinessBuilding(it.building.name, it.happiness, it.productionCost, it.building.maintenance, it.missingTechnologies, it.requirements.map(String::tr)) },
    )
}
