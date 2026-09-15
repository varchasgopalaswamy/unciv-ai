package uncivai.player

import com.unciv.json.json
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.Notification
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.util.Collections

/** An ordinary message in this player's UI, with locations carried by the notification itself. */
@Serializable
data class PlayerNotification(
    val handle: String,
    val text: String,
    val category: String,
    val icons: List<String>,
    val locations: List<TilePosition>,
    val canDismiss: Boolean,
)

internal fun observeNotifications(player: Civilization, state: TurnSessionState, canDismiss: Boolean): List<PlayerNotification> {
    state.notificationHandles.keys.removeIf { old -> player.notifications.none { it === old } }
    state.dismissedNotifications.retainAll(state.notificationHandles.values.toSet())
    return frozen(
        player.notifications.mapNotNull { notification ->
            val handle = state.notificationHandles.getOrPut(notification) { state.identifiers.next(PlayerIdentifier.NOTIFICATION) }
            if (handle in state.dismissedNotifications) return@mapNotNull null
            PlayerNotification(handle, notification.text.tr(), notification.category.name, frozen(notification.icons), notification.locations(), canDismiss)
        },
    )
}

private fun Notification.locations(): List<TilePosition> {
    // Use the engine's notification serialization contract: action fields are private.
    // Project only event locations; never expose engine unit IDs, arbitrary action payloads,
    // or look up hidden tiles/units behind these UI links.
    // Include defaults so a link to (0, 0) is not mistaken for an action without a location.
    val serialized = Json.parseToJsonElement(json().apply { setUsePrototypes(false) }.toJson(this)).jsonObject
    return frozen(
        serialized["actions"]?.jsonArray.orEmpty().mapNotNull { entry ->
            val action = entry.jsonObject
            val location = when {
                "LocationAction" in action -> action.getValue("LocationAction").jsonObject["location"]
                "MapUnitAction" in action -> action.getValue("MapUnitAction").jsonObject["location"]
                "CityAction" in action -> action.getValue("CityAction").jsonObject["city"]
                "PromoteUnitAction" in action -> action.getValue("PromoteUnitAction").jsonObject["location"]
                else -> null
            }?.jsonObject ?: return@mapNotNull null
            // Native JSON omits default zero coordinates.
            TilePosition(location["x"]?.jsonPrimitive?.intOrNull ?: 0, location["y"]?.jsonPrimitive?.intOrNull ?: 0)
        }.distinct(),
    )
}

internal fun PlayerNotification.detached() = copy(icons = frozen(icons), locations = frozen(locations))

private fun <T> frozen(values: Collection<T>): List<T> = Collections.unmodifiableList(values.toList())
