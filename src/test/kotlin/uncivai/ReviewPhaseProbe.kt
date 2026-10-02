package uncivai

import com.unciv.logic.GameStarter
import com.unciv.logic.civilization.PlayerType
import com.unciv.models.metadata.GameSetupInfo
import com.unciv.models.metadata.Player
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path

/** All review slots are usable while the shared phase fences player mutations. */
object ReviewPhaseProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        HeadlessRuntime.createDuel(root.resolve("initial"), 412L).use { runtime ->
            val setup = GameSetupInfo(runtime.game)
            setup.gameParameters.players.add(Player("Egypt", PlayerType.Human))
            GameExecutor(GameStarter.startNewGame(setup)).use { executor ->
                executor.enableCheckpoints(root.resolve("checkpoint.json"))
                val control = OperatorControl(executor)
                val players = listOf("Rome", "Greece", "Egypt").associateWith { PlayerHost(executor).bindPlayer(it) }
                executor.accessGame { game ->
                    game.getCivilization("Rome").diplomacyFunctions.makeCivilizationsMeet(game.getCivilization("Greece"))
                }
                fun claim(civ: String, id: String, purpose: String) = players.getValue(civ).communication(
                    obj("op" to "job_claim", "epoch" to executor.epoch, "job" to id, "purpose" to purpose),
                ).getValue("granted").jsonPrimitive.boolean
                fun rejected(reason: PlayerSessionFailure = PlayerSessionFailure.COMMAND_UNAVAILABLE, block: () -> Unit) {
                    val error = runCatching(block).exceptionOrNull()
                    check(error is PlayerSessionException && error.reason == reason) { "$error" }
                }
                check(!claim("Rome", "too-early", "review"))
                val lease = "review-test"
                control.handle(OperatorRequest("acquire", lease))
                control.handle(OperatorRequest("barrier", lease))
                executor.communicationState.maxConcurrent = 3
                for (civ in players.keys) check(claim(civ, "$civ-review", "review"))
                check(executor.communicationState.jobs.values.count { it.expires > 0 } == 3)
                check(!claim("Rome", "chat-during-review", "communication"))
                check(!claim("Rome", "move-during-review", "gameplay"))

                val rome = players.getValue("Rome")
                val before = rome.turn()
                val chat = obj("op" to "send", "epoch" to executor.epoch, "requestId" to "chat", "to" to "Greece", "text" to "Discuss after reviewing.")
                rejected { rome.communication(chat) }
                rejected {
                    rome.communication(obj("op" to "trade_offer", "epoch" to executor.epoch, "requestId" to "trade", "partner" to "Greece", "give" to emptyList<String>(), "receive" to emptyList<String>()))
                }
                rejected(PlayerSessionFailure.GAME_PAUSED) { rome.execute(TurnCommandRequest("research", executor.epoch, before.revision, TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf(before.researchOptions.first()))) }
                check(rome.turn().revision == before.revision)
                check(executor.handoff == 0L)

                for ((civ, player) in players) player.communication(obj("op" to "job_release", "epoch" to executor.epoch, "job" to "$civ-review"))
                control.handle(OperatorRequest("unbarrier", lease))
                check(!claim("Rome", "too-late", "review"))
                check(claim("Rome", "chat-after-review", "communication"))
                check(rome.communication(chat)["message"] != null)
            }
        }
    }
}
