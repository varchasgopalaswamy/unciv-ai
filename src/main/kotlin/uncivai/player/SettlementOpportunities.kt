package uncivai.player

import com.unciv.logic.automation.Automation
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.TileMap
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.logic.map.tile.RoadStatus
import com.unciv.logic.map.tile.Tile
import com.unciv.models.ruleset.tile.ResourceType
import kotlinx.serialization.Serializable
import java.util.Collections
import kotlin.math.roundToInt

@Serializable
data class KnownTileSurface(
    val terrain: String,
    val features: List<String>,
    val improvement: String?,
    val improvementPillaged: Boolean?,
    val road: String,
    val roadPillaged: Boolean,
    val naturalWonder: String?,
    val riverBottom: Boolean,
    val riverBottomLeft: Boolean,
    val riverBottomRight: Boolean,
)

/** Last-seen facts only. Unseen founding, razing and border changes never update this record. */
@Serializable
data class KnownSettlementTile(
    val position: TilePosition,
    val observedTurn: Int,
    val land: Boolean,
    val impassable: Boolean,
    val continent: Int,
    val owner: String?,
    val cityName: String?,
    val hill: Boolean,
    val river: Boolean,
    val mountain: Boolean,
    val resource: String?,
    val resourceType: String?,
    val yieldScore: Float,
    val resourceAmount: Int? = null,
    val resourceImprovements: List<ResourceImprovement> = emptyList(),
    val surface: KnownTileSurface? = null,
    val cityIsOriginalMajorCapital: Boolean? = null,
)

@Serializable
data class SettlementCandidate(
    val position: TilePosition,
    val score: Int,
    val nearestSettler: String?,
    val distance: Int?,
    val reasons: List<String>,
    val route: SettlementRoute? = null,
    val nearestCity: String? = null,
    val resources: List<ExpansionResource> = emptyList(),
    val unseenWorkableTiles: Int = 0,
    val happiness: SettlementHappiness? = null,
)

@Serializable
data class SettlementOpportunities(
    val scope: String = "EXPLORED_TILES",
    val exploredTiles: Int,
    val eligibleSites: Int,
    val blockedSites: Int,
    val status: String,
    val candidates: List<SettlementCandidate>,
    val settlers: List<SettlerReview> = emptyList(),
    val recommendations: List<TilePosition> = emptyList(),
    val happinessBaseline: ExpansionHappinessBaseline? = null,
    val expansionBasis: String = "Resource potential covers the native city work radius using current/last-seen observations. Unowned tiles still need acquisition; other cities may share the area. Resource benefits require the appropriate technologies and improvements. Duplicate luxuries are potential trade goods, not extra happiness. Candidate areas can compete for the same tiles and resources.",
    val scoring: String = "Advisory adaptation of Unciv's settlement heuristic using current and last-seen player knowledge. Distance is in hexes from nearestSettler, or nearestCity when no settler exists, not a route estimate. Unknown territory may contain more sites.",
)

internal fun updateSettlementKnowledge(player: Civilization, state: TurnSessionState) {
    captureSettlementKnowledge(player, state, player.gameInfo.tileMap.values.filter { it.isVisible(player) || it.getOwner() === player })
    val learned = player.tech.techsResearched - state.settlementTechnologies
    state.settlementTechnologies.addAll(learned)
    if (learned.isEmpty()) return
    // Technology reveals resources throughout explored territory. It does not
    // reveal changes to terrain, ownership, cities or infrastructure under fog.
    val revealed = state.settlementKnowledge.values.mapNotNull { known ->
        val tile = player.gameInfo.tileMap[known.position.x, known.position.y]
        val resource = tile.tileResource?.takeIf { it.revealedBy in learned && tile.isExplored(player) && player.canSeeResource(it) }
        if (resource == null || resource.name == known.resource || known.surface == null) {
            null
        } else {
            known.copy(
                resource = resource.name,
                resourceType = resource.resourceType.name,
                resourceAmount = tile.resourceAmount.takeIf { resource.resourceType == ResourceType.Strategic },
            )
        }
    }
    if (revealed.isEmpty()) return
    revealed.forEach { state.settlementKnowledge[it.position] = it }
    val remembered = rememberedSettlementMap(player, state)
    for (known in revealed) {
        val tile = remembered[known.position.x, known.position.y]
        state.settlementKnowledge[known.position] = known.copy(
            yieldScore = settlementYieldScore(player, tile),
            resourceImprovements = resourceImprovements(player, tile),
        )
    }
}

