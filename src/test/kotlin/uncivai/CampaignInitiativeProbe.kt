package uncivai

import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
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

/** A fixed candidate campaign using only player observations and ordinary commands. */
object CampaignInitiativeProbe {
    private val json = Json { prettyPrint = true }
    private val paris = TilePosition(0, 0)

    @JvmStatic fun main(args: Array<String>) {
        val source = Path.of(args[0])
        val root = Path.of(args[1])
        require(source.isAbsolute && root.isAbsolute && !Files.exists(root))
        Files.createDirectories(root)
        for (id in listOf("campaign-reconnaissance", "campaign-siege", "campaign-counteroffensive")) {
            val directory = Files.createDirectories(root.resolve(id))
            val checkpoint = source.resolve(id).resolve("exploration.json")
            val original = Files.readString(checkpoint)
            val gameText = Json.parseToJsonElement(original).jsonObject.getValue("gameJson").jsonPrimitive.content
            Files.writeString(directory.resolve("source-game.json"), gameText)
            HeadlessRuntime.load(directory.resolve("source-game.json"), directory.resolve("runtime")).use { runtime ->
                runtime.executor.enableCheckpoints(directory.resolve("checkpoint.json"))
                GameplayTestApi(runtime, directory.resolve("tls"), mapOf("Greece" to "G".repeat(43))).use { api ->
                    api.acquire()
                    var token = ""
                    var initial: PlayerTurnSnapshot? = null
                    var captureTurn: Int? = null
                    var declarationTurn: Int? = null
                    var completed = 0
                    fun act(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null, destination: TilePosition? = null, attackFrom: TilePosition? = null, queue: List<String> = emptyList()) {
                        var request = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection,
                            destination = destination, attackFrom = attackFrom, queue = queue, confirmWar = kind == TurnCommandKind.DECLARE_WAR)
                        if (kind == TurnCommandKind.END_TURN) request = request.reviewedFor(turn)
                        val receipt = api.execute(token, request)
                        val event = obj("turn" to turn.turn, "request" to Json.parseToJsonElement(PlayerTurnJson.json.encodeToString(request)),
                            "receipt" to Json.parseToJsonElement(PlayerTurnJson.json.encodeToString(receipt)))
                        Files.writeString(directory.resolve("actions.jsonl"), "$event\n", StandardOpenOption.CREATE, StandardOpenOption.APPEND)
                    }
                    for (ownTurn in 0 until 7) {
                        token = checkNotNull(api.activate().playerToken)
                        var turn = api.turn(token)
                        if (initial == null) initial = turn
                        Files.writeString(directory.resolve("turn-${turn.turn}.json"), PlayerTurnJson.json.encodeToString(turn))
                        val moved = mutableSetOf<String>()
                        for (step in 0 until 60) {
                            turn = api.turn(token)
                            val trade = turn.diplomacy.incomingRequests.firstOrNull()
                            if (trade != null) {
                                val request = obj("op" to "trade_decide", "epoch" to turn.epoch, "requestId" to UUID.randomUUID().toString(),
                                    "proposal" to trade.proposal, "version" to trade.version, "decision" to "decline")
                                val result = api.communicate(token, request)
                                Files.writeString(directory.resolve("actions.jsonl"), obj("turn" to turn.turn, "communication" to request, "result" to result).toString() + "\n", StandardOpenOption.CREATE, StandardOpenOption.APPEND)
                                continue
                            }
                            val alert = turn.alerts.firstOrNull()
                            if (alert != null) {
                                when {
                                    alert.capture != null -> {
                                        val option = alert.capture.options.first { it.available && it.name in setOf("Puppet", "Keep") }
                                        act(turn, TurnCommandKind.RESOLVE_CAPTURED_CITY, alert.handle, option.name)
                                        if (api.turn(token).cities.any { it.name == "Paris" }) captureTurn = turn.turn
                                    }
                                    alert.friendship != null -> act(turn, TurnCommandKind.RESPOND_FRIENDSHIP, alert.handle, "Decline")
                                    alert.canAcknowledge -> act(turn, TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle)
                                    else -> error("Unsupported native alert: $alert")
                                }
                                continue
                            }
                            val city = turn.cities.firstOrNull { !it.puppet && it.constructionQueue.isEmpty() }
                            if (city != null) {
                                val choice = listOf("Library", "Colosseum", "Barracks", "Market", "Nothing").firstOrNull { it in city.constructionOptions } ?: city.constructionOptions.first()
                                act(turn, TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, choice)
                                continue
                            }
                            if (turn.researchQueue.isEmpty()) {
                                act(turn, TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf(turn.researchOptions.first()))
                                continue
                            }
                            if (turn.pending.any { it.kind == "Policy" }) {
                                val choice = listOf("Tradition", "Liberty", "Oligarchy", "Republic").firstOrNull { it in turn.policyOptions } ?: turn.policyOptions.first()
                                act(turn, TurnCommandKind.ADOPT_POLICY, selection = choice)
                                continue
                            }
                            if (turn.cities.any { it.name == "Paris" }) break
                            val atWar = turn.diplomacy.knownCivilizations.single { it.civilizationId == "France" }.atWar
                            if (!atWar) {
                                if (turn.map.tiles.any { it.position == paris && it.visible }) {
                                    act(turn, TurnCommandKind.DECLARE_WAR, "France")
                                    declarationTurn = turn.turn
                                    continue
                                }
                                val scout = turn.map.units.single { it.name == "Scout" }
                                val move = scout.moves.filter { distance(it.destination, paris) < distance(scout.position, paris) }
                                    .minByOrNull { distance(it.destination, paris) }
                                if (move != null) {
                                    act(turn, TurnCommandKind.MOVE_UNIT, scout.handle, destination = move.destination)
                                    continue
                                }
                                break
                            }
                            val targetHealth = turn.map.tiles.firstOrNull { it.position == paris && it.visible }?.cityHealth ?: 10000
                            val attacks = turn.map.units.filter { it.military && it.name != "Scout" }.flatMap { unit ->
                                unit.combat!!.attacks.filter { attack ->
                                    attack.maxDamageToAttacker < unit.health &&
                                        (!attack.defenderIsCity || attack.destination == paris &&
                                            (unit.combat.rangedStrength > 0 || attack.minDamageToDefender >= targetHealth - 1))
                                }.map { unit to it }
                            }
                            val attack = attacks.maxWithOrNull(compareBy<Pair<OwnedUnit, AttackOption>>(
                                { (u, a) -> if (a.defenderIsCity && u.combat!!.rangedStrength == 0) 3 else if (!a.defenderIsCity) 2 else 1 },
                                { (_, a) -> a.minDamageToDefender }, { (_, a) -> -a.movementCost },
                            ))
                            if (attack != null) {
                                act(turn, TurnCommandKind.ATTACK, attack.first.handle, destination = attack.second.destination, attackFrom = attack.second.attackFrom)
                                continue
                            }
                            val siege = turn.map.units.firstOrNull { it.name == "Catapult" && distance(it.position, paris) == 2 && it.combat!!.orders.any { o -> o.name == "SET_UP" && o.available } }
                            if (siege != null) {
                                act(turn, TurnCommandKind.SET_UNIT_ORDER, siege.handle, "SET_UP")
                                continue
                            }
                            val movements = turn.map.units.filter { it.military && it.name != "Scout" && it.handle !in moved }.flatMap { unit ->
                                val wanted = if (unit.combat!!.rangedStrength > 0) 2 else 1
                                unit.moves.filter { distance(it.destination, paris) in wanted until distance(unit.position, paris) }
                                    .map { unit to it }
                            }
                            val movement = movements.minWithOrNull(compareBy(
                                { (u, _) -> if (u.name == "Catapult") 0 else if (u.combat!!.rangedStrength > 0) 1 else 2 },
                                { (_, m) -> distance(m.destination, paris) }, { (_, m) -> m.movementCost.toFloatOrNull() ?: 100f },
                            ))
                            if (movement == null) break
                            moved += movement.first.handle
                            act(turn, TurnCommandKind.MOVE_UNIT, movement.first.handle, destination = movement.second.destination)
                        }
                        turn = api.turn(token)
                        check(turn.canEndTurn) { "Unresolved decisions: ${turn.pending}" }
                        act(turn, TurnCommandKind.END_TURN)
                        completed++
                        if (captureTurn != null) break
                    }
                    token = checkNotNull(api.activate().playerToken)
                    val final = api.turn(token)
                    Files.writeString(directory.resolve("final-player.json"), PlayerTurnJson.json.encodeToString(final))
                    val lost = initial!!.map.units.filter { it.military }.map { it.handle }.toSet() - final.map.units.map { it.handle }.toSet()
                    val result = obj("case" to id, "modelCalls" to 0, "sourceSha256" to sha256(original), "sourceUnchanged" to (Files.readString(checkpoint) == original),
                        "completedOwnTurns" to completed, "declarationTurn" to declarationTurn, "captureTurn" to captureTurn,
                        "ownsParisAfterNativeTurn" to final.cities.any { it.name == "Paris" }, "lostStartingMilitary" to lost.toList(),
                        "finalUnits" to final.map.units.filter { it.military }.map { obj("unit" to it.handle, "name" to it.name, "health" to it.health) },
                        "scope" to "Fixed visible-information candidate sequence with native opponent turns; demonstrates feasibility, not optimality or that every alternative is wrong")
                    Files.writeString(directory.resolve("result.json"), json.encodeToString(result))
                    println("CAMPAIGN_PROBE_RESULT $result")
                    check(captureTurn != null && final.cities.any { it.name == "Paris" }) { "Candidate campaign failed; retain its evidence and reconsider the fixture or sequence" }
                    check(Files.readString(checkpoint) == original)
                }
            }
        }
    }

    private fun distance(a: TilePosition, b: TilePosition): Int = max(abs(a.x - b.x), max(abs(a.y - b.y), abs((a.x - b.x) - (a.y - b.y))))
    private fun sha256(value: String) = MessageDigest.getInstance("SHA-256").digest(value.toByteArray()).joinToString("") { "%02x".format(it) }
}
