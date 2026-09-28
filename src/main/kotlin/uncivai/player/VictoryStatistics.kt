package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.ui.screens.victoryscreen.RankingType
import kotlinx.serialization.Serializable
import java.util.Collections
import kotlin.math.roundToInt

@Serializable
data class VictoryProgressRequest(
    val names: List<String> = emptyList(),
    val sections: List<String> = listOf("our_status", "global_status", "rankings"),
    val metrics: List<String> = emptyList(),
    val civilizations: List<String> = emptyList(),
    val fromTurn: Int? = null,
    val toTurn: Int? = null,
)

@Serializable
data class VictorySectionAvailability(val available: Boolean, val reason: String? = null)

@Serializable
data class VictoryMetric(val name: String, val meaning: String)

@Serializable
data class VictoryTrend(val fromTurn: Int, val toTurn: Int, val changes: List<Int?>)

/** Values, ranks and changes use the shared metric column order. */
@Serializable
data class VictoryRankingRow(
    val civilization: String,
    val defeated: Boolean,
    val values: List<Int?>,
    val ranks: List<Int?>,
    val trend: VictoryTrend?,
)

@Serializable
data class VictoryRankings(
    val comparedCivilizations: Int,
    val scope: String = "Disclosed civilizations only, before the civilization filter. Ties sort by civilization name; defeated civilizations have no current value or rank. Trends compare recorded turns, up to 10 turns apart.",
    val rows: List<VictoryRankingRow>,
)

@Serializable
data class VictoryChartSeries(
    val civilization: String,
    /** One row per chart turn, one column per metric; null means no recorded value. */
    val history: List<List<Int?>>,
    /** Separate from recorded history, including when both refer to the same turn. */
    val currentValues: List<Int>?,
)

@Serializable
data class VictoryCharts(
    val turns: List<Int>,
    val currentTurn: Int?,
    val nextFromTurn: Int?,
    val requestedToTurn: Int,
    val series: List<VictoryChartSeries>,
)

@Serializable
data class VictoryDemographicExtreme(val civilization: String?, val value: Int?)

@Serializable
data class VictoryDemographicRow(
    val metric: String,
    val ownValue: Int?,
    val ownRank: Int?,
    val best: VictoryDemographicExtreme?,
    val average: Int?,
    val worst: VictoryDemographicExtreme?,
)

@Serializable
data class VictoryDemographics(
    val scope: String = "Native demographics aggregates include living major civilizations, including unmet players. Unmet best/worst identities and values are concealed. Values use the latest recorded snapshot, falling back to live values when the metric has no recorded value.",
    val rows: List<VictoryDemographicRow>,
)

internal val victorySections = listOf("our_status", "global_status", "rankings", "charts", "demographics")

internal fun canIdentifyVictoryCivilization(player: Civilization, other: Civilization): Boolean =
    player == other || player.knows(other) || other.isDefeated() || player.isDefeated() || player.isSpectator()

/** Match ordinary tabs; secret keyboard overrides do not grant API access. */
internal fun victorySectionAvailability(player: Civilization): Map<String, VictorySectionAvailability> {
    val game = player.gameInfo
    val parameters = game.gameParameters
    val postGame = game.victoryData != null || player.isSpectator()
    fun available(enabled: Boolean, reason: String) = VictorySectionAvailability(enabled, reason.takeUnless { enabled })
    return Collections.unmodifiableMap(linkedMapOf(
        "our_status" to available(!player.isSpectator(), "SPECTATOR"),
        "global_status" to VictorySectionAvailability(true),
        "rankings" to available(postGame || parameters.showCivilizationStats == true && parameters.showRankings, "STATISTICS_DISABLED"),
        "charts" to when {
            player.isSpectator() && game.civilizations.all { it.statsHistory.size < 2 } -> available(false, "INSUFFICIENT_HISTORY")
            !postGame && (parameters.showCivilizationStats != true || !parameters.showCharts) -> available(false, "STATISTICS_DISABLED")
            !postGame && player.statsHistory.size < 2 -> available(false, "INSUFFICIENT_HISTORY")
            else -> VictorySectionAvailability(true)
        },
        "demographics" to when {
            !postGame && (parameters.showCivilizationStats != true || !parameters.showDemographics) -> available(false, "STATISTICS_DISABLED")
            !postGame && parameters.hideOtherCivilizationStats -> available(false, "OPPONENT_STATISTICS_HIDDEN")
            else -> VictorySectionAvailability(true)
        },
    ))
}

