package uncivai

import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.AttackOption
import uncivai.player.OwnedUnit
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardOpenOption
import java.security.MessageDigest
import java.util.UUID
import kotlin.math.abs
import kotlin.math.max

/** A short operator-authored feasibility check, separate from autonomous model tests. */
object GreekOpportunityProbe {
    private val prettyJson = Json { prettyPrint = true }

    @JvmStatic
    fun main(args: Array<String>) {
        val source = Path.of(args[0])
        val root = Path.of(args[1])
        require(source.isAbsolute && root.isAbsolute && !Files.exists(root))
        Files.createDirectories(root)
        val original = Files.readString(source)
        val checkpoint = Json.parseToJsonElement(original).jsonObject
        Files.writeString(root.resolve("source-game.json"), checkpoint.getValue("gameJson").jsonPrimitive.content)
        val events = root.resolve("actions.jsonl")
        fun log(value: JsonObject) {
            Files.writeString(events, value.toString() + "\n", StandardOpenOption.CREATE, StandardOpenOption.APPEND)
        }
        HeadlessRuntime.load(root.resolve("source-game.json"), root.resolve("runtime")).use { runtime ->
            runtime.executor.enableCheckpoints(root.resolve("checkpoint.json"))
            GameplayTestApi(runtime, root.resolve("tls"), mapOf("Greece" to "G".repeat(43))).use { api ->
                api.acquire()
                var token = checkNotNull(api.activate().playerToken)
                val initial = api.turn(token)
                check(initial.turn == 40 && initial.map.units.count { it.name == "Hoplite" } == 3)
                val target = initial.map.tiles.single { it.cityName == "Paris" && it.visible }.position
                val initialUnits = initial.map.units.filter { it.military }.map { it.handle }.toSet()
                Files.writeString(root.resolve("initial-player.json"), PlayerTurnJson.json.encodeToString(initial))
                var captureTurn: Int? = null
                var completedTurns = 0
                var actions = 0
                fun act(turn: PlayerTurnSnapshot, request: TurnCommandRequest) {
                    Files.writeString(root.resolve("observation-${actions++}.json"), PlayerTurnJson.json.encodeToString(turn))
                    val preview = turn.map.units.firstOrNull { it.handle == request.target }?.combat?.attacks?.firstOrNull {
                        it.destination == request.destination && it.attackFrom == request.attackFrom
                    }
                    log(
                        obj(
                            "phase" to "attempt",
                            "turn" to turn.turn,
                            "request" to Json.parseToJsonElement(PlayerTurnJson.json.encodeToString(request)),
                            "preview" to preview?.let { Json.parseToJsonElement(PlayerTurnJson.json.encodeToString(it)) },
                        ),
                    )
                    val receipt = api.execute(token, request)
                    log(
                        obj(
                            "phase" to "receipt",
                            "turn" to turn.turn,
                            "receipt" to Json.parseToJsonElement(PlayerTurnJson.json.encodeToString(receipt)),
                        ),
                    )
                }
                for (ownTurn in 0..1) {
                    if (ownTurn > 0) token = checkNotNull(api.activate().playerToken)
                    if (ownTurn == 0) {
                        val turn = api.turn(token)
                        act(turn, command(turn, TurnCommandKind.DECLARE_WAR, "France").copy(confirmWar = true))
                    }
                    for (step in 0 until 15) {
                        val turn = api.turn(token)
                        val alert = turn.alerts.firstOrNull()
                        if (alert != null) {
                            if (alert.capture != null) {
                                val puppet = alert.capture.options.single { it.name == "Puppet" && it.available }
                                act(turn, command(turn, TurnCommandKind.RESOLVE_CAPTURED_CITY, alert.handle, puppet.name))
                                captureTurn = turn.turn
                            } else {
                                check(alert.canAcknowledge) { "Unsupported alert: $alert" }
                                act(turn, command(turn, TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle))
                            }
                            continue
                        }
                        if (turn.cities.any { it.name == "Paris" }) break
                        val cityHealth = turn.map.tiles.single { it.position == target && it.visible }.cityHealth!!
                        val best = turn.map.units.filter { it.military }.flatMap { unit ->
                            unit.combat!!.attacks.filter { it.defenderIsCity && it.destination == target && it.maxDamageToAttacker < unit.health }
                                .map { unit to it }
                        }.maxWithOrNull(
                            compareBy<Pair<OwnedUnit, AttackOption>>(
                                { (unit, attack) ->
                                    if (unit.combat!!.rangedStrength == 0 && attack.minDamageToDefender >= cityHealth - 1) {
                                        2
                                    } else if (unit.combat.rangedStrength > 0) {
                                        1
                                    } else {
                                        0
                                    }
                                },
                                { (_, attack) -> attack.minDamageToDefender },
                                { (_, attack) -> -attack.movementCost },
                            ),
                        )
                        if (best != null) {
                            val (unit, attack) = best
                            act(turn, command(turn, TurnCommandKind.ATTACK, unit.handle).copy(destination = attack.destination, attackFrom = attack.attackFrom))
                            continue
                        }
                        val move = turn.map.units.filter { it.military }.flatMap { unit ->
                            unit.moves.filter {
                                distance(it.destination, target) < distance(unit.position, target) && distance(it.destination, target) >= if (unit.combat!!.rangedStrength > 0) 2 else 1
                            }.map { unit to it }
                        }.minByOrNull { (_, option) -> distance(option.destination, target) }
                        if (move == null) break
                        act(turn, command(turn, TurnCommandKind.MOVE_UNIT, move.first.handle).copy(destination = move.second.destination))
                    }
                    val beforeEnd = api.turn(token)
                    check(beforeEnd.canEndTurn) { "Unresolved decisions: ${beforeEnd.pending}" }
                    act(beforeEnd, command(beforeEnd, TurnCommandKind.END_TURN).reviewedFor(beforeEnd))
                    completedTurns++
                    if (captureTurn != null) break
                }
                token = checkNotNull(api.activate().playerToken)
                val final = api.turn(token)
                Files.writeString(root.resolve("final-player.json"), PlayerTurnJson.json.encodeToString(final))
                val lost = initialUnits - final.map.units.map { it.handle }.toSet()
                val result = obj(
                    "sourceCheckpoint" to source.toString(), "sourceCheckpointSha256" to sha256(original),
                    "sourceUnchanged" to (Files.readString(source) == original), "modelCalls" to 0,
                    "declaredTurn" to initial.turn, "captureTurn" to captureTurn, "completedOwnTurns" to completedTurns,
                    "afterNativeOpponentTurn" to final.turn, "ownsParis" to final.cities.any { it.name == "Paris" },
                    "lostMilitaryHandles" to lost.toList(),
                    "finalUnits" to final.map.units.filter { it.military }.map {
                        obj("unit" to it.handle, "name" to it.name, "health" to it.health)
                    },
                    "remainingCapitalQueue" to final.cities.single { it.name == "Athens" }.constructionQueue,
                    "happiness" to final.economy!!.happiness,
                    "scope" to "One fixed visible-information attack sequence followed by native opponent turns; feasibility evidence, not optimal-play or win-rate evidence.",
                )
                Files.writeString(root.resolve("result.json"), prettyJson.encodeToString(result))
                println("GREEK_OPPORTUNITY_RESULT $result")
                check(captureTurn != null && final.cities.any { it.name == "Paris" }) { "The scripted fast capture did not succeed; retain the evidence." }
                check(Files.readString(source) == original)
            }
        }
    }

    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection)

    private fun distance(a: TilePosition, b: TilePosition): Int {
        val dx = a.x - b.x
        val dy = a.y - b.y
        return max(abs(dx), max(abs(dy), abs(dx - dy)))
    }

    private fun sha256(value: String) = MessageDigest.getInstance("SHA-256").digest(value.toByteArray()).joinToString("") { "%02x".format(it) }
}
