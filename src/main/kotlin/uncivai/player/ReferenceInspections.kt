package uncivai.player

import com.unciv.Constants
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.tile.Tile
import com.unciv.models.stats.INamed
import com.unciv.models.translations.tr
import com.unciv.ui.screens.civilopediascreen.CivilopediaCategories
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import uncivai.obj
import java.util.Collections

@Serializable
data class UnitStatus(val name: String, val turnsRemaining: Int, val description: String)

// Bundled reference notes carrying mechanics that are not generated from uniques.
// Other custom prose (for example Future Tech or Warrior flavor) remains excluded.
private val mechanicalReferenceNotes = setOf(
    "Improvement/Road",
    "Improvement/Railroad",
    "Improvement/Remove Forest",
    "Improvement/Repair",
    "Wonder/Apollo Program",
)

internal fun observeUnitStatuses(unit: MapUnit): List<UnitStatus> = Collections.unmodifiableList(
    unit.statusMap.values.sortedBy { it.name }.map { status ->
        UnitStatus(status.name, status.turnsLeft, unit.civ.gameInfo.ruleset.unitPromotions[status.name]?.getDescription(emptySet()).orEmpty().tr())
    },
)

internal fun inspectUnit(player: Civilization, unit: MapUnit): JsonObject = obj(
    "name" to unit.name, "displayName" to unit.displayName(),
    "position" to unit.currentTile.position.inspectionPosition(), "health" to unit.health,
    "civilizationId" to unit.civ.takeIf { it === player || player.knows(it) || it.isBarbarian }?.civID,
    "military" to unit.baseUnit.isMilitary, "description" to unit.baseUnit.getShortDescription().tr(),
    "baseMovement" to unit.baseUnit.movement, "strength" to unit.baseUnit.strength,
    "rangedStrength" to unit.baseUnit.rangedStrength, "range" to unit.getRange(),
    "promotions" to unit.promotions.promotions.sorted().map { name -> obj("name" to name, "description" to player.gameInfo.ruleset.unitPromotions[name]?.getDescription(emptySet())?.tr()) },
    "statuses" to observeUnitStatuses(unit).map { obj("name" to it.name, "turnsRemaining" to it.turnsRemaining, "description" to it.description) },
)

internal fun inspectTile(player: Civilization, tile: Tile, cityHandle: String?, state: TurnSessionState): JsonObject {
    // Tile statistics use live improvements. Fogged tiles remain available in map_region,
    // but cannot silently expose their current yields or their foreign city's bonuses.
    if (!tile.isVisible(player)) return obj("position" to tile.position.inspectionPosition(), "visible" to false)
    val city = if (cityHandle == null) {
        tile.getCity()?.takeIf { it.civ === player }
    } else {
        player.cities.singleOrNull { state.cityHandles[it.id] == cityHandle && tile in it.getWorkableTiles() }
            ?: inspectionFailure(PlayerSessionFailure.CITY_UNAVAILABLE)
    }
    return obj(
        "position" to tile.position.inspectionPosition(),
        "visible" to true,
        "city" to city?.name,
        "yields" to tile.stats.getTileStats(city, player).inspectionStats(),
        "yieldBreakdown" to tile.stats.getTileStatsBreakdown(city, player).map { (source, yields) -> obj("source" to source.tr(), "yields" to yields.inspectionStats()) },
        "defensePercent" to tile.getDefensiveBonus() * 100,
        "freshWater" to (tile.isLand && tile.isAdjacentTo(Constants.freshWater)),
    )
}

internal fun inspectCivilopedia(player: Civilization, request: PlayerInspectionRequest): JsonObject {
    val rules = player.gameInfo.ruleset
    val category = request.category?.let { name -> CivilopediaCategories.fromLink(name) ?: inspectionFailure(PlayerSessionFailure.INVALID_ARGUMENT) }
    val entries = (category?.let { listOf(it) } ?: CivilopediaCategories.entries).flatMap { group ->
        group.getCategoryIterator(rules, null).map { group to it }
    }.filter { (group, entry) ->
        val name = (entry as? INamed)?.name ?: entry.makeLink().substringAfter('/')
        (request.names.isEmpty() || name in request.names || entry.makeLink() in request.names) &&
            (request.search == null || (name + " " + group.label).contains(request.search, ignoreCase = true))
    }.sortedWith(compareBy({ it.first.name }, { (it.second as? INamed)?.name ?: it.second.makeLink() }))
    val page = entries.drop(request.offset).take(request.limit).map { (group, entry) ->
        val name = (entry as? INamed)?.name ?: entry.makeLink().substringAfter('/')
        // Only selected entries include text. Preserve bundled gameplay notes such
        // as river penalties and road movement costs without restoring flavor prose.
        val includeNotes = group in setOf(CivilopediaCategories.Tutorial, CivilopediaCategories.Terrain, CivilopediaCategories.UnitType) ||
            entry.makeLink() in mechanicalReferenceNotes
        val lines = if (request.names.isEmpty()) {
            null
        } else {
            (entry.getCivilopediaTextLines(rules) + if (includeNotes) entry.civilopediaText else emptyList())
                .filter { it.text.isNotBlank() }.map { line -> obj("text" to line.text.tr(), "link" to line.link.takeIf { it.isNotBlank() }) }
        }
        obj("name" to name, "category" to group.name, "link" to entry.makeLink(), "lines" to lines)
    }
    return obj(
        "items" to page,
        "total" to entries.size,
        "nextOffset" to (request.offset + request.limit).takeIf { it < entries.size },
        "categories" to CivilopediaCategories.entries.map { it.name },
        "basis" to "Public ruleset reference. These entries do not indicate which civilizations, units or resources exist in this game.",
    )
}
