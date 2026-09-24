package uncivai.player

import com.unciv.logic.battle.CityCombatant
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerCombatOperations
import com.unciv.logic.civilization.PlayerUnitOrders
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.models.translations.tr
import com.unciv.view.ObservedAttackResult
import com.unciv.view.ObservedCombatReportEntry
import com.unciv.view.ObservedCombatant
import com.unciv.view.ObservedImprovementDestruction
import com.unciv.view.ObservedInterception
import com.unciv.view.ObservedNuclearDetonation
import com.unciv.view.ObservedUnopposedAirSweep
import com.unciv.view.ObservedWithdrawal
import kotlinx.serialization.Serializable
import java.util.Collections
import kotlin.math.roundToInt

@Serializable
data class AttackOption(
    val destination: TilePosition,
    val attackFrom: TilePosition,
    val path: List<TilePosition>,
    val movementCost: Float,
    val defender: String,
    val defenderIsCity: Boolean,
    val attackerStrength: Int,
    val defenderStrength: Int,
    val attackerModifiers: Map<String, Int>,
    val defenderModifiers: Map<String, Int>,
    val minDamageToAttacker: Int,
    val maxDamageToAttacker: Int,
    val minDamageToDefender: Int,
    val maxDamageToDefender: Int,
    val interceptionWarning: String? = null,
    val consequences: CombatConsequences? = null,
)

@Serializable
data class CombatOutcome(
    val attacked: Boolean,
    val damageToAttacker: Int,
    val damageToDefender: Int,
    val attackerHealth: Int,
    val defenderHealth: Int?,
    val attackerDestroyed: Boolean,
    val defenderDestroyed: Boolean,
    val defenderCaptured: Boolean,
    val resolution: String? = null,
)

@Serializable
data class UnitOrderOption(val name: String, val available: Boolean, val unavailableReasons: List<String>)

@Serializable
data class UnitOrderStatus(val action: String?, val due: Boolean, val idle: Boolean, val fortificationTurns: Int, val healingPerTurn: Int)

@Serializable
data class PromotionOption(
    val name: String,
    val description: String,
    val prerequisites: List<String>,
    val acquired: Boolean,
    val experienceCost: Int,
    val available: Boolean,
    val unavailableReasons: List<String>,
)

@Serializable
data class UnitPromotionStatus(val experience: Int, val experienceForNextPromotion: Int, val earnedPromotions: Int, val choices: List<PromotionOption>)

@Serializable
data class UnitCombat(
    val supported: Boolean,
    val strength: Int,
    val rangedStrength: Int,
    val range: Int,
    val attacksRemaining: Int,
    val attacks: List<AttackOption>,
    val order: UnitOrderStatus?,
    val orders: List<UnitOrderOption>,
    val promotions: UnitPromotionStatus?,
)

@Serializable
data class CombatantDescription(val kind: String, val name: String?, val instanceName: String?, val isOwn: Boolean)

/** Player-filtered historical report. IDs reveal no hidden event counts. */
@Serializable
data class CombatReport(
    val id: String,
    val turn: Int,
    val kind: String,
    val text: String?,
    val locations: List<TilePosition>,
    val attacker: CombatantDescription? = null,
    val defender: CombatantDescription? = null,
    val outcome: String? = null,
    val damageReceived: Int? = null,
    val retaliationDamage: Int? = null,
    val attackerDefeatedByDefender: Boolean? = null,
    val showZeroDamage: Boolean? = null,
    val ourUnit: CombatantDescription? = null,
    val enemyUnit: CombatantDescription? = null,
    val ourDamage: Int? = null,
    val enemyDamage: Int? = null,
    val ourDestroyed: Boolean? = null,
    val enemyDestroyed: Boolean? = null,
    val isAttacking: Boolean? = null,
    val weaponType: String? = null,
    val attackingCivilizationName: String? = null,
    val hitOurTerritory: Boolean? = null,
    val improvementName: String? = null,
    val unitType: String? = null,
)

