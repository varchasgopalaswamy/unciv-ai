package uncivai

import com.unciv.models.metadata.BaseRuleset
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import uncivai.player.GameRules
import uncivai.player.PlayerHost
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import java.nio.file.Path
import java.util.UUID

/** Exercises a CLI-created game through HTTPS and checks the native checkpoint after restart. */
object VanillaGameProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        val checkpoint = root.resolve("checkpoint.json")
        val civilizations = listOf("Rome", "Greece", "France")
        val tokens = civilizations.associateWith { it.first().toString().repeat(43) }
        val settlements = mutableMapOf<String, Pair<TurnCommandRequest, TurnCommandReceipt>>()
        val victories = setOf("Scientific", "Cultural", "Domination", "Diplomatic", "Time")
        lateinit var rules: GameRules
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("play")).use { runtime ->
            val game = runtime.game
            val parameters = game.gameParameters
            check(parameters.baseRuleset == BaseRuleset.Civ_V_Vanilla.fullName)
            check(parameters.speed == "Quick" && parameters.difficulty == "Prince")
            check(parameters.startingEra == "Ancient era" && parameters.maxTurns == 330)
            check(parameters.victoryTypes.toSet() == victories)
            check(parameters.mods.isEmpty() && !parameters.isOnlineMultiplayer)
            check(!parameters.noBarbarians && parameters.numberOfCityStates == 0)
            check(game.tileMap.mapParameters.run { name.isEmpty() && seed == -42L && type == "Pangaea" && mapSize.name == "Small" && !noRuins })
            check(game.civilizations.filter { it.isMajorCiv() }.map { it.civID } == civilizations)
            check(game.civilizations.none { it.isCityState })
            check(runtime.executor.communicationState.trades.counterofferLimit == 5)
            for (civilization in civilizations) {
                val native = game.getCivilization(civilization)
                check(native.isHuman() && native.cities.isEmpty() && native.gold == 0)
                check(native.units.getCivUnits().map { it.name }.sorted().toList() == listOf("Settler", "Warrior"))
                check(!native.tech.isResearched("Pottery") && !native.tech.isResearched("Education"))
            }
            GameplayTestApi(runtime, root.resolve("tls"), tokens).use { api ->
                for (civilization in civilizations) {
                    val turn = api.turn(tokens.getValue(civilization))
                    check(turn.turn == 0 && turn.scenario == null && turn.cities.isEmpty())
                    check(turn.map.tiles.isNotEmpty() && turn.map.tiles.size < game.tileMap.tileList.size)
                    check(turn.map.units.all { it.handle.startsWith("unit") })
                    check(turn.rules.victoryTypes.toSet() == victories && turn.rules.maxTurns == 330)
                    val context = api.communicate(tokens.getValue(civilization), obj("op" to "state", "epoch" to turn.epoch)).getValue("context").jsonObject
                    check(context["rules"] == Json.encodeToJsonElement(GameRules.serializer(), turn.rules))
                    rules = turn.rules
                }
                api.acquire()
                repeat(2) { round ->
                    for (civilization in civilizations) {
                        check(api.status().activeCivilization == civilization && api.status().turn == round)
                        val token = checkNotNull(api.activate().playerToken)
                        val turn = api.readAlerts(token)
                        check(turn.phase == "ACTIVE" && turn.scenario == null)
                        if (round == 0) {
                            val settler = turn.map.units.single { it.name == "Settler" }
                            check(settler.canFoundCity)
                            val request = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, TurnCommandKind.FOUND_CITY, settler.handle)
                            val receipt = api.execute(token, request)
                            check(api.execute(token, request) == receipt)
                            settlements[civilization] = request to receipt
                        }
                        api.finishTurn(token)
                    }
                }
                check(api.status().handoff == 6L && api.status().turn == 2 && api.status().activeCivilization == "Rome")
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            check(runtime.game.turns == 2 && runtime.game.currentPlayer == "Rome" && runtime.executor.handoff == 6L)
            val host = PlayerHost(runtime)
            for (civilization in civilizations) {
                val native = runtime.game.getCivilization(civilization)
                check(native.isHuman() && native.cities.size == 1)
                val session = host.bindPlayer(civilization)
                check(session.turn().rules == rules && session.turn().scenario == null)
                val (request, receipt) = settlements.getValue(civilization)
                check(session.completedReceipt(request) == receipt)
            }
            GameplayTestApi(runtime, root.resolve("restart-tls"), tokens).use { api ->
                api.acquire()
                for (civilization in civilizations) {
                    check(api.status().activeCivilization == civilization)
                    api.finishTurn(checkNotNull(api.activate().playerToken))
                }
                check(api.status().turn == 3 && api.status().handoff == 9L && api.status().activeCivilization == "Rome")
            }
        }
    }
}
