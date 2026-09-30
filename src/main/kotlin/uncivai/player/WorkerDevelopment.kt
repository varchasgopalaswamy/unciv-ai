package uncivai.player

import com.unciv.Constants
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerWorkerOperations
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.mapunit.UnitWorkerRepair
import com.unciv.logic.map.mapunit.WorkerImprovementPicker
import com.unciv.logic.map.tile.Tile
import com.unciv.models.stats.Stats
import kotlinx.serialization.Serializable
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.util.Collections
import java.util.Locale
import kotlin.math.roundToInt

@Serializable
data class WorkerQueuedJob(val name: String, val remainingTurns: Int)

@Serializable
data class WorkerCompletionGrant(val afterStep: String, val city: String, val stats: Map<String, Int>)

@Serializable
data class WorkerJobOption(
    val name: String,
    val available: Boolean,
    val totalTurns: Int,
    val steps: List<WorkerQueuedJob>,
    val prerequisites: List<String> = emptyList(),
    val yieldChanges: Map<String, String> = emptyMap(),
    val maintenanceChanges: Map<String, String> = emptyMap(),
    val completionGrants: List<WorkerCompletionGrant> = emptyList(),
    val resourceProvided: String? = null,
    val replaces: String? = null,
)

@Serializable
data class WorkerDevelopment(
    val currentJob: String?,
    val remainingTurns: Int?,
    val queue: List<WorkerQueuedJob>,
    val canStop: Boolean,
    val options: List<WorkerJobOption>,
)

/** Detach only the bound civilization's worker and its occupied tile, using the desktop picker. */
internal fun observeWorkerDevelopment(player: Civilization, unit: MapUnit, active: Boolean): WorkerDevelopment? {
    val operations = PlayerWorkerOperations(player)
    if (!operations.ownsWorker(unit)) return null
    val tile = unit.currentTile
    val picker = WorkerImprovementPicker(unit)
    val decimal = DecimalFormat("0.#", DecimalFormatSymbols(Locale.ROOT))
    fun Stats.observation() = Collections.unmodifiableMap(
        associate { it.key.name to (it.value * 10).roundToInt() }.filterValues { it != 0 }
            .mapValues { decimal.format(it.value / 10.0) },
    )
    fun maintenance(at: Tile) = Stats().apply {
        if (!at.improvementIsPillaged) at.tileImprovement?.let { add(picker.getMaintenance(it, at)) }
        if (!at.roadIsPillaged) at.ruleset.tileImprovements[at.roadStatus.name]?.let { add(picker.getMaintenance(it, at)) }
    }
    fun preview(name: String, prerequisites: List<String> = emptyList()): WorkerJobOption {
        val steps = checkNotNull(operations.plannedQueue(unit, name))
        val future = tile.clone(addUnits = false).apply { setTerrainTransients() }
        val grants = mutableListOf<WorkerCompletionGrant>()
        for (step in steps) {
            if (step.name.startsWith(Constants.remove)) {
                future.improvementFunctions.removalYieldGrant(step.name.removePrefix(Constants.remove), player)?.let { grant ->
                    grants.add(WorkerCompletionGrant(step.name, grant.city.name, Collections.unmodifiableMap(grant.stats.associate { it.key.name to it.value.toInt() })))
                }
            }
            // No civilization argument: simulation must not award production or trigger live effects.
            future.setImprovement(step.name)
        }
        return WorkerJobOption(
            name = name,
            available = active && operations.canStartJob(unit, name),
            totalTurns = steps.sumOf { it.remainingTurns },
            steps = workerDetached(steps.map { WorkerQueuedJob(it.name, it.remainingTurns) }),
            prerequisites = workerDetached(prerequisites),
            yieldChanges = (future.stats.getTileStats(tile.getCity(), player) - tile.stats.getTileStats(tile.getCity(), player)).observation(),
            maintenanceChanges = (maintenance(future) - maintenance(tile)).observation(),
            completionGrants = workerDetached(grants),
            resourceProvided = future.tileResource?.takeIf { player.canSeeResource(it) && future.improvement?.let(it::isImprovedBy) == true && !future.improvementIsPillaged }?.name,
            replaces = tile.improvement.takeIf { it != future.improvement },
        )
    }
    val options = picker.options().filter { it.improvement.name != Constants.cancelImprovementOrder }.map { option ->
        preview(option.improvement.name, option.report.proposedSolutions.map { it.first })
    }.toMutableList()
    if (UnitWorkerRepair.isShown(unit) && options.none { it.name == Constants.repair }) {
        options.add(preview(Constants.repair))
    }
    return WorkerDevelopment(
        currentJob = tile.improvementInProgress,
        remainingTurns = tile.turnsToImprovement.takeIf { tile.improvementInProgress != null },
        queue = workerDetached(operations.queue(unit).map { WorkerQueuedJob(it.name, it.remainingTurns) }),
        canStop = active && operations.canStopJob(unit),
        options = workerDetached(options.sortedBy { it.name }),
    )
}

private fun <T> workerDetached(values: Collection<T>): List<T> = Collections.unmodifiableList(ArrayList(values))
