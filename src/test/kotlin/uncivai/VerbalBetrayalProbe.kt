package uncivai

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path
import java.util.UUID

object VerbalBetrayalProbe {
    private const val PACT_TERMS = "Neither Rome nor Greece will declare war on or attack the other before turn 8."
    private const val PACT_CONDITIONS = "Unconditional. Neither party may end this fixed-term promise unilaterally."

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
                check(initial.scenario == null)
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

            chat("Rome", "send", "to" to "Greece", "text" to PACT_TERMS)
            val offer = chat(
                "Rome",
                "offer",
                "participants" to listOf("Rome", "Greece"),
                "kind" to "peace",
                "terms" to PACT_TERMS,
                "conditions" to PACT_CONDITIONS,
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
            check(before.socialAdvisories.single().let { it.proposal == proposal && it.terms == PACT_TERMS && it.conditions == PACT_CONDITIONS })
            check(before.diplomacy.knownCivilizations.single().declareWar.available)
            val request =
                TurnCommandRequest(
                    "war",
                    before.epoch,
                    before.revision,
                    TurnCommandKind.DECLARE_WAR,
                    target = "Rome",
                    confirmWar = true,
                    socialVersion = Long.MAX_VALUE, // Historical clients cannot turn a chat counter into a rule.
                )
            val receipt = greece.execute(request)
            check(greece.execute(request) == receipt) { "Retry declared war twice" }
            check(runtime.game.getCivilization("Rome").isAtWarWith(runtime.game.getCivilization("Greece")))
            val events = rome.communication(obj("op" to "state", "epoch" to runtime.executor.epoch))
            check(
                events["offers"]!!.jsonArray.single().jsonObject["terms"]!!.jsonPrimitive.content ==
                    PACT_TERMS,
            )
            check(
                events["consents"]!!.jsonArray.count {
                    it.jsonObject["decision"]!!.jsonPrimitive.content == "accept"
                } ==
                    2,
            )
            check(events["outcomes"]!!.jsonArray.single().jsonObject["author"]!!.jsonPrimitive.content == "Greece")
            check(events["offers"]!!.jsonArray.single().jsonObject["effective"]!!.jsonPrimitive.boolean)
            check(events["consents"]!!.jsonArray.all { it.jsonObject["decision"]!!.jsonPrimitive.content == "accept" })
            check(rome.turn().diplomacy.knownCivilizations.single().atWar)
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restarted")).use { runtime ->
            check(runtime.game.getCivilization("Rome").isAtWarWith(runtime.game.getCivilization("Greece")))
            val outcome = runtime.executor.communicationState.outcomes.single()
            check(outcome.action == "DECLARE_WAR")
            check(outcome.confirmation.relationships.keys == setOf("Rome", "Greece"))
            check(outcome.confirmation.relationships.getValue("Rome").let { it.otherCivilizationId == "Greece" && it.atWar })
            check(outcome.confirmation.relationships.getValue("Greece").let { it.otherCivilizationId == "Rome" && it.atWar })
            check(PlayerHost(runtime).bindPlayer("Rome").turn().scenario == null)
            check(PlayerHost(runtime).bindPlayer("Rome").turn().socialAdvisories.single().terms == PACT_TERMS)
        }
        attacksDespitePromise(root.resolve("combat"))
    }

    private fun attacksDespitePromise(root: Path) {
        HeadlessRuntime.createDuel(root, combatScenario = true).use { runtime ->
            val rome = PlayerHost(runtime).bindPlayer("Rome")
            val greece = PlayerHost(runtime).bindPlayer("Greece")
            val before = rome.turn()
            val proposal = rome.communication(obj("op" to "offer", "epoch" to before.epoch, "requestId" to "ceasefire", "participants" to listOf("Rome", "Greece"), "kind" to "peace", "terms" to "Stop all attacks", "conditions" to "Unconditional", "expiresTurn" to 8))["offer"]!!.jsonObject
            greece.communication(obj("op" to "accept", "epoch" to before.epoch, "requestId" to "accept-ceasefire", "proposal" to proposal["proposal"], "version" to 1))
            val advised = rome.turn()
            check(advised.revision == before.revision)
            check(advised.socialAdvisories.single().terms == "Stop all attacks")
            check(advised.map == before.map && advised.cities == before.cities) { "Social promises changed native combat options" }
            val unit = before.map.units.first { it.combat!!.attacks.any { option -> !option.defenderIsCity } }
            val attack = unit.combat!!.attacks.first { !it.defenderIsCity }
            val command = TurnCommandRequest("attack", before.epoch, before.revision, TurnCommandKind.ATTACK, target = unit.handle, destination = attack.destination, attackFrom = attack.attackFrom, socialVersion = 0)
            val receipt = rome.execute(command)
            check(receipt.combat!!.attacked)
            check(rome.execute(command) == receipt)
            val next = rome.turn()
            val city = next.cities.first { it.attacks.isNotEmpty() }
            val cityAttack = TurnCommandRequest("city-attack", next.epoch, next.revision, TurnCommandKind.CITY_ATTACK, target = city.handle, destination = city.attacks.first().destination)
            check(rome.execute(cityAttack).combat!!.attacked)
            val ledger = rome.communication(obj("op" to "state", "epoch" to before.epoch))
            check(ledger["offers"]!!.jsonArray.single().jsonObject["effective"]!!.jsonPrimitive.boolean)
            check(ledger["consents"]!!.jsonArray.all { it.jsonObject["decision"]!!.jsonPrimitive.content == "accept" })
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
