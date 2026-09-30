package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.models.ruleset.Building
import com.unciv.models.ruleset.INonPerpetualConstruction
import com.unciv.models.ruleset.unique.Conditionals
import com.unciv.models.ruleset.unique.GameContext
import com.unciv.models.ruleset.unique.IHasUniques
import com.unciv.models.ruleset.unique.UniqueType
import com.unciv.models.ruleset.unit.BaseUnit
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class CivilizationProfile(
    val name: String,
    val ability: CivilizationAbility,
    val uniqueUnits: List<CivilizationConstruction>,
    val uniqueBuildings: List<CivilizationConstruction>,
    val comparisonBasis: String = "Base ruleset stats and inherent effects. Promotions and other bonuses can change unit stats; city construction details give current costs, yields and availability.",
)

@Serializable
data class CivilizationAbility(val name: String, val effects: List<CivilizationEffect>)

@Serializable
data class CivilizationEffect(val text: String, val technologyConditions: List<CivilizationCondition>)

@Serializable
data class CivilizationCondition(val condition: String, val met: Boolean)

@Serializable
data class CivilizationPromotion(val name: String, val effects: List<String>)

@Serializable
data class CivilizationValueComparison<T>(val ordinary: T, val unique: T)

@Serializable
data class CivilizationComparison(
    val baseStats: Map<String, CivilizationValueComparison<Float>>,
    val resourceRequirements: Map<String, CivilizationValueComparison<Float>>,
    val unitType: CivilizationValueComparison<String?>?,
    val requiredTechnologies: CivilizationValueComparison<List<String>>?,
    val requiredBuilding: CivilizationValueComparison<String?>?,
    val requiredNearbyImprovedResources: CivilizationValueComparison<List<String>>?,
    val obsoletedBy: CivilizationValueComparison<List<String>>?,
    val upgradesTo: CivilizationValueComparison<List<String>>?,
    val addedEffects: List<String>,
    val removedEffects: List<String>,
    val addedPromotions: List<String>,
    val removedPromotions: List<String>,
)

@Serializable
data class CivilizationConstruction(
    val name: String,
    val replaces: String?,
    val unitType: String?,
    val baseStats: Map<String, Float>,
    val effects: List<String>,
    val freePromotions: List<CivilizationPromotion>,
    val requiredTechnologies: List<String>,
    val missingTechnologies: List<String>,
    val resourceRequirements: Map<String, Int>,
    val requiredBuilding: String?,
    val requiredNearbyImprovedResources: List<String>,
    val obsoletedBy: List<String>,
    val obsolete: Boolean,
    val upgradesTo: List<String>,
    val comparison: CivilizationComparison?,
)

/** Public rules for the bound civilization, including future unlocks before a city exists.
 * No construction rejection query or foreign game state is needed for this profile.
 */
internal fun observeCivilizationProfile(player: Civilization): CivilizationProfile {
    val rules = player.gameInfo.ruleset
    fun upgrades(item: INonPerpetualConstruction): List<String> = frozen(
        (item as? BaseUnit)?.getUpgradeUnits(GameContext.IgnoreConditionals)
            ?.map { player.getEquivalentUnit(it).name }?.distinct()?.sorted()?.toList().orEmpty(),
    )
    fun construction(item: INonPerpetualConstruction): CivilizationConstruction {
        val unit = item as? BaseUnit
        val building = item as? Building
        val replaces = unit?.replaces ?: building?.replaces
        val ordinary: INonPerpetualConstruction? = if (unit != null) rules.units[replaces] else rules.buildings[replaces]
        val stats = profileStats(item)
        val effects = profileEffects(item)
        val promotions = unit?.promotions.orEmpty().sorted()
        val resources = item.getResourceRequirementsPerTurn(player.state).toSortedMap()
        val technologies = profileTechnologies(item)
        val obsoletedBy = profileObsoletedBy(item)
        val upgradesTo = upgrades(item)
        val obsolete = if (unit != null) {
            player.tech.isObsolete(unit)
        } else {
            item.getMatchingUniques(UniqueType.ObsoleteWith, player.state).any { player.tech.isResearched(it.params[0]) }
        }
        return CivilizationConstruction(
            name = item.name,
            replaces = replaces,
            unitType = unit?.unitType,
            baseStats = frozen(stats),
            effects = frozen(effects),
            freePromotions = frozen(
                promotions.map { name ->
                    CivilizationPromotion(name, frozen(profileEffects(rules.unitPromotions.getValue(name))))
                },
            ),
            requiredTechnologies = technologies,
            missingTechnologies = frozen(technologies.filterNot(player.tech::isResearched)),
            resourceRequirements = frozen(resources),
            requiredBuilding = building?.requiredBuilding,
            requiredNearbyImprovedResources = frozen(building?.requiredNearbyImprovedResources.orEmpty().sorted()),
            obsoletedBy = obsoletedBy,
            obsolete = obsolete,
            upgradesTo = upgradesTo,
            comparison = ordinary?.let {
                val normalEffects = profileEffects(it)
                val normalPromotions = (it as? BaseUnit)?.promotions.orEmpty()
                CivilizationComparison(
                    baseStats = compareProfileStats(profileStats(it), stats),
                    resourceRequirements = compareProfileStats(
                        it.getResourceRequirementsPerTurn(player.state).mapValues { entry -> entry.value.toFloat() },
                        resources.mapValues { entry -> entry.value.toFloat() },
                    ),
                    unitType = profileDifference((it as? BaseUnit)?.unitType, unit?.unitType),
                    requiredTechnologies = profileDifference(profileTechnologies(it), technologies),
                    requiredBuilding = profileDifference((it as? Building)?.requiredBuilding, building?.requiredBuilding),
                    requiredNearbyImprovedResources = profileDifference(
                        frozen((it as? Building)?.requiredNearbyImprovedResources.orEmpty().sorted()),
                        frozen(building?.requiredNearbyImprovedResources.orEmpty().sorted()),
                    ),
                    obsoletedBy = profileDifference(profileObsoletedBy(it), obsoletedBy),
                    upgradesTo = profileDifference(upgrades(it), upgradesTo),
                    addedEffects = frozen(effects.filterNot { effect -> effect in normalEffects }),
                    removedEffects = frozen(normalEffects.filterNot { effect -> effect in effects }),
                    addedPromotions = frozen(promotions.filterNot { name -> name in normalPromotions }),
                    removedPromotions = frozen(normalPromotions.filterNot { name -> name in promotions }.sorted()),
                )
            },
        )
    }
    return CivilizationProfile(
        name = player.civName,
        ability = CivilizationAbility(
            player.nation.uniqueName.tr(),
            frozen(
                player.nation.uniqueObjects.filterNot { it.isHiddenToUsers() }.map { unique ->
                    CivilizationEffect(
                        unique.getDisplayText().tr(),
                        // Evaluate technology gates in the own-civ context. Other
                        // conditions retain their native text and may require city/unit/event state.
                        frozen(
                            unique.modifiers.filter {
                                it.type in setOf(UniqueType.ConditionalTech, UniqueType.ConditionalNoTech) && !it.isHiddenToUsers()
                            }.map { CivilizationCondition(it.getDisplayText().tr(), Conditionals.conditionalApplies(unique, it, player.state)) },
                        ),
                    )
                },
            ),
        ),
        uniqueUnits = frozen(player.cache.uniqueUnits.sortedBy { it.name }.map(::construction)),
        uniqueBuildings = frozen(player.cache.uniqueBuildings.sortedBy { it.name }.map(::construction)),
    )
}

