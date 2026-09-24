package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.map.tile.RoadStatus
import com.unciv.models.ruleset.unique.GameContext
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class ResourceImprovement(
    val name: String,
    val siteAvailable: Boolean,
    val missingTechnologies: List<String>,
    val blockers: List<String>,
    val firstJob: String? = null,
)

@Serializable
data class UnimprovedResource(
    val position: TilePosition,
    val city: String,
    val resource: String,
    val type: String,
    val worked: Boolean,
    val water: Boolean,
    val pillaged: Boolean,
    val currentJob: String?,
    val improvements: List<ResourceImprovement>,
    val builder: String,
)

@Serializable
data class DevelopmentOpportunities(
    val unimprovedResources: List<UnimprovedResource>,
    val roadsAvailable: Boolean,
    val railroadsAvailable: Boolean,
    val unconnectedCities: List<String>,
    val citiesWithoutRailroadConnection: List<String>,
)

/** Own land only. Site eligibility does not certify a worker's route or remaining actions. */
internal fun observeDevelopmentOpportunities(player: Civilization): DevelopmentOpportunities {
    val rules = player.gameInfo.ruleset
    val roads = rules.roadImprovement?.let { it.techRequired == null || player.tech.isResearched(it.techRequired!!) } == true
    val railroads = rules.railroadImprovement?.let { it.techRequired == null || player.tech.isResearched(it.techRequired!!) } == true
    val resources = player.cities.flatMap { city ->
        city.getTiles().filter { tile ->
            val resource = tile.tileResource
            tile.getOwner() === player && tile.isVisible(player) && !tile.isCityCenter() &&
                resource != null && player.canSeeResource(resource) &&
                (tile.improvementIsPillaged || tile.improvement == null || !resource.isImprovedBy(tile.improvement!!))
        }.map { tile ->
            val resource = checkNotNull(tile.tileResource)
            val improvements = resource.getImprovements().sorted().mapNotNull { name ->
                val improvement = rules.tileImprovements[name] ?: return@mapNotNull null
                if (improvement.isGreatImprovement()) return@mapNotNull null
                val context = GameContext(civInfo = player, tile = tile)
                val missing = improvement.requiredTechnologies(rules).filterNotNull().filterNot { player.tech.isResearched(it.name) }.map { it.name }
                var problems = tile.improvementFunctions.getImprovementBuildingProblems(improvement, context).toList()
                var firstJob: String? = if (tile.improvementIsPillaged && tile.improvement != null && resource.isImprovedBy(tile.improvement!!)) "Repair" else null
                if (problems.any { !it.reportable }) {
                    val removal = rules.tileRemovals.firstOrNull { it.name == "Remove ${tile.lastTerrain.name}" }
                    if (removal != null) {
                        val cleared = tile.clone(addUnits = false)
                        cleared.setTerrainTransients()
                        cleared.removeTerrainFeature(cleared.lastTerrain.name)
                        val after = cleared.improvementFunctions.getImprovementBuildingProblems(improvement, GameContext(civInfo = player, tile = cleared)).toList()
                        if (after.all { it.reportable }) {
                            firstJob = removal.name
                            problems = after
                        }
                    }
                }
                ResourceImprovement(name, problems.isEmpty() && firstJob == null && !tile.improvementIsPillaged, missing.sorted().toList(), problems.map { it.name }.sorted(), firstJob)
            }
            UnimprovedResource(
                TilePosition(tile.position.x, tile.position.y), city.name, resource.name, resource.resourceType.name,
                city.isWorked(tile), tile.isWater, tile.improvementIsPillaged, tile.improvementInProgress,
                Collections.unmodifiableList(improvements),
                if (tile.isWater) "WATER_IMPROVEMENT_UNIT" else "WORKER",
            )
        }.toList()
    }.distinctBy { it.position }.sortedWith(compareBy({ it.city }, { it.position.x }, { it.position.y }))
    return DevelopmentOpportunities(
        Collections.unmodifiableList(resources),
        roads,
        railroads,
        if (roads) player.cities.filter { !it.isCapital() && !it.isConnectedToCapital() }.map { it.name }.sorted() else emptyList(),
        if (railroads) player.cities.filter { !it.isCapital() && !it.cityStats.isConnectedToCapital(RoadStatus.Railroad) }.map { it.name }.sorted() else emptyList(),
    )
}
