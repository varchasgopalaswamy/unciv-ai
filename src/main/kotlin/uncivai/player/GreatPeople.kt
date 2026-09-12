package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerGreatPersonOperations
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.models.UnitActionType
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class GreatPersonAbility(
    val name: String,
    val kind: String,
    val title: String,
    val description: String,
    val supported: Boolean,
    val available: Boolean,
    val reasons: List<String>,
    val consumesUnit: Boolean,
    val science: Int?,
    val culture: Int?,
    val production: Int?,
    val goldenAgeTurns: Int?,
    val improvement: String?,
)

@Serializable
data class GreatPersonUnit(val description: String, val abilities: List<GreatPersonAbility>)

@Serializable
data class GreatPersonResearchChange(val name: String, val scienceBefore: Int, val scienceAfter: Int, val completed: Boolean)

@Serializable
data class GreatPersonProductionChange(val city: String, val construction: String, val productionBefore: Int, val productionAfter: Int, val completed: Boolean)

@Serializable
data class GreatPersonOutcome(
    val unit: String,
    val ability: String,
    val consumed: Boolean,
    val position: TilePosition,
    val improvementBefore: String?,
    val improvementAfter: String?,
    val goldChange: Int,
    val cultureChange: Int,
    val goldenAgeTurnsBefore: Int,
    val goldenAgeTurnsAfter: Int,
    val research: List<GreatPersonResearchChange>,
    val production: List<GreatPersonProductionChange>,
    val gainedTiles: List<TilePosition>,
)

internal fun observeGreatPerson(player: Civilization, unit: MapUnit, active: Boolean): GreatPersonUnit? {
    if (!unit.isGreatPerson()) return null
    return GreatPersonUnit(
        unit.baseUnit.getShortDescription(),
        frozenGreatPeople(
            PlayerGreatPersonOperations(player).options(unit).map { option ->
                val supported = option.kind != UnitActionType.ConductTradeMission
                val reasons = option.reasons +
                    (if (active) emptyList() else listOf("This player cannot act now")) +
                    (if (supported) emptyList() else listOf("City-state trade missions are not supported by this player API yet"))
                GreatPersonAbility(
                    option.name, option.kind.name, option.title.tr(), option.description.tr(), supported,
                    supported && active && option.available, frozenGreatPeople(reasons.distinct().map { it.tr() }),
                    option.consumesUnit, option.science, option.culture, option.production, option.goldenAgeTurns, option.improvement,
                )
            },
        ),
    )
}

internal fun greatPersonOutcome(request: TurnCommandRequest, before: PlayerTurnSnapshot, after: PlayerTurnSnapshot, cultureChange: Int): GreatPersonOutcome {
    val unit = before.map.units.single { it.handle == request.target }
    val beforeTile = before.map.tiles.single { it.position == unit.position }
    val afterTile = after.map.tiles.single { it.position == unit.position }
    val oldResearch = before.research!!.technologies.associateBy { it.name }
    return GreatPersonOutcome(
        unit.handle, request.selection!!, after.map.units.none { it.handle == unit.handle }, unit.position,
        beforeTile.improvement, afterTile.improvement,
        after.economy!!.treasury - before.economy!!.treasury, cultureChange,
        before.economy.goldenAge.turnsRemaining, after.economy.goldenAge.turnsRemaining,
        frozenGreatPeople(
            after.research!!.technologies.mapNotNull { tech ->
                val old = oldResearch.getValue(tech.name)
                if (old.scienceStored == tech.scienceStored && old.researched == tech.researched) {
                    null
                } else {
                    GreatPersonResearchChange(tech.name, old.scienceStored, tech.scienceStored, !old.researched && tech.researched)
                }
            },
        ),
        frozenGreatPeople(
            before.cities.flatMap { city ->
                val updated = after.cities.singleOrNull { it.handle == city.handle } ?: return@flatMap emptyList()
                city.development!!.constructions.mapNotNull { construction ->
                    val old = construction.productionStored ?: return@mapNotNull null
                    val completed = construction.name !in city.development.builtBuildings && construction.name in updated.development!!.builtBuildings
                    val new = updated.development!!.constructions.firstOrNull { it.name == construction.name }?.productionStored ?: 0
                    if (old == new && !completed) null else GreatPersonProductionChange(city.handle, construction.name, old, new, completed)
                }
            }.distinct(),
        ),
        frozenGreatPeople(
            after.map.tiles.filter { tile ->
                tile.owner != null && tile.owner == afterTile.owner && before.map.tiles.any { it.position == tile.position && it.owner != tile.owner }
            }.map { it.position },
        ),
    )
}

internal fun GreatPersonOutcome.detached() = copy(
    research = frozenGreatPeople(research),
    production = frozenGreatPeople(production),
    gainedTiles = frozenGreatPeople(gainedTiles),
)

private fun <T> frozenGreatPeople(values: List<T>): List<T> = Collections.unmodifiableList(ArrayList(values))
