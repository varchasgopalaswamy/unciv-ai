package uncivai

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path
import java.util.UUID

object VerbalBetrayalProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val checkpoint = root.resolve("checkpoint.json")
        HeadlessRuntime.createVerbalBetrayalExercise(root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            val sessions = listOf("Rome", "Greece").associateWith { PlayerHost(runtime).bindPlayer(it) }
            val rome = sessions.getValue("Rome")
            val greece = sessions.getValue("Greece")
            for (session in sessions.values) {
                val initial = session.turn()
                check(initial.diplomacy.knownCivilizations.single().atWar.not())
                check(initial.alerts.any { it.kind == "FirstContact" && it.content != null })
                check(initial.scenario!!.name == "DIPLOMACY_MEMORY_EXERCISE")
            }
            check(
                rome.turn().scenario!!.instructions.none {
                    "betray" in it || "reconsider" in it || "DECLARE_WAR" in it
                },
            )
            check(greece.turn().scenario!!.instructions.any { "DECLARE_WAR" in it })
            for ((civ, session) in sessions) {
                val context = session.communication(obj("op" to "state", "epoch" to runtime.executor.epoch)).getValue("context").jsonObject
                val role = context.getValue("scenario").jsonObject.getValue("instructions").jsonArray.map { it.jsonPrimitive.content }
                check(role == session.turn().scenario!!.instructions)
                check(role.any { "DECLARE_WAR" in it } == (civ == "Greece"))
            }
            var sequence = 0
            fun chat(civ: String, op: String, vararg fields: Pair<String, Any?>): JsonObject = sessions.getValue(civ).communication(
                obj(
                    "op" to op,
                    "epoch" to runtime.executor.epoch,
                    "requestId" to "chat${++sequence}",
                    *fields,
                ),
            )

            chat("Rome", "send", "to" to "Greece", "text" to VerbalBetrayalScenario.PACT_TERMS)
            val offer = chat(
                "Rome",
                "offer",
                "participants" to listOf("Rome", "Greece"),
                "kind" to "peace",
                "terms" to VerbalBetrayalScenario.PACT_TERMS,
                "conditions" to VerbalBetrayalScenario.PACT_CONDITIONS,
                "expiresTurn" to 8,
            )["offer"]!!.jsonObject
            val proposal = offer["proposal"]!!.jsonPrimitive.content
            chat("Greece", "accept", "proposal" to proposal, "version" to 1)
            chat(
                "Greece",
                "send",
                "to" to "Rome",
                "text" to "I accept those exact non-aggression terms through turn 8.",
            )
            for (player in runtime.game.civilizations.filter { it.isMajorCiv() }) {
                check(
                    player.diplomacy.values.all {
                        it.trades.isEmpty()
                    },
                ) { "A social pact must not create an engine deal" }
            }
            finishTurn(rome)
            finishTurn(greece)
            finishTurn(rome)
            val before = greece.turn()
            check(before.turn == 1 && before.phase == "ACTIVE")
            val stale =
                TurnCommandRequest(
                    "war",
                    before.epoch,
                    before.revision,
                    TurnCommandKind.DECLARE_WAR,
                    target = "Rome",
                    confirmWar = true,
                )
            check(runCatching { greece.execute(stale) }.exceptionOrNull() is PlayerSessionException)
            chat(
                "Greece",
                "reconsider",
                "proposal" to proposal,
                "version" to 1,
                "reason" to "I deliberately choose to violate my unexpired promise; Rome has not released me.",
            )
            val observed = greece.turn()
            val request = stale.copy(revision = observed.revision)
            val receipt = greece.execute(request)
            check(greece.execute(request) == receipt) { "Retry declared war twice" }
            check(runtime.game.getCivilization("Rome").isAtWarWith(runtime.game.getCivilization("Greece")))
            val events = rome.communication(obj("op" to "state", "epoch" to runtime.executor.epoch))
            check(
                events["offers"]!!.jsonArray.single().jsonObject["terms"]!!.jsonPrimitive.content ==
                    VerbalBetrayalScenario.PACT_TERMS,
            )
            check(
                events["consents"]!!.jsonArray.count {
                    it.jsonObject["decision"]!!.jsonPrimitive.content == "accept"
                } ==
                    2,
            )
            check(events["outcomes"]!!.jsonArray.single().jsonObject["author"]!!.jsonPrimitive.content == "Greece")
            check(rome.turn().scenario!!.phase == "AT_WAR")
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restarted")).use { runtime ->
            check(runtime.game.getCivilization("Rome").isAtWarWith(runtime.game.getCivilization("Greece")))
            val outcome = runtime.executor.communicationState.outcomes.single()
            check(outcome["action"]!!.jsonPrimitive.content == "DECLARE_WAR")
            check(
                PlayerHost(runtime).bindPlayer("Rome").turn().scenario!!.instructions.none {
                    "betray" in it ||
                        "reconsider" in it
                },
            )
        }
    }

    private fun finishTurn(session: PlayerSession) {
        repeat(20) {
            val turn = session.turn()
            val alert = turn.alerts.firstOrNull { it.canAcknowledge }
            val settler = turn.map.units.firstOrNull { it.canFoundCity }
            val city = turn.cities.firstOrNull { it.constructionQueue.isEmpty() }
            fun request(
                kind: TurnCommandKind,
                target: String? = null,
                selection: String? = null,
                queue: List<String> = emptyList(),
            ) = TurnCommandRequest(
                UUID.randomUUID().toString(),
                turn.epoch,
                turn.revision,
                kind,
                target,
                selection,
                queue,
            ).reviewedFor(turn)
            val command = when {
                alert != null -> request(TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle)

                settler != null -> request(TurnCommandKind.FOUND_CITY, settler.handle)

                turn.researchQueue.isEmpty() -> request(
                    TurnCommandKind.SET_RESEARCH_QUEUE,
                    queue = listOf(turn.researchOptions.first()),
                )

                city != null -> request(
                    TurnCommandKind.QUEUE_CONSTRUCTION,
                    city.handle,
                    city.constructionOptions.first(),
                )

                turn.canEndTurn -> request(TurnCommandKind.END_TURN)

                else -> error("Unexpected blocker: ${turn.pending}")
            }
            session.execute(command)
            if (command.kind == TurnCommandKind.END_TURN) return
        }
        error("Turn did not finish")
    }
}