internal fun PlayerCombatOperations.AttackPreview.observation(contexts: PositionContexts = PositionContexts(attacker.getCivInfo())): AttackOption = AttackOption(
    destination = TilePosition(target.position.x, target.position.y),
    attackFrom = TilePosition(attackFrom.position.x, attackFrom.position.y),
    path = frozen(path.map { TilePosition(it.position.x, it.position.y) }),
    movementCost = movementCost,
    defender = defender.getName(),
    defenderIsCity = defender is CityCombatant,
    attackerStrength = attackerStrength.roundToInt(),
    defenderStrength = defenderStrength.roundToInt(),
    attackerModifiers = Collections.unmodifiableMap(LinkedHashMap(attackerModifiers)),
    defenderModifiers = Collections.unmodifiableMap(LinkedHashMap(defenderModifiers)),
    minDamageToAttacker = minDamageToAttacker,
    maxDamageToAttacker = maxDamageToAttacker,
    minDamageToDefender = minDamageToDefender,
    maxDamageToDefender = maxDamageToDefender,
    interceptionWarning = if ((attacker as? com.unciv.logic.battle.MapUnitCombatant)?.unit?.baseUnit?.isAirUnit() == true) "Damage estimates exclude interception, which can stop the strike. Inspect combat reports for actual effects." else null,
    consequences = consequences(contexts),
)

internal fun PlayerCombatOperations.AttackResult.observation() = CombatOutcome(attacked, damageToAttacker, damageToDefender, attackerHealth, defenderHealth, attackerDestroyed, defenderDestroyed, defenderCaptured, resolution?.name)

internal fun observeUnitCombat(player: Civilization, unit: MapUnit, active: Boolean, contexts: PositionContexts = PositionContexts(player)): UnitCombat {
    val combat = PlayerCombatOperations(player)
    val orders = PlayerUnitOrders(player)
    val order = orders.status(unit)
    val promotions = orders.promotions(unit)
    return UnitCombat(
        supported = combat.supportsAttack(unit),
        strength = unit.baseUnit.strength,
        rangedStrength = unit.baseUnit.rangedStrength,
        range = unit.getRange(),
        attacksRemaining = (unit.maxAttacksPerTurn() - unit.attacksThisTurn).coerceAtLeast(0),
        attacks = frozen(if (active) combat.attacks(unit).map { it.observation(contexts) } else emptyList()),
        order = order?.let { UnitOrderStatus(it.action, it.due, it.idle, it.fortificationTurns, it.healingPerTurn) },
        orders = frozen(orders.orders(unit).map { UnitOrderOption(it.order.name, active && it.available, frozen(it.unavailableReasons)) }),
        promotions = promotions?.let { status ->
            UnitPromotionStatus(
                status.experience,
                status.experienceForNextPromotion,
                status.earnedPromotions,
                frozen(
                    status.choices.map {
                        PromotionOption(it.name, it.description, frozen(it.prerequisites), it.acquired, it.experienceCost, active && it.available, frozen(it.unavailableReasons))
                    },
                ),
            )
        },
    )
}

internal fun ObservedCombatant.description() = CombatantDescription(kind.name, name, instanceName, isOwn)

internal fun ObservedCombatReportEntry.observation(id: String): CombatReport {
    val common = CombatReport(id, report.turn, report::class.simpleName!!, text?.tr(), emptyList())
    fun positions(values: List<com.unciv.logic.map.HexCoord>) = frozen(values.map { TilePosition(it.x, it.y) })
    return when (val value = report) {
        is ObservedAttackResult -> common.copy(locations = positions(value.locations), attacker = value.attacker.description(), defender = value.target.description(), outcome = value.outcome.name, damageReceived = value.damageReceived, retaliationDamage = value.retaliationDamage, attackerDefeatedByDefender = value.attackerDefeatedByDefender, showZeroDamage = value.showZeroDamage)
        is ObservedInterception -> common.copy(locations = positions(value.locations), ourUnit = value.ourUnit.description(), enemyUnit = value.enemyUnit.description(), ourDamage = value.ourDamage, enemyDamage = value.enemyDamage, ourDestroyed = value.ourDestroyed, enemyDestroyed = value.enemyDestroyed, isAttacking = value.isAttacking)
        is ObservedWithdrawal -> common.copy(locations = positions(value.locations), attacker = value.attacker.description(), defender = value.target.description(), outcome = "Withdrawn")
        is ObservedImprovementDestruction -> common.copy(locations = positions(value.locations), attacker = value.attacker.description(), improvementName = value.improvementName)
        is ObservedNuclearDetonation -> common.copy(locations = positions(listOfNotNull(value.location)), weaponType = value.weaponType, attackingCivilizationName = value.attackingCivilizationName, hitOurTerritory = value.hitOurTerritory)
        is ObservedUnopposedAirSweep -> common.copy(unitType = value.unitType)
    }
}

internal fun CombatReport.detached(): CombatReport = copy(locations = frozen(locations))

private fun <T> frozen(values: Collection<T>): List<T> = Collections.unmodifiableList(values.toList())
