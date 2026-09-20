package uncivai.player

import com.unciv.logic.battle.CityCombatant
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerAirOperations
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.models.ruleset.unique.UniqueType
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
enum class UnitMission { REBASE, PARADROP, AIR_SWEEP, NUCLEAR_STRIKE }

@Serializable
data class MissionPreviewRequest(val target: String, val mission: UnitMission, val destination: TilePosition)

@Serializable
data class MissionVictim(val position: TilePosition, val name: String, val civilization: String, val isCity: Boolean, val isOwn: Boolean)

@Serializable
data class MissionPreview(
    val epoch: String,
    val revision: String,
    val turn: Int,
    val target: String,
    val mission: UnitMission,
    val destination: TilePosition,
    val available: Boolean,
    val reasons: List<String>,
    val range: Int?,
    val blastRadius: Int,
    val affectedExploredTiles: List<TilePosition>,
    val visibleVictims: List<MissionVictim>,
    val knownWarDeclarations: List<String>,
    val speculative: Boolean,
    val consequences: List<String>,
    val confirmation: String?,
)

@Serializable
data class MissionOption(val mission: UnitMission, val range: Int, val available: Boolean, val reasons: List<String>)

@Serializable
data class AirBase(val position: TilePosition, val kind: String, val name: String, val availableSlots: Int, val canRebase: Boolean)

@Serializable
data class InterceptionStatus(val range: Int, val chancePercent: Int, val remaining: Int, val ready: Boolean)

@Serializable
data class UnitAirStatus(
    val missions: List<MissionOption>,
    val bases: List<AirBase>,
    val transported: Boolean,
    val carrier: String?,
    val carriedUnits: List<String>,
    val interception: InterceptionStatus?,
)

@Serializable
data class MissionOutcome(
    val unit: String,
    val mission: UnitMission,
    val destination: TilePosition,
    val resolution: String?,
    val start: TilePosition,
    val end: TilePosition?,
    val healthBefore: Int,
    val healthAfter: Int?,
    val movementBefore: String,
    val movementAfter: String?,
    val destroyed: Boolean,
    val transported: Boolean?,
    val newWars: List<String>,
    val reports: List<CombatReport>,
)

internal fun TurnCommandKind.mission(): UnitMission? = when (this) {
    TurnCommandKind.REBASE_UNIT -> UnitMission.REBASE
    TurnCommandKind.PARADROP -> UnitMission.PARADROP
    TurnCommandKind.AIR_SWEEP -> UnitMission.AIR_SWEEP
    TurnCommandKind.NUCLEAR_STRIKE -> UnitMission.NUCLEAR_STRIKE
    else -> null
}

internal fun observeMissionPreview(player: Civilization, unit: MapUnit, turn: PlayerTurnSnapshot, request: MissionPreviewRequest): MissionPreview {
    val active = turn.phase == "ACTIVE" && turn.result == null && turn.alerts.none { it.capture != null }
    val tile = player.gameInfo.tileMap.getOrNull(request.destination.x, request.destination.y)
    val preview = tile?.let { PlayerAirOperations(player).preview(unit, PlayerAirOperations.Mission.valueOf(request.mission.name), it) }
    val consequences = when (request.mission) {
        UnitMission.REBASE -> listOf("Uses remaining movement; aircraft in transport move with the carrier and can be lost with it.")
        UnitMission.PARADROP -> listOf("Uses native paradrop movement and attack allowance.")
        UnitMission.AIR_SWEEP -> listOf("Uses an attack even if no interceptor responds. Enemy interception and damage are unknown before execution.")
        UnitMission.NUCLEAR_STRIKE -> listOf("May start wars with affected civilizations, damage friendly units and cities, destroy improvements and create fallout.", "Unseen victims and interception may change legality or outcomes. A successful launch uses the weapon according to its native rules.")
    }
    return MissionPreview(
        turn.epoch, turn.revision, turn.turn, request.target, request.mission, request.destination,
        active && preview?.available == true,
        frozen(if (!active) listOf("This player cannot act now") else preview?.reasons ?: listOf("This mission or destination is unavailable")),
        preview?.range, preview?.blastRadius ?: 0,
        frozen(preview?.affectedTiles?.map { TilePosition(it.position.x, it.position.y) } ?: emptyList()),
        frozen(preview?.visibleVictims?.map { MissionVictim(TilePosition(it.getTile().position.x, it.getTile().position.y), it.getName(), it.getCivInfo().civName, it is CityCombatant, it.getCivInfo() === player) } ?: emptyList()),
        frozen(preview?.knownDeclarations?.map { it.civID } ?: emptyList()),
        preview?.speculative ?: true, frozen(consequences),
        if (request.mission == UnitMission.NUCLEAR_STRIKE) "Launch this nuclear weapon at the specified tile, accepting the listed and native consequences?" else null,
    )
}

