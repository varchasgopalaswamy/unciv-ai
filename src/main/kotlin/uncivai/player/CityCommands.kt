package uncivai.player

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

@Serializable
data class OwnedCitiesSnapshot(
    val epoch: String,
    val cities: List<OwnedCitySummary>,
)

@Serializable
data class OwnedCitySummary(
    val handle: String,
    val name: String,
)

object PlayerCommandJson {
    private val json = Json {
        explicitNulls = true
    }

    fun encode(snapshot: OwnedCitiesSnapshot): String = json.encodeToString(OwnedCitiesSnapshot.serializer(), snapshot)

    fun encode(snapshot: FreeGreatPersonSnapshot): String = json.encodeToString(FreeGreatPersonSnapshot.serializer(), snapshot)
}
