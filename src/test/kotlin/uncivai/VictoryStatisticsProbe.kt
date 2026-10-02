package uncivai

import com.unciv.json.json
import com.unciv.logic.VictoryData
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.ui.screens.victoryscreen.RankingType
import kotlinx.serialization.json.jsonObject
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.VictoryProgress
import uncivai.player.VictoryProgressRequest
import java.nio.file.Files
import java.nio.file.Path

object VictoryStatisticsProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        HeadlessRuntime.createVanillaGame(root.resolve("game"), 412L).use { runtime ->
            for (player in runtime.game.civilizations.filter { it.isMajorCiv() }) {
                checkNotNull(UnitSettlement.tryFoundCity(player.units.getCivUnits().single { it.name == "Settler" }))
            }
            val session = PlayerHost(runtime).bindPlayer("Rome")
            when (args[1]) {
                "statistics" -> statistics(runtime, session, root)
                "privacy" -> privacy(runtime, session)
                "settings" -> settings(runtime, session)
                "score" -> score(runtime, session, root)
            }
        }
    }

    private fun history(runtime: HeadlessRuntime) = runtime.executor.accessGame { game ->
        game.turns = 120
        for ((index, civ) in game.civilizations.filter { it.isMajorCiv() }.withIndex()) {
            civ.statsHistory.clear()
            for (turn in 0..119) {
                if (civ.civName == "Greece" && turn == 10) continue
                civ.statsHistory[turn] = RankingType.entries.filterNot {
                    civ.civName == "France" && turn == 24 && it == RankingType.Growth
                }.associateWith { metric ->
                    when (metric) {
                        RankingType.Production -> 20 + 100 * index + 2 * turn
                        RankingType.Gold -> 50
                        else -> index * 100 + turn
                    }
                }
            }
        }
    }

    private fun meet(runtime: HeadlessRuntime, vararg names: String) = runtime.executor.accessGame { game ->
        val rome = game.getCivilization("Rome")
        for (name in names) rome.diplomacyFunctions.makeCivilizationsMeet(game.getCivilization(name))
    }

    private fun statistics(runtime: HeadlessRuntime, session: PlayerSession, root: Path) {
        history(runtime)
        meet(runtime, "Greece", "France")
        val game = runtime.game
        val before = json().toJson(game)
        val progress = session.victoryProgress()
        check(progress.charts == null && progress.demographics == null)
        val metrics = RankingType.filteredEntries(game.gameParameters).filterNot { it == RankingType.Score }
        check(progress.metrics.map { it.name } == metrics.map { it.name })
        val rankings = checkNotNull(progress.rankings)
        check(rankings.rows.map { it.civilization } == listOf("France", "Greece", "Rome"))
        check(rankings.comparedCivilizations == 3)
        for (row in rankings.rows) {
            val civ = game.getCivilization(row.civilization)
            check(row.values == metrics.map { civ.getStatForRanking(it) })
            val trend = checkNotNull(row.trend)
            check(trend.fromTurn == 109 && trend.toTurn == 119)
            check(trend.changes[progress.metrics.indexOfFirst { it.name == "Production" }] == 20)
            check(trend.changes[progress.metrics.indexOfFirst { it.name == "Gold" }] == 0)
        }
        val subset = session.victoryProgress(VictoryProgressRequest(sections = listOf("rankings"), civilizations = listOf("Rome")))
        check(subset.rankings!!.rows.single() == rankings.rows.single { it.civilization == "Rome" })
        check(subset.rankings.comparedCivilizations == 3 && subset.victories.isEmpty() && subset.rivals.isEmpty())
        check(progress.victories.single { it.name == "Cultural" }.milestones.first().objectives.size == game.ruleset.policyBranches.size)

        val query = VictoryProgressRequest(sections = listOf("charts"), metrics = listOf("Growth", "Production"), fromTurn = 0, toTurn = 120)
        var start = 0
        val collected = mutableListOf<Int>()
        do {
            val page = session.victoryProgress(query.copy(fromTurn = start))
            val chart = checkNotNull(page.charts)
            check(page.rankings == null && page.victories.isEmpty())
            check(page.metrics.map { it.name } == listOf("Growth", "Production"))
            check(chart.turns.size <= 50)
            check(chart.series.sumOf { it.history.sumOf { row -> row.size } + it.currentValues.orEmpty().size } <= 2000)
            collected.addAll(chart.turns)
            if (10 in chart.turns) check(chart.series.single { it.civilization == "Greece" }.history[chart.turns.indexOf(10)] == listOf(null, null))
            if (24 in chart.turns) check(chart.series.single { it.civilization == "France" }.history[chart.turns.indexOf(24)][0] == null)
            if (120 in chart.turns) {
                check(chart.currentTurn == 120)
                for (series in chart.series) {
                    val civ = game.getCivilization(series.civilization)
                    check(series.history.last() == listOf(null, null))
                    check(series.currentValues == listOf(civ.getStatForRanking(RankingType.Growth), civ.getStatForRanking(RankingType.Production)))
                }
            } else {
                check(chart.currentTurn == null && chart.series.all { it.currentValues == null })
            }
            start = chart.nextFromTurn ?: -1
        } while (start >= 0)
        check(collected == (0..120).toList())
        check(session.victoryProgress(VictoryProgressRequest(sections = listOf("charts"))).charts!!.turns == (101..120).toList())
        check(json().toJson(game) == before) { "Statistics observation changed the native save" }
        check(runtime.executor.turnSessions.getValue("Rome").observedRevision == null)
        check(runCatching { (rankings.rows as MutableList).clear() }.exceptionOrNull() is UnsupportedOperationException)

        GameplayTestApi(runtime, root.resolve("tls")).use { api ->
            val body = PlayerTurnJson.json.encodeToJsonElement(VictoryProgressRequest.serializer(), query).jsonObject
            val actual = PlayerTurnJson.json.decodeFromString<VictoryProgress>(api.tool(api.playerToken("Rome"), "victory_progress", body).toString())
            check(actual == session.victoryProgress(query))
        }
        val checkpoint = root.resolve("checkpoint.json")
        runtime.executor.enableCheckpoints(checkpoint)
        runtime.executor.accessGame { runtime.executor.commitCheckpoint() }
        Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(VictoryProgress.serializer(), progress))
        // A separate process covers restoration of native history and permitted trends.
    }

    private fun privacy(runtime: HeadlessRuntime, session: PlayerSession) {
        history(runtime)
        meet(runtime, "Greece")
        val game = runtime.game
        check(!game.getCivilization("Rome").knows(game.getCivilization("France")))
        val query = VictoryProgressRequest(sections = listOf("rankings", "charts"), fromTurn = 100, toTurn = 120)
        val initial = session.victoryProgress(query)
        check(initial.rankings!!.rows.map { it.civilization } == listOf("Greece", "Rome"))
        check(initial.charts!!.series.none { it.civilization == "France" })
        runtime.executor.accessGame {
            val france = it.getCivilization("France")
            france.addGold(100000)
            france.statsHistory[110] = RankingType.entries.associateWith { 123456 }
        }
        check(session.victoryProgress(query) == initial) { "Unmet rival altered numeric output or ordering" }
        rejected { session.victoryProgress(query.copy(civilizations = listOf("France"))) }
        rejected { session.victoryProgress(query.copy(civilizations = listOf("not a civilization"))) }
        val own = VictoryProgressRequest(names = listOf("Domination"), sections = listOf("our_status"))
        val objectives = session.victoryProgress(own).victories.single().milestones.single().objectives
        val franceCapital = game.getCivilization("France").getCapital()!!
        check(!game.getCivilization("Rome").hasExplored(franceCapital.getCenterTile()))
        check(objectives.any { it.name == null } && objectives.none { it.name == franceCapital.name })
        runtime.executor.accessGame {
            it.gameParameters.randomNumberOfPlayers = true
            it.gameParameters.maxNumberOfPlayers = 8
        }
        val hiddenCount = session.victoryProgress(own).victories.single().milestones.single()
        check(hiddenCount.objectives.none { it.name == null } && hiddenCount.objectives.size < objectives.size)
        check("/?" in hiddenCount.display)
        meet(runtime, "France")
        val met = session.victoryProgress(query)
        check(met.charts!!.series.single { it.civilization == "France" }.history[10].all { it == 123456 })
        runtime.executor.accessGame { it.gameParameters.hideOtherCivilizationStats = true }
        val concealed = session.victoryProgress(query)
        check(concealed.rankings!!.rows.single().civilization == "Rome")
        check(concealed.charts!!.series.single().civilization == "Rome")
        runtime.executor.accessGame {
            it.getCivilization("Greece").addGold(10000)
            it.getCivilization("Greece").statsHistory.clear()
        }
        check(session.victoryProgress(query) == concealed)
        runtime.executor.accessGame {
            it.gameParameters.hideOtherCivilizationStats = false
            it.getCivilization("France").cities = emptyList()
        }
        val defeated = session.victoryProgress(query)
        val row = defeated.rankings!!.rows.single { it.civilization == "France" }
        check(row.defeated && row.values.all { it == null } && row.ranks.all { it == null } && row.trend == null)
        check(defeated.charts!!.series.single { it.civilization == "France" }.let { it.history[10].all { v -> v == 123456 } && it.currentValues == null })
    }

    private fun score(runtime: HeadlessRuntime, session: PlayerSession, root: Path) {
        history(runtime)
        meet(runtime, "Greece", "France")
        runtime.executor.accessGame { it.gameParameters.showDemographics = true }
        val sections = listOf("our_status", "global_status", "rankings", "charts", "demographics")
        val query = VictoryProgressRequest(sections = sections)
        val initial = session.victoryProgress(query)
        val encoded = PlayerTurnJson.json.encodeToJsonElement(VictoryProgress.serializer(), initial).jsonObject
        check("score" !in encoded && !encoded.toString().contains("\"Score\""))
        check(initial.metrics.any { it.name == "Technologies" } && initial.metrics.any { it.name == "Force" })
        check(initial.rankings != null && initial.charts != null && initial.demographics != null)
        // Alter only the native score history: no player-facing values or trends may change.
        runtime.executor.accessGame { game ->
            for (civ in game.civilizations.filter { it.isMajorCiv() }) {
                for (turn in civ.statsHistory.keys.toList()) {
                    civ.statsHistory[turn] = civ.statsHistory.getValue(turn) + (RankingType.Score to 987654)
                }
            }
        }
        check(session.victoryProgress(query) == initial)
        for (section in sections) {
            rejected { session.victoryProgress(query.copy(sections = listOf(section), metrics = listOf("Score"))) }
            rejected { session.victoryProgress(query.copy(sections = listOf(section), metrics = listOf("Production", "Score"))) }
        }
        GameplayTestApi(runtime, root.resolve("tls")).use { api ->
            val arguments = PlayerTurnJson.json.encodeToJsonElement(VictoryProgressRequest.serializer(), query).jsonObject
            check(api.tool(api.playerToken("Rome"), "victory_progress", arguments) == encoded)
        }
    }

    private fun settings(runtime: HeadlessRuntime, session: PlayerSession) {
        val charts = VictoryProgressRequest(sections = listOf("charts"))
        check(session.victoryProgress(charts).availability.getValue("charts").reason == "INSUFFICIENT_HISTORY")
        history(runtime)
        meet(runtime, "Greece")
        runtime.executor.accessGame { it.gameParameters.showCharts = false }
        check(session.victoryProgress().rankings!!.rows.all { it.trend == null })
        check(session.victoryProgress(charts).charts == null)
        runtime.executor.accessGame { it.gameParameters.showCivilizationStats = false }
        val all = VictoryProgressRequest(sections = listOf("our_status", "global_status", "rankings", "charts", "demographics"))
        val disabled = session.victoryProgress(all)
        check(disabled.rankings == null && disabled.charts == null && disabled.demographics == null && disabled.metrics.isEmpty())
        check(disabled.victories.isNotEmpty() && disabled.rivals.single().civilization == "Greece")
        runtime.executor.accessGame {
            it.gameParameters.showCivilizationStats = true
            it.gameParameters.showCharts = true
            it.gameParameters.showRankings = false
            it.gameParameters.showDemographics = true
        }
        val demographics = VictoryProgressRequest(sections = listOf("demographics"), metrics = listOf("Production", "Gold"))
        val demo = session.victoryProgress(demographics)
        check(demo.rankings == null)
        val production = demo.demographics!!.rows.single { it.metric == "Production" }
        check(production.ownValue == 258 && production.ownRank == 3 && production.average == 358)
        check(production.best!!.civilization == null && production.best.value == 458)
        check(production.worst!!.civilization == "Rome" && production.worst.value == 258)
        runtime.executor.accessGame { it.getCivilization("France").addGold(1000000) }
        check(session.victoryProgress(demographics) == demo) { "Demographics exposed a mid-turn treasury change" }
        runtime.executor.accessGame { it.gameParameters.hideOtherCivilizationStats = true }
        check(session.victoryProgress(demographics).let { it.demographics == null && it.availability.getValue("demographics").reason == "OPPONENT_STATISTICS_HIDDEN" })

        for (request in listOf(
            charts.copy(fromTurn = -1), charts.copy(toTurn = 121), charts.copy(fromTurn = 20, toTurn = 19),
            VictoryProgressRequest(fromTurn = 0), VictoryProgressRequest(sections = emptyList()),
            charts.copy(sections = listOf("charts", "charts")), charts.copy(metrics = listOf("Science")),
            charts.copy(metrics = listOf("Gold", "Gold")), charts.copy(metrics = listOf("TilesExplored")),
            charts.copy(names = listOf("not a victory")), charts.copy(civilizations = listOf("Rome", "Rome")),
        )) {
            rejected { session.victoryProgress(request) }
        }
        runtime.executor.accessGame { it.gameParameters.showAdditionalRankingTypes = true }
        check(session.victoryProgress(charts.copy(metrics = listOf("TilesExplored"))).metrics.single().name == "TilesExplored")

        runtime.executor.accessGame {
            it.gameParameters.showCivilizationStats = false
            it.gameParameters.showCharts = false
            it.gameParameters.showDemographics = false
            it.victoryData = VictoryData(it.getCivilization("Rome"), "Time", it.turns)
        }
        val postgame = session.victoryProgress(all)
        check(postgame.metrics.none { it.name == "Score" })
        check(postgame.availability.values.all { it.available })
        check(postgame.rankings!!.rows.map { it.civilization } == listOf("Greece", "Rome"))
        check(postgame.charts!!.series.map { it.civilization } == listOf("Greece", "Rome"))
        check(postgame.demographics != null)
    }

    private fun rejected(action: () -> Unit) {
        val failure = runCatching(action).exceptionOrNull()
        check(failure is PlayerSessionException && failure.reason == PlayerSessionFailure.INVALID_ARGUMENT) { "Expected invalid argument, got $failure" }
    }
}
