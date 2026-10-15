package uncivai.player

import com.unciv.logic.automation.AutomationSelection
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.map.tile.Tile
import com.unciv.models.ruleset.unique.UniqueType

/** Uses retained observations and map geometry, never undisclosed tile contents. */
internal fun nativeSettlementValues(
    player: Civilization,
    turn: PlayerTurnSnapshot,
    unit: OwnedUnit,
    knowledge: Map<TilePosition, KnownSettlementTile>,
): Map<TilePosition, AutomationSelection.Settlement> {
    val map = player.gameInfo.tileMap
    fun Tile.position() = TilePosition(position.x, position.y)
    val origin = map[unit.position.x, unit.position.y]
    val home = turn.cities.mapNotNull { it.development?.position }
    val range = if (home.isEmpty() && turn.turn < 4) {
        (3 - turn.turn).coerceAtLeast(1)
    } else {
        (8 - (home.minOfOrNull { origin.aerialDistanceTo(map[it.x, it.y]) } ?: 0)).coerceIn(1, 5)
    }
    val rules = player.gameInfo.ruleset
    fun terrains(known: KnownSettlementTile) = (listOfNotNull(known.surface?.terrain) + known.surface?.features.orEmpty()).mapNotNull { rules.terrains[it] }
    fun base(known: KnownSettlementTile): Float {
        val terrains = terrains(known)
        return known.yieldScore +
            (if (terrains.any { it.hasUnique(UniqueType.FreshWater) }) 0.5f else 0f) +
            (if (known.surface?.features?.isNotEmpty() == true && terrains.lastOrNull()?.hasUnique(UniqueType.ProductionBonusWhenRemoved) == true) 0.7f else 0f) +
            (if (known.surface?.naturalWonder != null) 4f else 0f)
    }
    val priorTiles = origin.getTilesInDistance(range + 2).mapNotNull { knowledge[it.position()] }.filter { it.owner == null }.toList()
    val prior = if (priorTiles.isEmpty()) 0f else priorTiles.map(::base).average().toFloat()
    val destinations = unit.moves.filter { it.afterMove?.foundCity == "AVAILABLE" && it.tilesOutsideCurrentSight == 0 && it.unexploredTiles == 0 }
        .map { it.destination } + if (unit.canFoundCity && unit.settlementPromises.isEmpty()) listOf(unit.position) else emptyList()
    return destinations.distinct().mapNotNull { position ->
        val known = knowledge[position] ?: return@mapNotNull null
        val tile = map[position.x, position.y] // geometry only
        val distance = tile.aerialDistanceTo(origin)
        if (distance > range) return@mapNotNull null
        val neighbors = tile.neighbors.mapNotNull { knowledge[it.position()] }
        val coast = known.land && neighbors.any { rules.terrains[it.surface?.terrain]?.isCoast == true }
        val last = terrains(known).lastOrNull()
        val luxuries = hashSetOf<String>()
        val rings = (0..2).map { radius ->
            tile.getTilesAtDistance(radius).map { surrounding ->
                val observed = knowledge[surrounding.position()]
                when {
                    observed == null -> prior

                    observed.owner != null -> -1f

                    // Native ranking discounts already claimed city tiles.
                    else -> {
                        val resource = observed.resource?.let { rules.tileResources[it] }
                        val newLuxury = observed.resourceType == "Luxury" && resource != null && !player.hasResource(resource) && luxuries.add(resource.name)
                        base(observed) - (if (!observed.land && !coast) 1 else 0) + (if (newLuxury) 10 else 0)
                    }
                }
            }.toList()
        }
        val capital = player.getCapital()?.location
        val cityDistance = home.sumOf { city ->
            AutomationSelection.cityDistanceValue(tile.aerialDistanceTo(map[city.x, city.y]), capital?.x == city.x && capital.y == city.y).toDouble()
        }.toFloat()
        position to AutomationSelection.Settlement(
            distance, cityDistance, coast, known.hill, neighbors.any { it.mountain }, known.river,
            terrains(known).any { terrain -> terrain.getMatchingUniques(UniqueType.RestrictedBuildableImprovements).any { it.params[0] == "All Road" } },
            known.resource != null, known.resourceType == "Bonus",
            known.resourceType == "Luxury" && last?.hasUnique(UniqueType.Vegetation) == true && !last.hasUnique(UniqueType.ProductionBonusWhenRemoved), rings,
        )
    }.toMap()
}