private fun profileEffects(item: IHasUniques): List<String> = (item.uniqueObjects + (item as? BaseUnit)?.type?.uniqueObjects.orEmpty())
    .filterNot { it.isHiddenToUsers() }.map { it.getDisplayText().tr() }.distinct()

private fun profileTechnologies(item: INonPerpetualConstruction): List<String> = frozen(item.requiredTechs().distinct().sorted().toList())

private fun profileObsoletedBy(item: INonPerpetualConstruction): List<String> = frozen(
    (
        if (item is BaseUnit) {
            item.techsThatObsoleteThis()
        } else {
            item.getMatchingUniques(UniqueType.ObsoleteWith, GameContext.IgnoreConditionals).map { it.params[0] }
        }
        ).distinct().sorted().toList(),
)

/** Dotted keys distinguish base yields, percentage bonuses, specialist slots and GP points. */
private fun profileStats(item: INonPerpetualConstruction): Map<String, Float> = buildMap {
    put("productionCost", item.cost.toFloat())
    when (item) {
        is BaseUnit -> {
            put("strength", item.strength.toFloat())
            put("rangedStrength", item.rangedStrength.toFloat())
            put("movement", item.movement.toFloat())
            if (item.isMelee()) {
                put("range", 1f)
            } else if (item.isRanged()) {
                put("range", item.range.toFloat())
            }
            put("interceptRange", item.interceptRange.toFloat())
            put("religiousStrength", item.religiousStrength.toFloat())
        }

        is Building -> {
            put("maintenance", item.maintenance.toFloat())
            put("cityStrength", item.cityStrength.toFloat())
            put("cityHealth", item.cityHealth.toFloat())
            item.forEach { put("yields.${it.key.name}", it.value) }
            item.getStatPercentageBonuses(null).forEach { put("yieldPercent.${it.key.name}", it.value) }
            item.specialistSlots.forEach { (name, count) -> put("specialistSlots.$name", count.toFloat()) }
            item.greatPersonPoints.forEach { (name, count) -> put("greatPersonPoints.$name", count.toFloat()) }
        }
    }
}.filterValues { it != 0f }.toSortedMap()

private fun compareProfileStats(ordinary: Map<String, Float>, unique: Map<String, Float>): Map<String, CivilizationValueComparison<Float>> = frozen(
    (ordinary.keys + unique.keys).sorted().mapNotNull { key ->
        val before = ordinary[key] ?: 0f
        val after = unique[key] ?: 0f
        if (before == after) null else key to CivilizationValueComparison(before, after)
    }.toMap(),
)

private fun <T> profileDifference(ordinary: T, unique: T): CivilizationValueComparison<T>? = if (ordinary == unique) null else CivilizationValueComparison(ordinary, unique)

private fun <T> frozen(values: Collection<T>): List<T> = Collections.unmodifiableList(values.toList())
private fun <K, V> frozen(values: Map<K, V>): Map<K, V> = Collections.unmodifiableMap(LinkedHashMap(values))
