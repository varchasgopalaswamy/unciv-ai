package uncivai.player

import com.unciv.logic.city.City
import com.unciv.logic.civilization.PlayerPurchaseOperations
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class ConstructionPurchaseOption(
    val name: String,
    val kind: String,
    val description: String,
    val goldCost: Int?,
    val resourceRequirements: Map<String, Int>,
    val requiredTechnologies: List<String>,
    val available: Boolean,
    val unavailableReasons: List<String>,
    val confirmation: String?,
)

@Serializable
data class TilePurchaseOption(
    val position: TilePosition,
    val goldCost: Int,
    val available: Boolean,
    val unavailableReasons: List<String>,
    val confirmation: String?,
)

@Serializable
data class CityPurchases(val constructions: List<ConstructionPurchaseOption>, val tiles: List<TilePurchaseOption>)

internal fun observePurchases(city: City, active: Boolean): CityPurchases {
    val operations = PlayerPurchaseOperations(city.civ)
    fun reasons(values: List<String>): List<String> = frozen(
        (values + if (active) emptyList() else listOf("This player cannot act now")).distinct().map { it.tr() },
    )
    return CityPurchases(
        constructions = frozen(
            operations.constructionOptions(city).map {
                ConstructionPurchaseOption(
                    it.name, it.kind, it.description, it.goldCost,
                    Collections.unmodifiableMap(LinkedHashMap(it.resourceRequirements)), frozen(it.requiredTechnologies),
                    active && it.available, reasons(it.unavailableReasons), it.confirmation?.tr(),
                )
            },
        ),
        tiles = frozen(
            operations.tileOptions(city).map {
                TilePurchaseOption(
                    TilePosition(it.x, it.y),
                    it.goldCost,
                    active && it.available,
                    reasons(it.unavailableReasons),
                    it.confirmation?.tr(),
                )
            },
        ),
    )
}

private fun <T> frozen(values: List<T>): List<T> = Collections.unmodifiableList(ArrayList(values))