/** Called synchronously by native visibility updates, even between movement steps. */
internal fun captureSettlementKnowledge(player: Civilization, state: TurnSessionState, tiles: Iterable<Tile>, remoteReveal: Boolean = false) {
    for (tile in tiles) {
        val resource = tile.tileResource?.takeIf { player.canSeeResource(it) }
        val position = TilePosition(tile.position.x, tile.position.y)
        // A distant map reveal does not reveal an improvement's private state.
        // Only improvements explicitly supplied by native map memory are known.
        val improvement = if (remoteReveal) tile.getShownImprovement(player) else tile.improvement
        val pillaged = if (remoteReveal) state.settlementKnowledge[position]?.surface
            ?.takeIf { it.improvement == improvement }?.improvementPillaged else tile.improvementIsPillaged
        val observed = if (remoteReveal) tile.clone(addUnits = false).apply {
            this.improvement = improvement
            improvementIsPillaged = pillaged == true
            setTerrainTransients()
        } else tile
        state.settlementKnowledge[position] = KnownSettlementTile(
            position, player.gameInfo.turns, tile.isLand, tile.isImpassible(), tile.getContinent(), tile.getOwner()?.civName,
            tile.getCity()?.name?.takeIf { tile.isCityCenter() }, tile.isHill(), tile.isAdjacentToRiver(),
            tile.baseTerrain == "Mountain" || "Mountain" in tile.terrainFeatures, resource?.name, resource?.resourceType?.name, settlementYieldScore(player, observed),
            resource?.takeIf { it.resourceType == ResourceType.Strategic }?.let { tile.resourceAmount }, resource?.let { resourceImprovements(player, observed) }.orEmpty(),
            KnownTileSurface(
                tile.baseTerrain, tile.terrainFeatures.toList(), improvement, pillaged, tile.roadStatus.name, tile.roadIsPillaged,
                tile.naturalWonder, tile.hasBottomRiver, tile.hasBottomLeftRiver, tile.hasBottomRightRiver,
            ),
            tile.getCity()?.takeIf { tile.isCityCenter() }?.let { if (it.isOriginalCapital) it.foundingCivObject?.isMajorCiv() else false },
        )
    }
}

private fun settlementYieldScore(player: Civilization, tile: Tile): Float = Automation.rankStatsValue(tile.stats.getTileStats(null, player), player) + when (tile.tileResource?.takeIf(player::canSeeResource)?.resourceType) {
    ResourceType.Bonus -> 1f
    ResourceType.Strategic -> 2f
    ResourceType.Luxury -> 10f
    else -> 0f
}

