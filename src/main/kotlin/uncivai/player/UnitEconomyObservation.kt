package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerUnitEconomyOperations
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class UnitUpgradeOption(
    val targetName: String,
    val title: String,
    val description: String,
    val strength: Int,
    val rangedStrength: Int,
    val movement: Int,
    val range: Int,
    val requiredTechnologies: List<String>,
    val goldCost: Int,
    /** Additional resource usage after releasing the original unit's requirements. */
    val additionalResourceRequirements: Map<String, Int>,
    val available: Boolean,
    val reasons: List<String>,
    val confirmation: String? = null,
    val destination: TilePosition? = null,
)

@Serializable
data class UnitPillageOption(
    val improvementName: String,
    val title: String,
    val movementCost: Float,
    val healing: Int,
    val available: Boolean,
    val reasons: List<String>,
    val confirmation: String?,
)

@Serializable
data class UnitDisbandOption(
    val gold: Int,
    val available: Boolean,
    val reasons: List<String>,
    val confirmation: String?,
)

@Serializable
data class UnitEconomy(
    val supported: Boolean,
    val upgrades: List<UnitUpgradeOption>,
    val pillage: UnitPillageOption?,
    val disband: UnitDisbandOption?,
)

internal fun observeUnitEconomy(player: Civilization, unit: MapUnit, active: Boolean): UnitEconomy {
    val operations = PlayerUnitEconomyOperations(player)
    if (!operations.supports(unit)) return UnitEconomy(false, emptyList(), null, null)
    fun reasons(values: List<String>) = frozen((values + if (active) emptyList() else listOf("This player cannot act now")).distinct().map { it.tr() })
    return UnitEconomy(
        supported = true,
        upgrades = frozen(
            operations.upgrades(unit).map {
                UnitUpgradeOption(
                    it.targetName, it.title.tr(), it.description, it.strength, it.rangedStrength, it.movement, it.range,
                    frozen(it.requiredTechnologies), it.goldCost, Collections.unmodifiableMap(LinkedHashMap(it.resourceRequirements)),
                    active && it.available, reasons(it.reasons), it.confirmation?.tr(),
                    it.destination?.takeIf { position -> player.gameInfo.tileMap[position].isExplored(player) }?.let { position -> TilePosition(position.x, position.y) },
                )
            },
        ),
        pillage = operations.pillage(unit)?.let {
            UnitPillageOption(
                it.improvementName,
                it.title.tr(),
                it.movementCost,
                it.healing,
                active && it.available,
                reasons(it.reasons),
                it.confirmation.tr(),
            )
        },
        disband = operations.disband(unit)?.let {
            UnitDisbandOption(it.gold, active && it.available, reasons(it.reasons), it.confirmation.tr())
        },
    )
}

private fun <T> frozen(values: List<T>): List<T> = Collections.unmodifiableList(ArrayList(values))
