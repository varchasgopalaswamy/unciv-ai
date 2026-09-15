package uncivai

import com.unciv.json.json
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.models.translations.tr
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.VictoryProgress
import java.nio.file.Files
import java.nio.file.Path

object VictoryProgressProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val checkpoint = root.resolve("checkpoint.json")
        if (args[1] == "restore") {
            HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
                val expected = PlayerTurnJson.json.decodeFromString<VictoryProgress>(Files.readString(root.resolve("expected.json")))
                val session = PlayerHost(runtime).bindPlayer("Rome")
                check(session.victoryProgress() == expected)
                check(runtime.executor.turnSessions.getValue("Rome").observedRevision == null)
            }
            return
        }
        HeadlessRuntime.createVanillaGame(root.resolve("initial"), 412L).use { runtime ->
            val game = runtime.game
            val rome = game.getCivilization("Rome")
            val greece = game.getCivilization("Greece")
            val france = game.getCivilization("France")
            for (player in listOf(rome, greece, france)) {
                checkNotNull(UnitSettlement.tryFoundCity(player.units.getCivUnits().single { it.name == "Settler" }))
            }
            check(!rome.knows(greece) && !rome.knows(france))
            runtime.executor.enableCheckpoints(checkpoint)
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val initial = session.victoryProgress()
            check(runtime.executor.turnSessions.getValue("Rome").observedRevision == null)
            check(initial.rivals.isEmpty())
            check(initial.victories.map { it.name }.toSet() == game.getEnabledVictories().keys)
            check(initial.score == rome.calculateTotalScore().toInt())
            check(initial.maxTurns == game.gameParameters.maxTurns)
            check(initial.victories.single { it.name == "Scientific" }.milestones.map { it.status } == listOf("Current", "Incomplete"))
            // Changing an unmet rival's score and private science milestone must not affect this view.
            runtime.executor.accessGame {
                france.addGold(10000)
                france.cities.single().cityConstructions.addBuilding("Apollo Program")
                france.victoryManager.currentsSpaceshipParts["SS Booster"] = 2
            }
            check(session.victoryProgress() == initial)
            runtime.executor.accessGame { rome.diplomacyFunctions.makeCivilizationsMeet(greece) }
            val met = session.victoryProgress()
            check(met.rivals.map { it.civilization } == listOf("Greece"))
            check(
                met.rivals.single().nextMilestones == game.getEnabledVictories().mapValues { (_, victory) ->
                    (greece.victoryManager.getNextMilestone(victory)?.getVictoryScreenButtonHeaderText(false, greece) ?: "Done!").tr()
                },
            )
            runtime.executor.accessGame {
                rome.cities.single().cityConstructions.addBuilding("Apollo Program")
                rome.victoryManager.currentsSpaceshipParts["SS Booster"] = 1
            }
            val advanced = session.victoryProgress()
            val scientific = advanced.victories.single { it.name == "Scientific" }
            check(scientific.milestones.map { it.status } == listOf("Completed", "Current"))
            check(scientific.milestones.last().parts.single { it.name == "SS Booster" }.let { it.completed == 1 && it.required == 3 })
            for (path in advanced.victories) {
                val rules = game.getEnabledVictories().getValue(path.name)
                for ((shown, milestone) in path.milestones.zip(rules.milestoneObjects)) {
                    check(shown.display == milestone.getVictoryScreenButtonHeaderText(milestone.hasBeenCompletedBy(rome), rome).tr())
                }
            }
            val native = json().toJson(game)
            check(session.victoryProgress() == advanced)
            check(json().toJson(game) == native) { "Observation mutated the native game" }
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                val token = api.playerToken("Rome")
                val https = PlayerTurnJson.json.decodeFromString<VictoryProgress>(api.tool(token, "victory_progress").toString())
                check(https == advanced)
                val selected = session.victoryProgress(listOf("Scientific"))
                check(selected.victories.single() == scientific)
                check(selected.rivals.single().nextMilestones.keys == setOf("Scientific"))
                try {
                    session.victoryProgress(listOf("not an enabled victory"))
                    error("Unknown victory accepted")
                } catch (error: PlayerSessionException) {
                    check(error.reason == PlayerSessionFailure.INVALID_ARGUMENT)
                }
            }
            runtime.executor.accessGame { runtime.executor.commitCheckpoint() }
            Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(VictoryProgress.serializer(), advanced))
        }
    }
}