/** A detached map for native yield and improvement calculations. No live tile facts are copied. */
internal fun rememberedSettlementMap(player: Civilization, state: TurnSessionState): TileMap {
    val original = player.gameInfo.tileMap
    val tiles = state.settlementKnowledge.mapNotNull { (position, known) ->
        val surface = known.surface ?: return@mapNotNull null
        position to Tile().apply {
            ruleset = player.gameInfo.ruleset
            this.position = HexCoord(position.x, position.y)
            baseTerrain = surface.terrain
            improvement = surface.improvement
            improvementIsPillaged = surface.improvementPillaged == true
            roadStatus = RoadStatus.valueOf(surface.road)
            roadIsPillaged = surface.roadPillaged
            naturalWonder = surface.naturalWonder
            hasBottomRiver = surface.riverBottom
            hasBottomLeftRiver = surface.riverBottomLeft
            hasBottomRightRiver = surface.riverBottomRight
            tileResource = known.resource?.let { ruleset.tileResources[it] }
            resourceAmount = known.resourceAmount ?: 0
            setTerrainTransients()
            setTerrainFeatures(surface.features)
        }
    }.toMap()
    return TileMap(tiles.size).apply {
        gameInfo = player.gameInfo
        mapParameters = original.mapParameters
        // Preserve the original geometry (including wrapping), leaving unknown
        // positions empty rather than borrowing the live map's neighbors.
        leftX = original.leftX
        bottomY = original.bottomY
        width = original.width
        tileMatrix = ArrayList(
            original.tileMatrix.map { column ->
                ArrayList(column.map { tile -> tile?.let { tiles[TilePosition(it.position.x, it.position.y)] } })
            },
        )
        tileList = ArrayList(tiles.values)
        setTransients(player.gameInfo.ruleset, setUnitCivTransients = false)
        width = original.width
    }
}

