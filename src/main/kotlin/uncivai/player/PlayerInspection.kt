package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.tile.Tile
import com.unciv.models.stats.Stats
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import uncivai.obj

@Serializable
enum class InspectionSubject { POLITICS, WONDERS, NOTIFICATIONS, REPLAY, CIVILOPEDIA, TILE, UNIT, ROAD }

@Serializable
data class PlayerInspectionRequest(
    val subject: InspectionSubject,
    val target: String? = null,
    val destination: TilePosition? = null,
    val category: String? = null,
    val names: List<String> = emptyList(),
    val search: String? = null,
    val turn: Int? = null,
    val offset: Int = 0,
    val limit: Int = 50,
)

@Serializable
data class PlayerInspection(val epoch: String, val revision: String, val turn: Int, val subject: InspectionSubject, val data: JsonObject)

internal fun inspectPlayer(player: Civilization, state: TurnSessionState, turn: PlayerTurnSnapshot, request: PlayerInspectionRequest): JsonObject {
    val subject = request.subject
    if (request.offset < 0 || request.limit !in 1..100 || request.names.size > 16 || request.names.any { it.isBlank() } ||
        request.search?.length?.let { it > 128 } == true ||
        (request.target != null && subject !in setOf(InspectionSubject.UNIT, InspectionSubject.ROAD, InspectionSubject.TILE)) ||
        (request.destination != null && subject !in setOf(InspectionSubject.UNIT, InspectionSubject.ROAD, InspectionSubject.TILE)) ||
        ((request.category != null || request.names.isNotEmpty() || request.search != null) && subject != InspectionSubject.CIVILOPEDIA) ||
        (request.turn != null && subject !in setOf(InspectionSubject.REPLAY, InspectionSubject.NOTIFICATIONS)) ||
        request.turn?.let { it !in 0..player.gameInfo.turns } == true
    ) {
        inspectionFailure(PlayerSessionFailure.INVALID_ARGUMENT)
    }
    fun tile(): Tile = request.destination?.let { player.gameInfo.tileMap.getOrNull(it.x, it.y) }
        ?.takeIf { it.isExplored(player) } ?: inspectionFailure(PlayerSessionFailure.OPTION_UNAVAILABLE)
    return when (subject) {
        InspectionSubject.POLITICS -> inspectPolitics(player, request)

        InspectionSubject.WONDERS -> inspectWonders(player, request)

        InspectionSubject.NOTIFICATIONS -> inspectNotifications(player, request)

        InspectionSubject.REPLAY -> inspectReplay(player, request)

        InspectionSubject.CIVILOPEDIA -> inspectCivilopedia(player, request)

        InspectionSubject.TILE -> inspectTile(player, tile(), request.target, state)

        InspectionSubject.UNIT -> {
            if ((request.target == null) == (request.destination == null)) inspectionFailure(PlayerSessionFailure.INVALID_ARGUMENT)
            val units = if (request.target != null) {
                listOf(inspectionUnit(player, state, request.target))
            } else {
                tile().takeIf { it.isVisible(player) }?.getUnits()?.filter { it.isVisibleTo(player) }?.toList()
                    ?: inspectionFailure(PlayerSessionFailure.OPTION_UNAVAILABLE)
            }
            obj("units" to units.map { inspectUnit(player, it) })
        }

        InspectionSubject.ROAD -> inspectRoad(
            player,
            inspectionUnit(player, state, request.target),
            tile(),
            canStart = turn.map.units.single { it.handle == request.target }.controls?.canConnectRoad == true,
        )
    }
}

internal fun inspectionUnit(player: Civilization, state: TurnSessionState, target: String?): MapUnit = player.units.getCivUnits().singleOrNull { state.unitHandles[it.id] == target && !it.isDestroyed }
    ?: inspectionFailure(PlayerSessionFailure.UNIT_UNAVAILABLE)

internal fun inspectionFailure(failure: PlayerSessionFailure): Nothing = throw PlayerSessionException(failure)

internal fun inspectionPage(rows: List<JsonObject>, request: PlayerInspectionRequest): JsonObject = obj(
    "items" to rows.drop(request.offset).take(request.limit),
    "total" to rows.size,
    "nextOffset" to (request.offset + request.limit).takeIf { it < rows.size },
)

internal fun HexCoord.inspectionPosition() = obj("x" to x, "y" to y)
internal fun Stats.inspectionStats() = JsonObject(map { (stat, value) -> stat.name to JsonPrimitive(value) }.toMap())
