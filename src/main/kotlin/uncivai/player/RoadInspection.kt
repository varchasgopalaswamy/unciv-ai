package uncivai.player

import com.unciv.Constants
import com.unciv.logic.automation.unit.RoadToAutomation
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerUnitOperations
import com.unciv.logic.copyForPreview
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.tile.RoadStatus
import com.unciv.logic.map.tile.Tile
import kotlinx.serialization.json.JsonObject
import uncivai.obj

internal fun inspectRoad(player: Civilization, unit: MapUnit, destination: Tile, canStart: Boolean): JsonObject {
    val operations = PlayerUnitOperations(player)
    val road = player.tech.getBestRoadAvailable()
    val path = operations.roadPath(unit, destination)
    if (path == null) return obj("available" to false, "reason" to "No native road connection route is available")
    val route = RoadToAutomation(player)
    val visible = path.all { it.isVisible(player) }
    val copy = player.gameInfo.copyForPreview()
    val owner = copy.getCivilization(player.civID)
    fun refresh() {
        owner.cache.updateCitiesConnectedToCapital(true)
        owner.cities.forEach { it.cityStats.update() }
        owner.updateStatsForNextTurn()
    }
    val work = path.filter { it.isVisible(player) && route.shouldBuildRoadOnTile(it) }.map { tile ->
        val previewTile = copy.tileMap[tile.position]
        val repair = player.gameInfo.ruleset.tileImprovements[Constants.repair]
        val jobs = buildList {
            while (previewTile.roadIsPillaged && previewTile.roadStatus != RoadStatus.None && repair != null) {
                add(repair)
                // Repair clears a pillaged ordinary improvement first, then the
                // road. Estimate both jobs without reallocating preview citizens.
                if (previewTile.improvementIsPillaged) {
                    previewTile.improvementIsPillaged = false
                } else {
                    previewTile.roadIsPillaged = false
                }
            }
            if (RoadToAutomation(owner).shouldBuildRoadOnTile(previewTile)) {
                add(checkNotNull(road.improvement(player.gameInfo.ruleset)))
            }
        }
        val steps = jobs.mapIndexed { index, job ->
            val turns = if (index == 0 && tile.improvementInProgress == job.name) tile.turnsToImprovement else job.getTurnsToBuild(player, unit)
            turns to obj("job" to job.name, "workerTurns" to turns)
        }
        // Restore the original road before measuring baseline maintenance and connections.
        previewTile.roadIsPillaged = tile.roadIsPillaged
        previewTile.improvementIsPillaged = tile.improvementIsPillaged
        steps.sumOf { it.first } to obj("position" to tile.position.inspectionPosition(), "jobs" to steps.map { it.second }, "workerTurns" to steps.sumOf { it.first })
    }
    val benefits = if (!visible) {
        null
    } else {
        refresh()
        val before = owner.stats.getStatMapForNextTurn()
        val cityBefore = owner.cities.associate { it.id to it.cityStats.currentCityStats.clone() }
        val connectedBefore = owner.cities.associate { it.id to it.isConnectedToCapital() }
        for (tile in path.filter { route.shouldBuildRoadOnTile(it) }) {
            val completed = copy.tileMap[tile.position]
            if (completed.roadIsPillaged && owner.gameInfo.ruleset.tileImprovements.containsKey(Constants.repair)) {
                while (completed.roadIsPillaged) completed.setRepaired()
            }
            if (RoadToAutomation(owner).shouldBuildRoadOnTile(completed)) completed.setRoadStatus(road, owner)
        }
        refresh()
        val after = owner.stats.getStatMapForNextTurn()
        obj(
            "goldPerTurnChange" to (after.values.sumOf { it.gold.toDouble() } - before.values.sumOf { it.gold.toDouble() }),
            "maintenanceChange" to ((before["Transportation upkeep"]?.gold ?: 0f) - (after["Transportation upkeep"]?.gold ?: 0f)),
            "cities" to owner.cities.map { city ->
                val prior = cityBefore.getValue(city.id)
                val current = city.cityStats.currentCityStats
                obj(
                    "name" to city.name,
                    "connectedBefore" to connectedBefore.getValue(city.id),
                    "connectedAfter" to city.isConnectedToCapital(),
                    "goldPerTurnChange" to current.gold - prior.gold,
                    "productionPerTurnChange" to current.production - prior.production,
                )
            },
        )
    }
    return obj(
        "available" to canStart, "reason" to if (canStart) null else "Road construction cannot be started now", "road" to road.name,
        "destination" to destination.position.inspectionPosition(), "path" to path.map { it.position.inspectionPosition() },
        "tilesOutsideCurrentSight" to path.count { !it.isVisible(player) }, "work" to work.map { it.second },
        "workerTurns" to if (visible) work.sumOf { it.first } else null,
        "completionEffects" to benefits,
        "basis" to "Worker-turns exclude travel and interruptions. Completion effects assume the whole route is built now with current cities and policies; unavailable when any route tile is outside current sight. Native automation may stop early or change route.",
    )
}
