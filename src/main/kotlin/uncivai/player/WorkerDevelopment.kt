package uncivai.player

import com.unciv.Constants
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerWorkerOperations
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.mapunit.UnitWorkerRepair
import com.unciv.logic.map.mapunit.WorkerImprovementPicker
import kotlinx.serialization.Serializable
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.util.Collections
import java.util.Locale
import kotlin.math.roundToInt

@Serializable
data class WorkerQueuedJob(val name: String, val remainingTurns: Int)

@Serializable
data class WorkerJobOption(
    val name: String,
    val available: Boolean,
    val turns: Int,
    val firstJob: String? = null,
    val prerequisites: List<String> = emptyList(),
    val yieldChanges: Map<String, String> = emptyMap(),
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
    val options = picker.options().filter { it.improvement.name != Constants.cancelImprovementOrder }.map { option ->
        val improvement = option.improvement
        val replaces = !improvement.isRoad() && !improvement.name.startsWith(Constants.remove)
        val stats = tile.stats.getStatDiffForImprovement(improvement, player, tile.getCity())
        stats.add(picker.getMaintenance(improvement))
        WorkerJobOption(
            name = improvement.name,
            available = active && operations.canStartJob(unit, improvement.name),
            turns = if (tile.improvementInProgress == improvement.name) tile.turnsToImprovement else improvement.getTurnsToBuild(player, unit),
            firstJob = option.report.removalImprovement?.name,
            prerequisites = workerDetached(option.report.proposedSolutions.map { it.first }),
            yieldChanges = Collections.unmodifiableMap(
                stats.associate { it.key.name to (it.value * 10).roundToInt() }.filterValues { it != 0 }
                    .mapValues { decimal.format(it.value / 10.0) },
            ),
            resourceProvided = tile.tileResource?.takeIf { player.canSeeResource(it) && it.isImprovedBy(improvement.name) }?.name,
            replaces = tile.improvement.takeIf { replaces },
        )
    }.toMutableList()
    if (UnitWorkerRepair.isShown(unit) && options.none { it.name == Constants.repair }) {
        options.add(
            WorkerJobOption(
                name = Constants.repair,
                available = active && operations.canStartJob(unit, Constants.repair),
                turns = UnitWorkerRepair.turns(unit),
            ),
        )
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