internal fun observeUnitAir(player: Civilization, unit: MapUnit, state: TurnSessionState, active: Boolean): UnitAirStatus? {
    val operations = PlayerAirOperations(player)
    val options = operations.options(unit)
    val carries = unit.hasUnique(UniqueType.CarryAirUnits)
    val chance = unit.interceptChance()
    if (options.isEmpty() && !carries && chance == 0) return null
    fun handle(other: MapUnit) = state.unitHandles.getOrPut(other.id) { state.identifiers.next(PlayerIdentifier.UNIT) }
    val bases = if (unit.baseUnit.isAirUnit()) {
        player.cities.map { city ->
            val tile = city.getCenterTile()
            AirBase(
                TilePosition(tile.position.x, tile.position.y),
                "CITY",
                city.name,
                (city.getMaxAirUnits() - tile.airUnits.count { !it.isTransported }).coerceAtLeast(0),
                active && operations.preview(unit, PlayerAirOperations.Mission.REBASE, tile)?.available == true,
            )
        } + player.units.getCivUnits().filter { it.hasUnique(UniqueType.CarryAirUnits) }.map { carrier ->
            val tile = carrier.currentTile
            AirBase(
                TilePosition(tile.position.x, tile.position.y),
                "CARRIER",
                carrier.displayName(),
                carrier.checkCarryCapacity(unit).coerceAtLeast(0),
                active && carrier.canTransport(unit) && operations.preview(unit, PlayerAirOperations.Mission.REBASE, tile)?.available == true,
            )
        }.toList()
    } else {
        emptyList()
    }
    return UnitAirStatus(
        frozen(options.map { MissionOption(UnitMission.valueOf(it.mission.name), it.range, active && it.available, frozen(it.reasons)) }),
        frozen(bases),
        unit.isTransported,
        unit.currentTile.militaryUnit?.takeIf { unit.isTransported && it.civ === player && it.isTransportTypeOf(unit) }?.let(::handle),
        frozen(if (carries) unit.currentTile.airUnits.filter { it.civ === player && it.isTransported && unit.isTransportTypeOf(it) }.map(::handle) else emptyList()),
        if (chance > 0) {
            InterceptionStatus(
                unit.getInterceptionRange(),
                chance,
                (1 + unit.getMatchingUniques(UniqueType.ExtraInterceptionsPerTurn).sumOf { it.params[0].toInt() } - unit.attacksThisTurn).coerceAtLeast(0),
                unit.canIntercept(),
            )
        } else {
            null
        },
    )
}

internal fun missionOutcome(request: TurnCommandRequest, before: PlayerTurnSnapshot, after: PlayerTurnSnapshot, resolution: String?): MissionOutcome {
    val old = before.map.units.single { it.handle == request.target }
    val next = after.map.units.singleOrNull { it.handle == request.target }
    val previousReports = before.combatReports.map { it.id }.toSet()
    fun PlayerTurnSnapshot.wars() = (
        diplomacy.knownCivilizations.filter { it.atWar }.map { it.civilizationId } +
            diplomacy.cityStates.filter { it.atWar }.map { it.civilizationId }
        ).toSet()
    val previousWars = before.wars()
    return MissionOutcome(
        old.handle, request.kind.mission()!!, request.destination!!, resolution, old.position, next?.position,
        old.health, next?.health, old.movement, next?.movement, next == null, next?.air?.transported,
        frozen(after.wars() - previousWars),
        frozen(after.combatReports.filter { it.id !in previousReports }.map { it.detached() }),
    )
}

internal fun MissionOutcome.detached() = copy(newWars = frozen(newWars), reports = frozen(reports.map { it.detached() }))
private fun <T> frozen(values: Collection<T>): List<T> = Collections.unmodifiableList(ArrayList(values))
