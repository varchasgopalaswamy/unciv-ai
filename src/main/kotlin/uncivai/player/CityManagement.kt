package uncivai.player

import com.unciv.logic.city.City
import com.unciv.logic.city.GreatPersonPointsBreakdown
import com.unciv.logic.civilization.PlayerCityManagementOperations
import com.unciv.models.stats.Stat
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class CityControl(val available: Boolean, val unavailableReasons: List<String>)

@Serializable
data class CityFocusOption(val name: String, val label: String, val action: CityControl)

@Serializable
data class CitySpecialist(
    val name: String,
    val assigned: Int,
    val slots: Int,
    val yields: List<DisplayedYield>,
    val greatPersonPoints: Map<String, Int>,
    val assign: CityControl,
    val unassign: CityControl,
)

@Serializable
data class CityGreatPersonProgress(
    val name: String,
    val pointsPerTurn: Int,
    val empirePoints: Int,
    val threshold: Int,
    val baseSources: List<EconomyAmount>,
    val percentSources: List<EconomyAmount>,
)

@Serializable
data class BuildingSale(
    val name: String,
    val gold: Int,
    val baseMaintenance: Int,
    val description: String,
    val action: CityControl,
    val confirmation: String,
)

@Serializable
data class SharedCityTile(val position: TilePosition, val workingCityHandle: String, val workingCityName: String, val action: CityControl)

@Serializable
data class CityManagement(
    val focusOptions: List<CityFocusOption>,
    val avoidGrowth: CityControl,
    val manualSpecialists: Boolean,
    val specialistControl: CityControl,
    val resetCitizens: CityControl,
    val specialists: List<CitySpecialist>,
    val greatPeople: List<CityGreatPersonProgress>,
    val buildingSales: List<BuildingSale>,
    val sharedTiles: List<SharedCityTile>,
)

/** Only owned-city data. Querying progress must not initialize engine counters or reassign population. */
internal fun observeCityManagement(city: City, active: Boolean, handle: (City) -> String): CityManagement {
    val player = city.civ
    val operations = PlayerCityManagementOperations(player)
    fun control(option: PlayerCityManagementOperations.Action) = CityControl(
        active && option.available,
        frozen((option.unavailableReasons + if (active) emptyList() else listOf("This player cannot act now")).distinct().map { it.tr() }),
    )
    val common = control(operations.control(city))
    val breakdown = GreatPersonPointsBreakdown(city)
    val points = breakdown.sum()
    return CityManagement(
        focusOptions = frozen(operations.focuses(city).map { CityFocusOption(it.name, it.label.tr(), common) }),
        avoidGrowth = common,
        manualSpecialists = city.manualSpecialists,
        specialistControl = control(operations.specialistControl(city)),
        resetCitizens = common,
        specialists = frozen(
            operations.specialists(city).map {
                val specialist = player.gameInfo.ruleset.specialists.getValue(it.name)
                val stats = city.cityStats.getStatsOfSpecialist(it.name)
                CitySpecialist(
                    it.name,
                    it.assigned,
                    it.slots,
                    frozen(stats.filter { (stat, _) -> stat != Stat.Faith || player.gameInfo.isReligionEnabled() }.map { (stat, value) -> DisplayedYield(stat.name, value.toInt().toString()) }),
                    Collections.unmodifiableMap(specialist.greatPersonPoints.toSortedMap()),
                    control(it.assign),
                    control(it.unassign),
                )
            },
        ),
        greatPeople = frozen(
            breakdown.allNames.sorted().filter { it in player.gameInfo.ruleset.units }.map { name ->
                CityGreatPersonProgress(
                    name,
                    points[name],
                    player.greatPeople.greatPersonPointsCounter[name],
                    player.greatPeople.getPointsRequiredForGreatPerson(name),
                    frozen(breakdown.basePoints.filter { it.counter[name] != 0 }.map { EconomyAmount(it.source.tr(), it.counter[name]) }),
                    frozen(breakdown.percentBonuses.filter { it.counter[name] != 0 }.map { EconomyAmount(it.source.tr(), it.counter[name]) }),
                )
            },
        ),
        buildingSales = frozen(operations.sales(city).map { BuildingSale(it.name, it.gold, it.maintenance, it.description, control(it.action), it.confirmation.tr()) }),
        sharedTiles = frozen(
            city.getWorkableTiles().filter { it.isVisible(player) && !it.isCityCenter() }
                .sortedWith(compareBy({ it.position.x }, { it.position.y })).mapNotNull { tile ->
                    val donor = tile.getWorkingCity()?.takeIf { it !== city && it.civ === player && it in player.cities } ?: return@mapNotNull null
                    SharedCityTile(TilePosition(tile.position.x, tile.position.y), handle(donor), donor.name, control(operations.reassignTile(city, tile)))
                }.toList(),
        ),
    )
}

@Serializable
data class SpecialistAssignment(val name: String, val assigned: Int)

@Serializable
data class ManagedCityOutcome(
    val handle: String,
    val name: String,
    val focus: String,
    val avoidGrowth: Boolean,
    val manualSpecialists: Boolean,
    val unassignedPopulation: Int,
    val specialists: List<SpecialistAssignment>,
    val workedTiles: List<TilePosition>,
    val lockedTiles: List<TilePosition>,
    val yields: List<DisplayedYield>,
    val removedBuildings: List<String>,
)

@Serializable
data class CityManagementOutcome(val goldChange: Int, val cities: List<ManagedCityOutcome>)

internal fun cityManagementOutcome(before: PlayerTurnSnapshot, after: PlayerTurnSnapshot): CityManagementOutcome = CityManagementOutcome(
    after.economy!!.treasury - before.economy!!.treasury,
    frozen(
        after.cities.mapNotNull { city ->
            val previous = before.cities.single { it.handle == city.handle }
            if (previous.development == city.development && previous.management == city.management) return@mapNotNull null
            val development = city.development!!
            val management = city.management!!
            ManagedCityOutcome(
                city.handle, city.name, development.focus, development.avoidGrowth, management.manualSpecialists,
                development.unassignedPopulation, frozen(management.specialists.map { SpecialistAssignment(it.name, it.assigned) }),
                frozen(development.tiles.filter { it.worked }.map { it.position }), frozen(development.tiles.filter { it.locked }.map { it.position }),
                development.yields, frozen(previous.development!!.builtBuildings - development.builtBuildings.toSet()),
            )
        },
    ),
)

internal fun CityManagementOutcome.detached(): CityManagementOutcome = copy(
    cities = frozen(cities.map { it.copy(specialists = frozen(it.specialists), workedTiles = frozen(it.workedTiles), lockedTiles = frozen(it.lockedTiles), yields = frozen(it.yields), removedBuildings = frozen(it.removedBuildings)) }),
)

private fun <T> frozen(values: List<T>): List<T> = Collections.unmodifiableList(ArrayList(values))
