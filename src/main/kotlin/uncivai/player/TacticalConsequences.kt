package uncivai.player

import com.unciv.Constants
import com.unciv.logic.battle.CityCombatant
import com.unciv.logic.battle.ICombatant
import com.unciv.logic.battle.MapUnitCombatant
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerCombatOperations
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.logic.map.tile.Tile
import com.unciv.models.ruleset.unique.UniqueType
import kotlinx.serialization.Serializable
import java.util.Collections

/** Proximity at inspection time, not enemy reachability or a guarantee of protection. */
@Serializable
data class PositionContext(
    val position: TilePosition,
    val visibleHostiles: List<NearbyHostile>,
    val ownMilitary: List<MilitaryPresence>,
    val unseenTiles: Int,
    val radius: Int = 2,
)

@Serializable
data class MilitaryPresence(val name: String, val position: TilePosition, val health: Int, val distance: Int)

/** Native exchange bounds include lethal adjustments; interception and triggered effects are separate. */
@Serializable
data class HealthProjection(val current: Int, val remainingMin: Int, val remainingMax: Int, val destructionPossible: Boolean)

@Serializable
data class CombatConsequences(
    val attacker: HealthProjection,
    val defender: HealthProjection,
    val capturePossible: Boolean,
    val attackFrom: PositionContext,
    val mayAdvanceTo: PositionContext?,
    val attackerConsumedOnAttack: Boolean,
)

/** Arrival estimates assume the inspected path is completed without discoveries interrupting it. */
@Serializable
data class MovementConsequences(
    val movementRemaining: Float,
    val attack: String,
    val foundCity: String?,
    val context: PositionContext,
)

internal class PositionContexts(private val player: Civilization) {
    private val hostiles = player.viewableTiles.flatMap { tile ->
        tile.getUnits().filter { it.isVisibleTo(player) && it.baseUnit.isMilitary && player.isAtWarWith(it.civ) }
            .map { tile to VisibleUnit(it.name, it.civ.civName, it.health, military = true, atWar = true) }.toList()
    }
    private val own = player.units.getCivUnits().filter { it.baseUnit.isMilitary }.toList()

    fun at(tile: Tile, excluding: MapUnit? = null): PositionContext = PositionContext(
        tile.position.let { TilePosition(it.x, it.y) },
        frozen(
            hostiles.mapNotNull { (position, unit) ->
                val distance = tile.aerialDistanceTo(position)
                if (distance <= 2) NearbyHostile(TilePosition(position.position.x, position.position.y), unit, distance) else null
            }.sortedWith(compareBy({ it.position.x }, { it.position.y }, { it.unit.name })),
        ),
        frozen(
            own.filter { it !== excluding }.mapNotNull { unit ->
                val distance = tile.aerialDistanceTo(unit.currentTile)
                if (distance <= 2) MilitaryPresence(unit.name, TilePosition(unit.currentTile.position.x, unit.currentTile.position.y), unit.health, distance) else null
            }.sortedWith(compareBy({ it.distance }, { it.position.x }, { it.position.y }, { it.name })),
        ),
        unseenTiles(tile),
    )

    private fun unseenTiles(tile: Tile): Int {
        var count = 0
        tile.forEachTileInDistance(2) { if (!it.isVisible(player)) count++ }
        return count
    }
}

internal fun PlayerCombatOperations.AttackPreview.consequences(contexts: PositionContexts): CombatConsequences {
    fun health(combatant: ICombatant, minDamage: Int, maxDamage: Int): HealthProjection {
        val floor = if (combatant is CityCombatant) 1 else 0
        val current = combatant.getHealth()
        return HealthProjection(current, (current - maxDamage).coerceAtLeast(floor), (current - minDamage).coerceAtLeast(floor), floor == 0 && maxDamage >= current)
    }
    val unit = (attacker as? MapUnitCombatant)?.unit
    val defenderHealth = health(defender, minDamageToDefender, maxDamageToDefender)
    return CombatConsequences(
        health(attacker, minDamageToAttacker, maxDamageToAttacker),
        defenderHealth,
        attacker.isMelee() && (defender.isCivilian() || (defender is CityCombatant && defenderHealth.remainingMin == 1)),
        contexts.at(attackFrom, unit),
        if (attacker.isMelee()) contexts.at(target, unit) else null,
        unit?.hasUnique(UniqueType.SelfDestructs) == true,
    )
}

internal fun movementConsequences(
    player: Civilization,
    unit: MapUnit,
    destination: Tile,
    cost: Float,
    contexts: PositionContexts,
    attacks: List<AttackOption>,
    knowledge: Map<TilePosition, KnownSettlementTile>,
    laterTurn: Boolean = false,
): MovementConsequences {
    val remaining = ((if (laterTurn) unit.getMaxMovement().toFloat() else unit.currentMovement) - cost)
        .coerceAtLeast(0f).let { if (it <= Constants.minimumMovementEpsilon) 0f else it }
    val position = TilePosition(destination.position.x, destination.position.y)
    val attack = when {
        unit.isCivilian() -> "NOT_APPLICABLE"
        remaining == 0f -> "NO_MOVEMENT"
        laterTurn -> "RECHECK_ON_ARRIVAL"
        unit.attacksThisTurn >= unit.maxAttacksPerTurn() -> "NO_ATTACKS_REMAINING"
        attacks.any { it.attackFrom == position } -> "VISIBLE_TARGET_OFFERED"
        else -> "NO_VISIBLE_ATTACK_OFFERED"
    }
    val site = observeSettlement(player, unit, position, active = true, knowledge = knowledge)
    val founding = site?.let {
        when {
            remaining == 0f -> "NO_MOVEMENT"

            it.siteStatus == "BLOCKED" -> "SITE_BLOCKED"

            laterTurn || it.siteStatus == "UNKNOWN" -> "RECHECK_ON_ARRIVAL"

            UnitSettlement.unitBlockers(
                unit.clone().apply {
                    currentTile = destination
                    currentMovement = remaining
                    setTransients(player.gameInfo.ruleset)
                },
            ).isNotEmpty() -> "UNIT_BLOCKED"

            else -> "AVAILABLE"
        }
    }
    return MovementConsequences(remaining, attack, founding, contexts.at(destination, unit))
}

private fun <T> frozen(values: List<T>): List<T> = Collections.unmodifiableList(values)