internal fun disclosedStatisticCivilizations(player: Civilization): List<Civilization> = player.gameInfo.civilizations.filter {
    it.isMajorCiv() && canIdentifyVictoryCivilization(player, it) &&
        (it == player || player.gameInfo.victoryData != null || player.isSpectator() || !player.gameInfo.gameParameters.hideOtherCivilizationStats)
}.sortedBy { it.civName }

internal fun validateVictoryRequest(player: Civilization, request: VictoryProgressRequest) {
    fun validNames(values: List<String>, limit: Int, allowed: Set<String>) =
        values.size <= limit && values.distinct().size == values.size && values.all { it in allowed }
    val enabledMetrics = RankingType.filteredEntries(player.gameInfo.gameParameters).map { it.name }.toSet()
    val identifiable = player.gameInfo.civilizations.filter { it.isMajorCiv() && canIdentifyVictoryCivilization(player, it) }.map { it.civName }.toSet()
    val currentTurn = player.gameInfo.turns
    if (!validNames(request.names, 16, player.gameInfo.getEnabledVictories().keys) ||
        request.sections.isEmpty() || !validNames(request.sections, 5, victorySections.toSet()) ||
        !validNames(request.metrics, 11, enabledMetrics) || !validNames(request.civilizations, 64, identifiable) ||
        request.fromTurn?.let { it !in 0..currentTurn } == true ||
        request.toTurn?.let { it !in 0..currentTurn } == true ||
        (request.fromTurn ?: 0) > (request.toTurn ?: currentTurn) ||
        (request.fromTurn != null || request.toTurn != null) && "charts" !in request.sections
    ) throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
}

internal fun selectedVictoryMetrics(player: Civilization, request: VictoryProgressRequest): List<RankingType> =
    RankingType.filteredEntries(player.gameInfo.gameParameters).filter { request.metrics.isEmpty() || it.name in request.metrics }

internal fun victoryMetric(metric: RankingType) = VictoryMetric(metric.name, when (metric) {
    RankingType.Score -> "Native integer game score; not a prediction of who will win."
    RankingType.Population -> "Sum of city population."
    RankingType.Growth -> "Net empire food per turn, rounded."
    RankingType.Production -> "Empire production per turn, rounded; current construction modifiers apply."
    RankingType.Gold -> "Treasury, not gold per turn."
    RankingType.Territory -> "Owned city tiles, including water."
    RankingType.Force -> "Native military rating, including unit evaluation and treasury; not unit count or a combat forecast."
    RankingType.Happiness -> "Current empire happiness."
    RankingType.Technologies -> "Number of researched technologies, not science per turn or their total research cost."
    RankingType.Culture -> "Adopted policies excluding branch-completion policies, not culture per turn."
    RankingType.TilesExplored -> "Explored tile count; only enabled with additional ranking types."
})

