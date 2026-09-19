package uncivai

import com.unciv.logic.GameStarter
import com.unciv.logic.civilization.PlayerType
import com.unciv.models.metadata.GameSetupInfo
import com.unciv.models.metadata.Player
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import java.nio.file.Path

object CommunicationProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val oldCredential = "R".repeat(43)
        var rotatedCredential: String? = null
        HeadlessRuntime.createDuel(root.resolve("initial"), 412L).use { runtime ->
            val setup = GameSetupInfo(runtime.game)
            setup.gameParameters.players.add(Player("Egypt", PlayerType.Human))
            val game = GameStarter.startNewGame(setup)
            val executor = GameExecutor(game)
            executor.use {
                executor.enableCheckpoints(root.resolve("checkpoint.json"))
                val control = OperatorControl(executor)
                val players = listOf("Rome", "Greece", "Egypt").associateWith { PlayerHost(executor).bindPlayer(it) }
                val oldManual = control.manualSession(players.getValue("Rome"), oldCredential)
                oldManual.turn()
                var request = 0
                fun command(civ: String, op: String, vararg fields: Pair<String, Any?>): JsonObject = players.getValue(
                    civ,
                ).communication(obj("op" to op, "epoch" to executor.epoch, "requestId" to "r${++request}", *fields))
                fun read(civ: String) = players.getValue(civ).communication(
                    obj(
                        "op" to "read",
                        "epoch" to executor.epoch,
                    ),
                )
                fun state(civ: String) = players.getValue(civ).communication(
                    obj(
                        "op" to "state",
                        "epoch" to executor.epoch,
                    ),
                )
                val contact = players.getValue("Rome").turn().diplomacy
                command("Rome", "send", "to" to "Greece", "text" to "My army is nowhere near your border")
                check(read("Egypt")["nextCursor"]!!.jsonPrimitive.int == 0)
                check(read("Greece")["events"]!!.jsonArray.single().jsonObject["wake"]!!.jsonPrimitive.boolean)
                command("Greece", "send", "text" to "Public hello", "mentions" to listOf("Egypt"))
                check(read("Rome")["events"]!!.jsonArray.last().jsonObject["wake"]!!.jsonPrimitive.boolean.not())
                check(read("Egypt")["events"]!!.jsonArray.single().jsonObject["wake"]!!.jsonPrimitive.boolean)
                check(players.getValue("Rome").turn().diplomacy == contact) // Chat did not manufacture contact.
                val rome = game.getCivilization("Rome")
                val greece = game.getCivilization("Greece")
                rome.diplomacyFunctions.makeCivilizationsMeet(greece)
                rome.addGold(100)
                greece.addGold(100)
                val egyptBefore = players.getValue("Egypt").turn().revision
                val egyptInbox = read("Egypt")
                val economic = command("Rome", "trade_offer", "partner" to "Greece", "give" to emptyList<String>(), "receive" to listOf(obj("type" to "Gold", "name" to "Gold", "amount" to 10, "duration" to -1)))["trade"]!!.jsonObject
                val economicId = economic["proposal"]!!.jsonPrimitive.content
                check(state("Egypt")["trades"]!!.jsonArray.isEmpty())
                fun hidden(id: String) = runCatching { players.getValue("Egypt").communication(obj("op" to "trade_read", "epoch" to executor.epoch, "proposal" to id)) }.exceptionOrNull() as PlayerSessionException
                check(hidden(economicId).reason == hidden("nonexistent").reason)
                command("Greece", "trade_decide", "proposal" to economicId, "version" to 1, "decision" to "accept")
                check(read("Egypt") == egyptInbox)
                check(players.getValue("Egypt").turn().revision == egyptBefore)
                check(state("Egypt")["outcomes"]!!.jsonArray.isEmpty())
                val offered = command(
                    "Rome",
                    "offer",
                    "participants" to listOf("Rome", "Greece"),
                    "kind" to "peace",
                    "terms" to "No attacks",
                    "conditions" to "Unconditional",
                    "expiresTurn" to 10,
                )["offer"]!!.jsonObject
                val proposal = offered["proposal"]!!.jsonPrimitive.content
                check(state("Egypt")["offers"]!!.jsonArray.isEmpty())
                val beforeConsent = players.getValue("Rome").turn()
                command("Greece", "accept", "proposal" to proposal, "version" to 1)
                val afterConsent = players.getValue("Rome").turn()
                check(afterConsent.revision == beforeConsent.revision) { "Social consent changed native action freshness" }
                check(afterConsent.socialAdvisories.single().proposal == proposal)
                check(afterConsent.diplomacy == beforeConsent.diplomacy) { "A social promise changed native diplomatic options" }
                check(players.getValue("Egypt").turn().socialAdvisories.isEmpty())
                val accepted = state("Rome")["consents"]!!.jsonArray.size
                command("Greece", "accept", "proposal" to proposal, "version" to 1)
                check(state("Rome")["consents"]!!.jsonArray.size == accepted)
                command(
                    "Rome",
                    "reconsider",
                    "proposal" to proposal,
                    "version" to 1,
                    "reason" to "They violated the agreement; reconsidering deliberately",
                )
                check(players.getValue("Rome").turn().socialAdvisories.isEmpty())
                command(
                    "Rome",
                    "revise",
                    "proposal" to proposal,
                    "participants" to listOf("Rome", "Greece"),
                    "kind" to "peace",
                    "terms" to "No attacks for five turns",
                    "conditions" to "Unconditional",
                    "expiresTurn" to 5,
                )
                check(runCatching { command("Greece", "accept", "proposal" to proposal, "version" to 1) }.isFailure)
                command("Greece", "accept", "proposal" to proposal, "version" to 2)
                check(players.getValue("Rome").turn().socialAdvisories.single().version == 2)
                command(
                    "Rome",
                    "revise",
                    "proposal" to proposal,
                    "participants" to listOf("Rome", "Greece"),
                    "kind" to "other",
                    "terms" to "Discuss borders; replace the previous promise",
                    "conditions" to "Unconditional",
                )
                check(players.getValue("Rome").turn().socialAdvisories.single().version == 2) {
                    "Proposing new terms must not silently dissolve the accepted peace promise"
                }
                command("Greece", "accept", "proposal" to proposal, "version" to 3)
                check(players.getValue("Rome").turn().socialAdvisories.isEmpty())
                val lease = "test-lease"
                control.handle(OperatorRequest("acquire", lease))
                control.handle(OperatorRequest("barrier", lease))
                val checkpoint = control.handle(OperatorRequest("snapshot", lease)).checkpoint!!
                val nativeGold = game.getCivilization("Rome").gold
                control.handle(OperatorRequest("unbarrier", lease))
                command("Rome", "send", "to" to "Egypt", "text" to "Abandoned timeline")
                val jobs = players.getValue("Rome").communication(
                    obj(
                        "op" to "job_claim",
                        "epoch" to executor.epoch,
                        "job" to "job1",
                        "purpose" to "communication",
                    ),
                )
                check(jobs["granted"]!!.jsonPrimitive.boolean)
                game.getCivilization("Rome").addGold(100)
                game.getCivilization("Greece").addGold(100)
                executor.commitCheckpoint()
                control.handle(OperatorRequest("barrier", lease))
                control.handle(
                    OperatorRequest("restore", leaseId = lease, epoch = "new-epoch", checkpoint = checkpoint),
                )
                check(executor.epoch == "new-epoch")
                check(executor.accessGame { it.getCivilization("Rome").gold } == nativeGold)
                check(executor.communicationState.invocations == 1L)
                check(read("Egypt").toString().contains("Abandoned timeline").not())
                check(executor.isPaused() && executor.communicationPaused)
                check(runCatching { oldManual.turn() }.isFailure)
                check(
                    runCatching {
                        control.handle(OperatorRequest("unbarrier", lease))
                    }.exceptionOrNull() is OperatorFailure,
                )
                val credentials = control.handle(
                    OperatorRequest("rotate_players", leaseId = lease, epoch = executor.epoch),
                )
                rotatedCredential = credentials.players.single { it.civilizationId == "Rome" }.token
                checkNotNull(control.activationSession(checkNotNull(rotatedCredential))).turn()
                control.handle(OperatorRequest("restore_complete", leaseId = lease, epoch = executor.epoch))
            }
        }
        HeadlessRuntime.loadCheckpoint(root.resolve("checkpoint.json"), root.resolve("restarted")).use { runtime ->
            val control = OperatorControl(runtime.executor)
            val old = control.manualSession(PlayerHost(runtime.executor).bindPlayer("Rome"), oldCredential)
            check(runCatching { old.turn() }.isFailure) { "Restart resurrected an abandoned player credential" }
            checkNotNull(control.activationSession(checkNotNull(rotatedCredential))).turn()
            check(runtime.executor.communicationState.invocations == 1L)
        }
    }
}
