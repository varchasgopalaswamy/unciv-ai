package uncivai.player

import com.unciv.logic.civilization.PlayerUnitOrders
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.models.ruleset.unique.UniqueType
import java.util.Collections

/** Comparisons use native costs and healing without applying a promotion to the live game. */
internal fun observePromotions(unit: MapUnit, status: PlayerUnitOrders.PromotionStatus, active: Boolean, recovery: UnitRecovery): UnitPromotionStatus {
    val ruleset = unit.civ.gameInfo.ruleset
    val totalExperience = unit.promotions.totalXpProduced()
    val barbarianLimit = ruleset.modOptions.constants.maxXPfromBarbarians
    return UnitPromotionStatus(
        experience = status.experience,
        experienceForNextPromotion = status.experienceForNextPromotion,
        earnedPromotions = status.earnedPromotions,
        totalExperience = totalExperience,
        barbarianExperienceLimit = barbarianLimit,
        canEarnExperienceFromBarbarians = totalExperience < barbarianLimit,
        recovery = recovery,
        choices = frozen(
            status.choices.map { option ->
                val promotion = ruleset.unitPromotions.getValue(option.name)
                val retained = !promotion.hasUnique(UniqueType.SkipPromotion)
                val available = active && option.available
                // These are newly satisfied prerequisites, not a claim that the successor
                // is immediately affordable or passes every other native condition.
                val unlocks = if (!retained || !available) {
                    emptyList()
                } else {
                    status.choices.filter { next ->
                        !next.acquired && unit.type.name in ruleset.unitPromotions.getValue(next.name).unitTypes &&
                            option.name in next.prerequisites && next.prerequisites.none { it in unit.promotions.promotions }
                    }.map { PromotionUnlock(it.name, it.description) }
                }
                val afterSelection = if (!available) {
                    null
                } else {
                    val future = unit.promotions.clone(unit)
                    future.XP -= option.experienceCost
                    if (!promotion.hasUnique(UniqueType.FreePromotion)) future.numberOfPromotions++
                    val nextCost = future.xpForNextPromotion()
                    PromotionExperience(future.XP, nextCost, (nextCost - future.XP).coerceAtLeast(0))
                }
                PromotionOption(
                    option.name, option.description, frozen(option.prerequisites), option.acquired,
                    option.experienceCost, available, frozen(option.unavailableReasons), retained, frozen(unlocks), afterSelection,
                )
            },
        ),
    )
}

internal fun observeRecovery(unit: MapUnit, eligibleHealingThisTurn: Int): UnitRecovery {
    val missingHealth = (100 - unit.health).coerceAtLeast(0)
    val baseHealing = unit.getHealAmountForCurrentTile()
    // Native healBy applies final bonuses (such as Rejuvenation) and the health cap.
    // Only the detached unit is changed; it is never placed on the live tile.
    val healingPerRestTurn = if (baseHealing <= 0) {
        0
    } else {
        val healed = unit.clone().apply {
            currentTile = unit.currentTile
            setTransients(unit.civ.gameInfo.ruleset)
            healBy(baseHealing)
        }
        (healed.health - unit.health).coerceAtLeast(0)
    }
    val healingThisTurn = if (eligibleHealingThisTurn > 0) healingPerRestTurn else 0
    val turnsToFullHealth = when {
        missingHealth == 0 -> 0
        healingPerRestTurn == 0 -> null
        else -> 1 + (missingHealth - healingThisTurn + healingPerRestTurn - 1) / healingPerRestTurn
    }
    return UnitRecovery(healingThisTurn, healingPerRestTurn, turnsToFullHealth)
}

private fun <T> frozen(values: Collection<T>): List<T> = Collections.unmodifiableList(values.toList())