internal fun observeVictoryRankings(player: Civilization, metrics: List<RankingType>, request: VictoryProgressRequest): VictoryRankings {
    val disclosed = disclosedStatisticCivilizations(player)
    val living = disclosed.filterNot { it.isDefeated() }
    val values = living.associateWith { civ -> metrics.map { civ.getStatForRanking(it) } }
    val ranks = metrics.indices.map { column ->
        living.sortedWith(compareByDescending<Civilization> { values.getValue(it)[column] }.thenBy { it.civName })
            .withIndex().associate { it.value to it.index + 1 }
    }
    val chartsAvailable = victorySectionAvailability(player).getValue("charts").available
    return VictoryRankings(comparedCivilizations = living.size, rows = immutableVictory(disclosed.filter {
        request.civilizations.isEmpty() || it.civName in request.civilizations
    }.map { civ ->
        val history = civ.statsHistory
        val end = history.keys.filter { it < player.gameInfo.turns }.maxOrNull()
        val start = end?.let { history.keys.filter { t -> t in (it - 10).coerceAtLeast(0) until it }.minOrNull() }
        VictoryRankingRow(
            civ.civName, civ.isDefeated(),
            immutableVictory(metrics.indices.map { values[civ]?.get(it) }),
            immutableVictory(ranks.map { it[civ] }),
            if (chartsAvailable && !civ.isDefeated() && start != null) VictoryTrend(start, end, immutableVictory(metrics.map {
                val before = history[start]?.get(it)
                val after = history[end]?.get(it)
                if (before == null || after == null) null else after - before
            })) else null,
        )
    }))
}

internal fun observeVictoryCharts(player: Civilization, metrics: List<RankingType>, request: VictoryProgressRequest): VictoryCharts {
    val civilizations = disclosedStatisticCivilizations(player).filter { request.civilizations.isEmpty() || it.civName in request.civilizations }
    val requestedEnd = request.toTurn ?: player.gameInfo.turns
    val start = request.fromTurn ?: (requestedEnd - 19).coerceAtLeast(0)
    // Bound both turns and numeric cells. Read successive pages with nextFromTurn.
    val pageSize = minOf(50, (2000 / (metrics.size * civilizations.size.coerceAtLeast(1)) - 1).coerceAtLeast(1))
    val end = minOf(requestedEnd, start + pageSize - 1)
    val turns = (start..end).toList()
    val current = player.gameInfo.turns.takeIf { it in start..end }
    return VictoryCharts(
        immutableVictory(turns), current, (end + 1).takeIf { end < requestedEnd }, requestedEnd,
        immutableVictory(civilizations.map { civ ->
            VictoryChartSeries(civ.civName,
                immutableVictory(turns.map { turn -> immutableVictory(metrics.map { metric -> civ.statsHistory[turn]?.get(metric) }) }),
                if (current != null && !civ.isDefeated() && civ.statsHistory.isNotEmpty()) immutableVictory(metrics.map { civ.getStatForRanking(it) }) else null,
            )
        }),
    )
}

internal fun observeVictoryDemographics(player: Civilization, metrics: List<RankingType>): VictoryDemographics {
    val participants = player.gameInfo.civilizations.filter { it.isMajorCiv() && (it.isAlive() || it == player) }
    fun recorded(civ: Civilization, metric: RankingType): Int =
        (civ.statsHistory[player.gameInfo.turns] ?: civ.statsHistory.maxByOrNull { it.key }?.value)?.get(metric)
            ?: civ.getStatForRanking(metric)
    fun extreme(civ: Civilization, value: Int) = if (canIdentifyVictoryCivilization(player, civ)) {
        VictoryDemographicExtreme(civ.civName, value)
    } else VictoryDemographicExtreme(null, null)
    return VictoryDemographics(rows = immutableVictory(metrics.map { metric ->
        val ranked = participants.map { it to recorded(it, metric) }.sortedByDescending { it.second }
        VictoryDemographicRow(metric.name,
            ranked.singleOrNull { it.first == player && !player.isDefeated() }?.second,
            ranked.indexOfFirst { it.first == player }.takeIf { it >= 0 }?.plus(1),
            ranked.firstOrNull()?.let { extreme(it.first, it.second) },
            ranked.takeIf { it.isNotEmpty() }?.let { (it.sumOf { row -> row.second }.toFloat() / it.size).roundToInt() },
            ranked.lastOrNull()?.let { extreme(it.first, it.second) },
        )
    }))
}

internal fun <T> immutableVictory(values: List<T>): List<T> = Collections.unmodifiableList(values)
