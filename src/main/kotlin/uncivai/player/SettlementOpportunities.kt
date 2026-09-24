package uncivai.player

import com.unciv.logic.automation.Automation
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.models.ruleset.tile.ResourceType
import kotlinx.serialization.Serializable
import java.util.Collections
import kotlin.math.roundToInt

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
)

@Serializable
data class SettlementCandidate(
    val position: TilePosition,
    val score: Int,
    val siteStatus: String,
    val uncertainty: String?,
    val nearestSettler: String,
    val distance: Int,
    val reasons: List<String>,
)

@Serializable
data class SettlementOpportunities(
    val scope: String = "EXPLORED_TILES",
    val exploredTiles: Int,
    val knownEligibleSites: Int,
    val uncertainSites: Int,
    val blockedSites: Int,
    val status: String,
    val candidates: List<SettlementCandidate>,
    val scoring: String = "Advisory adaptation of Unciv's settlement heuristic using current and last-seen player knowledge. Distance is in hexes, not a route estimate.",
)

internal fun updateSettlementKnowledge(player: Civilization, state: TurnSessionState) {
    // Older checkpoints can retain city locations already delivered on their map.
    // Do not obtain an unseen city's current existence from the native game.
    if (state.settlementKnowledge.isEmpty()) {
        for (shown in state.fingerprint?.map?.tiles.orEmpty().filter { it.cityName != null }) {
            val tile = player.gameInfo.tileMap.getOrNull(shown.position.x, shown.position.y) ?: continue
            state.settlementKnowledge[shown.position] = KnownSettlementTile(
                shown.position, state.fingerprint!!.turn, true, false, tile.getContinent(), shown.owner, shown.cityName,
                false, false, false, shown.resource, null, 0f,
            )
        }
    }
    for (tile in player.gameInfo.tileMap.values.filter { it.isVisible(player) || it.getOwner() === player }) {
        val resource = tile.tileResource?.takeIf { player.canSeeResource(it) }
        val position = TilePosition(tile.position.x, tile.position.y)
        val yieldScore = Automation.rankStatsValue(tile.stats.getTileStats(null, player), player) + when (resource?.resourceType) {
            ResourceType.Bonus -> 1f
            ResourceType.Strategic -> 2f
            ResourceType.Luxury -> 10f
            else -> 0f
        }
        state.settlementKnowledge[position] = KnownSettlementTile(
            position, player.gameInfo.turns, tile.isLand, tile.isImpassible(), tile.getContinent(), tile.getOwner()?.civName,
            tile.getCity()?.name?.takeIf { tile.isCityCenter() }, tile.isHill(), tile.isAdjacentToRiver(),
            tile.baseTerrain == "Mountain" || "Mountain" in tile.terrainFeatures, resource?.name, resource?.resourceType?.name, yieldScore,
        )
    }
}

internal fun observeSettlementOpportunities(player: Civilization, state: TurnSessionState): SettlementOpportunities? {
    val settlers = player.units.getCivUnits().filter { UnitSettlement.hasFoundingAbility(it) }.toList()
    if (settlers.isEmpty()) return null
    val tiles = player.gameInfo.tileMap.values.filter { it.isExplored(player) }.toList()
    val knowledge = state.settlementKnowledge
    val prior = knowledge.values.filter { it.land && it.owner == null }.map { it.yieldScore }.average().takeIf { it.isFinite() }?.toFloat() ?: 0f
    var eligible = 0
    var uncertain = 0
    var blocked = 0
    val candidates = tiles.mapNotNull { tile ->
        val position = TilePosition(tile.position.x, tile.position.y)
        val settler = settlers.minWith(compareBy({ it.currentTile.aerialDistanceTo(tile) }, { it.id }))
        val site = observeSettlement(player, settler, position, active = false, knowledge = knowledge)!!
        if (site.siteStatus == "BLOCKED") {
            blocked++
            return@mapNotNull null
        }
        if (site.siteStatus == "ELIGIBLE") eligible++ else uncertain++
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
            if (seen?.resourceType == "Luxury" && seen.resource != null && !player.hasResource(seen.resource) && newLuxuries.add(seen.resource)) score += 10f
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
        val distance = settler.currentTile.aerialDistanceTo(tile)
        score *= (100 - (distance * 3).coerceAtMost(99)) / 100f
        SettlementCandidate(position, score.roundToInt(), site.siteStatus, site.uncertainty, state.unitHandles.getValue(settler.id), distance, Collections.unmodifiableList(reasons))
    }.sortedWith(compareByDescending<SettlementCandidate> { it.score }.thenBy { it.position.x }.thenBy { it.position.y })
    return SettlementOpportunities(
        exploredTiles = tiles.size,
        knownEligibleSites = eligible,
        uncertainSites = uncertain,
        blockedSites = blocked,
        status = when {
            eligible > 0 -> "KNOWN_SITES_AVAILABLE"
            uncertain > 0 -> "ONLY_UNCERTAIN_SITES"
            else -> "NO_KNOWN_SITES"
        },
        candidates = Collections.unmodifiableList(candidates),
    )
}
