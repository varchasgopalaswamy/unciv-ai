package uncivai

import com.unciv.logic.GameInfo
import com.unciv.logic.GameReplayObserver
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.tile.Tile
import com.unciv.view.GameView
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.encodeToJsonElement
import uncivai.player.observation

/** Detached observations taken synchronously under the game executor. */
internal class ReplayCapture(private val game: GameInfo) : GameReplayObserver {
    private val viewers = game.civilizations.filter { it.isMajorCiv() }.sortedBy { it.civID }
    private val tiles = game.tileMap.tileList.sortedWith(compareBy({ it.position.x }, { it.position.y }))
    private val visible = mutableMapOf<String, Set<HexCoord>>()
    private val known = mutableMapOf<String, MutableSet<HexCoord>>()
    private val observed = mutableMapOf<String, MutableMap<HexCoord, JsonObject>>()
    private val events = mutableListOf<JsonObject>()
    private var activeCivilization = game.currentPlayer

    init {
        event("visibilityReset")
        for (viewer in viewers) event("viewStarted", "civilization" to viewer.civID)
        snapshotVisibility()
    }

    private fun position(tile: HexCoord) = obj("x" to tile.x, "y" to tile.y)

    fun tileRevealed(civilization: String, tile: Tile) {
        if (!known.getOrPut(civilization) { mutableSetOf() }.add(tile.position)) return
        event("exploration", "civilization" to civilization,
            "tiles" to listOf(obj("position" to position(tile.position), "terrain" to tile.baseTerrain, "features" to tile.terrainFeatures)))
    }

    fun visibilityChanged(civilization: String) {
        val viewer = viewers.firstOrNull { it.civID == civilization } ?: return
        val prior = visible[civilization].orEmpty()
        val now = viewer.viewableTiles.mapTo(linkedSetOf()) { it.position }
        val added = now - prior
        val removed = prior - now
        if (added.isEmpty() && removed.isEmpty()) return
        val order = compareBy<HexCoord>({ it.x }, { it.y })
        visible[civilization] = now
        val newlyVisible = added.sortedWith(order).map { game.tileMap[it] }
        for (tile in newlyVisible) tileRevealed(civilization, tile)
        event("visibility", "civilization" to civilization,
            "visible" to newlyVisible.map { position(it.position) },
            "fogged" to removed.sortedWith(order).map(::position))
        recordTiles(viewer, newlyVisible)
    }

    /** A fogged tile retains the last observation. Never replace it with current hidden state. */
    private fun recordTiles(viewer: Civilization, tiles: Collection<Tile>) {
        val prior = observed.getOrPut(viewer.civID) { mutableMapOf() }
        val changed = tiles.sortedWith(compareBy({ it.position.x }, { it.position.y })).mapNotNull { tile ->
            val city = tile.getCity()?.takeIf { tile.isCityCenter() }
            val units = tile.getUnits().filter { it.civ === viewer || it.isVisibleTo(viewer) }.sortedBy { it.id }
            val snapshot = obj(
                "position" to position(tile.position), "terrain" to tile.baseTerrain,
                "features" to tile.terrainFeatures, "improvement" to tile.getShownImprovement(viewer),
                "owner" to tile.getOwner()?.civID, "city" to city?.name,
                "units" to units.map { obj("unit" to it.id, "name" to it.name, "owner" to it.owner, "health" to it.health) }.toList(),
            )
            snapshot.takeIf { prior.put(tile.position, it) != it }
        }
        if (changed.isNotEmpty()) event("tiles", "civilization" to viewer.civID, "tiles" to changed)
    }

    override fun turnStarted(civilization: String) {
        for (viewer in viewers) recordTiles(viewer, viewer.viewableTiles)
        activeCivilization = civilization
        event("turnStarted", "civilization" to civilization)
    }

    override fun territoryChanged(tile: Tile) {
        for (viewer in viewers) if (tile.isVisible(viewer)) recordTiles(viewer, listOf(tile))
    }

    /** Record the initial view and remote exploration, including reveals without line of sight. */
    fun snapshotVisibility() {
        for (viewer in viewers) {
            val prior = known.getOrPut(viewer.civID) { mutableSetOf() }
            val discovered = tiles.filter { it.isExplored(viewer) && prior.add(it.position) }
            if (discovered.isNotEmpty()) {
                event("exploration", "civilization" to viewer.civID, "tiles" to discovered.map { tile ->
                    val inSight = tile.isVisible(viewer)
                    if (inSight) obj("position" to position(tile.position), "terrain" to tile.baseTerrain, "features" to tile.terrainFeatures)
                    else obj("position" to position(tile.position))
                })
            }
            visibilityChanged(viewer.civID)
        }
    }

    override fun unitEntered(unit: MapUnit, from: HexCoord?, visibleBefore: Set<String>, transit: List<HexCoord>) {
        val after = viewers.filter { it === unit.civ || unit.isVisibleTo(it) }.map { it.civID }
        event(if (from == null) "unitPlaced" else "unitMoved",
            "unit" to unit.id, "owner" to unit.owner, "name" to unit.name, "from" to from?.let(::position),
            "to" to position(unit.currentTile.position), "transit" to transit.map(::position),
            "visibleBefore" to visibleBefore.sorted(), "visibleAfter" to after)
    }

    override fun unitRemoved(unit: MapUnit, visibleBefore: Set<String>) {
        event("unitRemoved", "unit" to unit.id, "owner" to unit.owner, "name" to unit.name,
            "from" to position(unit.currentTile.position), "visibleBefore" to visibleBefore.sorted())
    }

    override fun attackCompleted(id: String) {
        for (viewer in viewers) {
            val reports = GameView(game, viewer).attackEventsView.getCombatReportEntries()
                .filter { it.id.substringBefore(':') == id }
            for (entry in reports) {
                event("combat", "civilization" to viewer.civID, "report" to Json.encodeToJsonElement(entry.observation(entry.id)))
            }
        }
    }

    fun input(civilization: String, type: String, payload: JsonObject) {
        event("input", "civilization" to civilization, "type" to type, "payload" to payload)
    }

    fun diplomacy(civilization: String, delivered: CommunicationEvent) {
        event("diplomacy", "civilization" to civilization, "delivered" to delivered.recordJson())
    }

    private fun event(kind: String, vararg fields: Pair<String, Any?>) {
        events.add(obj("kind" to kind, "turn" to game.turns, "activeCivilization" to activeCivilization, *fields))
    }

    fun drain(): List<JsonObject> {
        snapshotVisibility()
        for (viewer in viewers) recordTiles(viewer, viewer.viewableTiles)
        return events.toList().also { events.clear() }
    }
}
