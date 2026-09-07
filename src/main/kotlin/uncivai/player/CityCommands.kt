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

@Serializable
data class RenameCityRequest(
    val requestId: String,
    val epoch: String,
    val cityHandle: String,
    val name: String,
)

@Serializable
data class RenameCityReceipt(
    val requestId: String,
    val epoch: String,
    val cityHandle: String,
    val previousName: String,
    val name: String,
)

object PlayerCommandJson {
    private val json = Json {
        explicitNulls = true
    }

    fun encode(snapshot: OwnedCitiesSnapshot): String = json.encodeToString(OwnedCitiesSnapshot.serializer(), snapshot)

    fun encode(request: RenameCityRequest): String = json.encodeToString(RenameCityRequest.serializer(), request)

    fun encode(receipt: RenameCityReceipt): String = json.encodeToString(RenameCityReceipt.serializer(), receipt)

    fun encode(snapshot: FreeGreatPersonSnapshot): String = json.encodeToString(FreeGreatPersonSnapshot.serializer(), snapshot)

    fun encode(request: ChooseFreeGreatPersonRequest): String = json.encodeToString(ChooseFreeGreatPersonRequest.serializer(), request)

    fun encode(receipt: ChooseFreeGreatPersonReceipt): String = json.encodeToString(ChooseFreeGreatPersonReceipt.serializer(), receipt)
}