internal fun observeSettlementOpportunities(player: Civilization, state: TurnSessionState, active: Boolean): SettlementOpportunities {
    val settlers = player.units.getCivUnits().filter { UnitSettlement.hasFoundingAbility(it) }.toList()
    state.settlerHistory.keys.retainAll(settlers.map { it.id }.toSet())
    val tiles = player.gameInfo.tileMap.values.filter { it.isExplored(player) }.toList()
    val knowledge = state.settlementKnowledge
    val prior = knowledge.values.filter { it.land && it.owner == null }.map { it.yieldScore }.average().takeIf { it.isFinite() }?.toFloat() ?: 0f
    var eligible = 0
    var blocked = 0
    val baseScores = HashMap<TilePosition, Float>()
    val candidates = tiles.mapNotNull { tile ->
        val position = TilePosition(tile.position.x, tile.position.y)
        val settler = settlers.minWithOrNull(compareBy({ it.currentTile.aerialDistanceTo(tile) }, { it.id }))
        val city = if (settler == null) player.cities.minWithOrNull(compareBy({ it.getCenterTile().aerialDistanceTo(tile) }, { it.id })) else null
        val site = observeSettlement(player, settler, position, active = false, knowledge = knowledge)!!
        if (site.siteStatus == "BLOCKED") {
            blocked++
            return@mapNotNull null
        }
        eligible++
        val known = knowledge[position]
        val reasons = ArrayList<String>()
        var score = 0f
        if (known?.hill == true) {
            score += 14
            reasons.add("Hill")
        }
        if (known?.river == true) {
            score += 20
            reasons.add("River")
        }
        val coast = tile.neighbors.any { knowledge[TilePosition(it.position.x, it.position.y)]?.land == false }
        if (coast) {
            score += 3
            reasons.add("Coastal")
        }
        if (tile.neighbors.any { knowledge[TilePosition(it.position.x, it.position.y)]?.mountain == true }) {
            score += 5
            reasons.add("Adjacent mountain")
        }
        if (known?.resource != null) score -= if (known.resourceType == "Bonus") 12 else 4
        val newLuxuries = HashSet<String>()
        for (nearby in tile.getTilesInDistance(2)) {
            val seen = knowledge[TilePosition(nearby.position.x, nearby.position.y)]
            val distance = tile.aerialDistanceTo(nearby)
            val value = when {
                seen == null -> prior
                seen.cityName != null -> -1f
                seen.owner != null && seen.owner != player.civName -> 0f
                else -> seen.yieldScore + if (!seen.land && !coast) -1f else 0f
            }
            score += value * (3f / (distance + 1))
            if (seen?.resourceType == "Luxury" && seen.owner == null && seen.resource != null && !player.hasResource(seen.resource) && newLuxuries.add(seen.resource)) score += 10f
        }
        if (newLuxuries.isNotEmpty()) reasons.add("New luxuries: ${newLuxuries.sorted().joinToString()}")
        for (city in player.cities) {
            val spacing = when (tile.aerialDistanceTo(city.getCenterTile())) {
                7 -> 2
                6 -> 4
                5 -> 8
                4 -> 6
                3 -> -25
                in 0..2 -> -30
                else -> 0
            }
            score += spacing * if (city.isCapital()) 2 else 1
        }
        baseScores[position] = score
        val distance = (settler?.currentTile ?: city?.getCenterTile())?.aerialDistanceTo(tile)
        if (distance != null) score *= (100 - (distance * 3).coerceAtMost(99)) / 100f
        SettlementCandidate(
            position,
            score.roundToInt(),
            settler?.let { state.unitHandles.getValue(it.id) },
            distance,
            Collections.unmodifiableList(reasons),
            nearestCity = city?.let { state.cityHandle(it.id) },
            resources = expansionResources(player, tile, state),
            unseenWorkableTiles = tile.getTilesInDistance(player.gameInfo.ruleset.modOptions.constants.cityWorkRange).count { TilePosition(it.position.x, it.position.y) !in knowledge },
        )
    }.sortedWith(compareByDescending<SettlementCandidate> { it.score }.thenBy { it.position.x }.thenBy { it.position.y })
    val reviews = settlers.sortedBy { it.id }.map { unit ->
        fun scoreAt(position: TilePosition): Int? = baseScores[position]?.let { score ->
            val tile = player.gameInfo.tileMap[position.x, position.y]
            (score * (100 - (unit.currentTile.aerialDistanceTo(tile) * 3).coerceAtMost(99)) / 100f).roundToInt()
        }
        val history = observeSettlerHistory(player, unit, state)
        val current = observeSettlement(player, unit, history.position, active, knowledge)!!
        val target = history.destination?.let { position ->
            val site = observeSettlement(player, unit, position, active, knowledge)!!
            SettlementTarget(position, scoreAt(position), SettlementSiteSummary(site.siteStatus, site.siteBlockers, site.uncertainty), settlementRoute(player, unit, position))
        }
        SettlerReview(
            state.unitHandles.getValue(unit.id),
            history.firstObservedTurn,
            player.gameInfo.turns - history.firstObservedTurn,
            player.gameInfo.turns - history.lastMovedTurn,
            history.destinationChanges,
            current,
            scoreAt(history.position),
            target,
        )
    }
    // All candidates are native-legal sites. Suggest distinct possible city centers by score.
    val recommendations = ArrayList<SettlementCandidate>()
    for (candidate in candidates) {
        val tile = player.gameInfo.tileMap[candidate.position.x, candidate.position.y]
        if (recommendations.all { tile.aerialDistanceTo(player.gameInfo.tileMap[it.position.x, it.position.y]) > player.gameInfo.ruleset.modOptions.constants.minimalCityDistance }) recommendations.add(candidate)
        if (recommendations.size == 3) break
    }
    val selected = recommendations.map { it.position }.toSet()
    val planningMap = if (selected.isNotEmpty() && knowledge.values.any { it.surface != null }) rememberedSettlementMap(player, state) else null
    val routedCandidates = candidates.map { candidate ->
        if (candidate.position !in selected) {
            candidate
        } else {
            candidate.copy(
                route = candidate.nearestSettler?.let { handle -> settlementRoute(player, settlers.single { state.unitHandles[it.id] == handle }, candidate.position) },
                happiness = planningMap?.let { settlementHappiness(player, candidate.position, it) },
            )
        }
    }
    return SettlementOpportunities(
        exploredTiles = tiles.size,
        eligibleSites = eligible,
        blockedSites = blocked,
        status = if (eligible > 0) "SITES_AVAILABLE" else "NO_SITES_IN_EXPLORED_TERRITORY",
        candidates = Collections.unmodifiableList(routedCandidates),
        settlers = Collections.unmodifiableList(reviews),
        recommendations = recommendations.map { it.position },
        happinessBaseline = if (routedCandidates.none { it.happiness != null }) expansionHappinessBaseline(player) else null,
    )
}
