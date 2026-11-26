package uncivai

import com.unciv.logic.GameInfo
import com.unciv.logic.files.UncivFiles
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.UnitSettlement
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
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
import java.time.Instant
import java.util.UUID
import kotlin.math.abs
import kotlin.math.max

/**
 * Operator-only counterfactual experiment. Setup and reporting can inspect saves;
 * ReferencePlayer receives only the authenticated, player-filtered API.
 * A forced campaign tests feasibility, not autonomous strategic recognition.
 */
object MilitaryOpportunityEvaluation {
    private val json = Json { prettyPrint = true }

    @JvmStatic
    fun main(args: Array<String>) {
        val prepared = Path.of(args[0])
        val output = Path.of(args[1])
        val rounds = args.getOrNull(2)?.toInt() ?: 20
        val variants = (args.getOrNull(3) ?: "base,reserves,strong-defense").split(',')
        val policies = (args.getOrNull(4) ?: "attack,recon,develop").split(',')
        require(prepared.isAbsolute && output.isAbsolute && !Files.exists(output))
        require(rounds in 1..30 && variants.all { it in setOf("base", "reserves", "strong-defense") })
        require(policies.all { it in setOf("attack", "recon", "develop") })
        Files.createDirectories(output)
        write(
            output.resolve("protocol.json"),
            obj(
                "createdAt" to Instant.now().toString(),
                "engineRevision" to System.getProperty("uncivai.engineRevision"),
                "rounds" to rounds,
                "variants" to variants,
                "policies" to policies,
                "modelCalls" to 0,
                "controller" to "Reference policy v1; player observations and native legal commands only. No hidden-state access, combat rerolls, or search over future saves.",
                "economy" to "Keep Library; queue one Settler in Persepolis, then happiness/infrastructure. Same research, policy, worker automation and settlement policy in all branches. Puppet a captured capital. Found only with a four-happiness buffer.",
                "military" to "Attack commits to war immediately. Recon spends one turn revealing the Paris approach and commits next turn only if the visible capital is damaged and no more than two French military units are seen. Develop stays at peace unless attacked. Once Paris is taken, defend it and seek ordinary native peace; do not conquer further cities.",
                "tactics" to "Use offered ranged fire and melee attacks with survivable damage bounds; prioritize city capture and unit kills. Approach a target using legal movement. Withdraw wounded units; heal and hold after the objective. Native AI opponents take all their normal turns.",
                "limitations" to "Hand-authored reference policy, not an optimal player or win-rate estimate. Combat rolls depend on turn/location; identical replays are not independent combat samples. Synthetic reserves are sensitivity tests, not a distribution of hidden armies.",
            ),
        )
        val summaries = mutableListOf<JsonObject>()
        for (variant in variants) {
            for (policy in policies) {
                val source = prepared.resolve(if (variant == "strong-defense") "persia-strong-defense" else "persia-opportunity").resolve("exploration.json")
                val checkpoint = Json.parseToJsonElement(Files.readString(source)).jsonObject
                val gameJson = checkpoint.getValue("gameJson").jsonPrimitive.content
                val root = Files.createDirectories(output.resolve("$variant-$policy"))
                Files.writeString(root.resolve("source-game.json"), gameJson)
                HeadlessRuntime.load(root.resolve("source-game.json"), root.resolve("runtime")).use { runtime ->
                    check(runtime.game.currentPlayer == "Persia" && runtime.game.turns == 40)
                    if (variant == "reserves") addReserves(runtime.game)
                    runtime.executor.enableCheckpoints(root.resolve("checkpoint.json"))
                    Files.writeString(root.resolve("initial-game.json"), UncivFiles.gameInfoToString(runtime.game, forceZip = false))
                    write(root.resolve("source.json"), obj("checkpoint" to source.toString(), "nativeSaveSha256" to sha256(gameJson), "variant" to variant))
                    GameplayTestApi(runtime, root.resolve("tls"), mapOf("Persia" to "P".repeat(43))).use { api ->
                        api.acquire()
                        val player = ReferencePlayer(api, root, policy)
                        val frames = mutableListOf<JsonObject>()
                        repeat(rounds) {
                            val token = checkNotNull(api.activate().playerToken)
                            val before = api.turn(token)
                            Files.writeString(root.resolve("turn-${before.turn}-player.json"), PlayerTurnJson.json.encodeToString(before))
                            frames += nativeFacts(runtime.game)
                            player.play(token)
                            println("MILITARY_OPPORTUNITY $variant $policy completed=${before.turn} next=${runtime.game.turns} cities=${runtime.game.getCivilization("Persia").cities.size}")
                        }
                        val token = checkNotNull(api.activate().playerToken)
                        val final = api.turn(token)
                        Files.writeString(root.resolve("turn-${final.turn}-player.json"), PlayerTurnJson.json.encodeToString(final))
                        frames += nativeFacts(runtime.game)
                        write(root.resolve("native-frames.json"), JsonArray(frames))
                        Files.writeString(root.resolve("final-game.json"), UncivFiles.gameInfoToString(runtime.game, forceZip = false))
                        val summary = obj(
                            "variant" to variant, "policy" to policy, "completedOwnTurns" to rounds,
                            "declaredTurn" to player.declaredTurn, "capturedTurn" to player.capturedTurn,
                            "reconDecision" to player.reconDecision, "actions" to player.actions,
                            "final" to frames.last(), "directory" to root.toString(),
                        )
                        write(root.resolve("result.json"), summary)
                        summaries += summary
                        write(output.resolve("results.json"), JsonArray(summaries))
                    }
                }
            }
        }
    }

    private fun addReserves(game: GameInfo) {
        val persia = game.getCivilization("Persia")
        val france = game.getCivilization("France")
        // A rear city prevents elimination when Paris falls; its army can respond.
        // All additions are outside Persia's initial sight, but native demographics
        // may still differ. This is a labeled synthetic position, not the same save.
        val cityTile = game.tileMap[HexCoord(4, 1)]
        check(!cityTile.isVisible(persia) && !cityTile.isExplored(persia))
        val settler = checkNotNull(game.tileMap.placeUnitNearTile(cityTile.position, "Settler", france))
        check(settler.currentTile === cityTile)
        val city = checkNotNull(UnitSettlement.tryFoundCity(settler))
        city.population.setPopulation(2)
        city.cityConstructions.constructionQueue = arrayListOf("Archer")
        for ((name, position) in listOf("Warrior" to HexCoord(2, 0), "Archer" to HexCoord(2, 1))) {
            val tile = game.tileMap[position]
            check(!tile.isVisible(persia))
            val unit = checkNotNull(game.tileMap.placeUnitNearTile(position, name, france))
            check(unit.currentTile === tile)
            unit.currentMovement = unit.getMaxMovement().toFloat()
        }
        for (civ in game.civilizations) {
            civ.units.getCivUnits().forEach { it.updateVisibleTiles(updateCivViewableTiles = false) }
            civ.cities.forEach {
                it.reassignPopulation(resetLocked = true)
                it.cityStats.update()
            }
            civ.cache.updateCivResources()
            civ.cache.updateViewableTiles()
            civ.updateStatsForNextTurn()
        }
    }

    /** Raw state is only used for evaluator evidence, never passed to ReferencePlayer. */
    private fun nativeFacts(game: GameInfo): JsonObject = obj(
        "turn" to game.turns,
        "civilizations" to game.civilizations.filter { it.isMajorCiv() }.map { civ ->
            obj(
                "civilization" to civ.civName,
                "defeated" to civ.isDefeated(),
                "gold" to civ.gold,
                "technologies" to civ.tech.techsResearched.toList().sorted(),
                "cities" to civ.cities.map { city ->
                    obj(
                        "id" to city.id,
                        "name" to city.name,
                        "position" to pos(city.location.x, city.location.y),
                        "health" to city.health,
                        "population" to city.population.population,
                        "buildings" to city.cityConstructions.getBuiltBuildings().map { it.name }.sorted().toList(),
                        "queue" to city.cityConstructions.constructionQueue.toList(),
                    )
                },
                "units" to civ.units.getCivUnits().map { unit ->
                    obj(
                        "id" to unit.id,
                        "name" to unit.name,
                        "health" to unit.health,
                        "position" to pos(unit.currentTile.position.x, unit.currentTile.position.y),
                        "action" to unit.action,
                    )
                }.toList(),
            )
        },
    )

    private fun pos(x: Int, y: Int) = obj("x" to x, "y" to y)
    private fun sha256(value: String) = MessageDigest.getInstance("SHA-256").digest(value.toByteArray()).joinToString("") { "%02x".format(it) }
    private fun write(path: Path, value: kotlinx.serialization.json.JsonElement) = Files.writeString(path, json.encodeToString(value))
}

/** No GameInfo, runtime or operator state is available to this policy. */
private class ReferencePlayer(private val api: GameplayTestApi, private val root: Path, private val policy: String) {
    private val paris = TilePosition(0, 0)
    private var token = ""
    private var settlerQueued = false
    private var settlementTarget: TilePosition? = null
    var declaredTurn: Int? = null
        private set
    var capturedTurn: Int? = null
        private set
    var reconDecision: String? = null
        private set
    var actions = 0
        private set

    private fun read() = api.turn(token)
    private fun log(value: JsonObject) {
        Files.writeString(root.resolve("actions.jsonl"), value.toString() + "\n", StandardOpenOption.CREATE, StandardOpenOption.APPEND)
    }

    private fun execute(
        turn: PlayerTurnSnapshot,
        kind: TurnCommandKind,
        target: String? = null,
        selection: String? = null,
        destination: TilePosition? = null,
        attackFrom: TilePosition? = null,
        queue: List<String> = emptyList(),
        reason: String,
    ) {
        val request = TurnCommandRequest(
            UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection,
            queue = queue, destination = destination, attackFrom = attackFrom,
            confirmWar = kind == TurnCommandKind.DECLARE_WAR,
            reviewId = turn.turnReview?.id.takeIf { kind == TurnCommandKind.END_TURN },
            deferReason = reason.takeIf { kind == TurnCommandKind.END_TURN },
        )
        val preview = turn.map.units.firstOrNull { it.handle == target }?.combat?.attacks
            ?.firstOrNull { it.destination == destination && it.attackFrom == attackFrom }
        log(
            obj(
                "phase" to "attempt",
                "turn" to turn.turn,
                "reason" to reason,
                "request" to PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request),
                "combatPreview" to preview?.let { PlayerTurnJson.json.encodeToJsonElement(AttackOption.serializer(), it) },
            ),
        )
        val receipt = api.execute(token, request)
        actions++
        log(
            obj(
                "phase" to "receipt",
                "turn" to turn.turn,
                "requestId" to request.requestId,
                "receipt" to Json.parseToJsonElement(PlayerTurnJson.json.encodeToString(receipt)),
            ),
        )
    }

    fun play(activeToken: String) {
        token = activeToken
        resolveRequired()
        prepareEconomy()
        val turn = read()
        if (declaredTurn == null && capturedTurn == null && policy != "develop") {
            if (policy == "recon" && turn.turn == 40) {
                reconnoitre()
            } else if (policy == "attack" || reconAccepts(turn)) {
                val fresh = read()
                if (fresh.diplomacy.knownCivilizations.any { it.civilizationId == "France" && it.declareWar.available }) {
                    execute(fresh, TurnCommandKind.DECLARE_WAR, "France", reason = "Counterfactual campaign commitment using existing forces; no production diversion.")
                    declaredTurn = fresh.turn
                    resolveRequired()
                }
            }
        }
        fight()
        moveSettler()
        holdUnits()
        resolveRequired()
        val end = read()
        check(end.canEndTurn) { "Unresolved native decisions: ${end.pending}" }
        execute(end, TurnCommandKind.END_TURN, reason = "Reference policy has completed available campaign, settlement and economic actions; hold remaining units.")
    }

    private fun reconAccepts(turn: PlayerTurnSnapshot): Boolean {
        if (reconDecision != null) return reconDecision == "attack"
        val target = turn.map.tiles.firstOrNull { it.position == paris && it.visible }
        val defenders = turn.map.tiles.filter { it.visible }.sumOf { tile -> tile.units.count { it.civilization == "France" && it.military } }
        val accept = target?.cityHealth != null && target.cityHealth <= 100 && (target.cityDefensiveStrength ?: 100) <= 16 && defenders <= 2
        reconDecision = if (accept) "attack" else "develop"
        log(
            obj(
                "phase" to "reconDecision",
                "turn" to turn.turn,
                "decision" to reconDecision,
                "visibleDefenders" to defenders,
                "cityHealth" to target?.cityHealth,
                "cityStrength" to target?.cityDefensiveStrength,
            ),
        )
        return accept
    }

    private fun prepareEconomy() {
        var turn = read()
        val capital = turn.cities.first { it.name == "Persepolis" }
        if (!settlerQueued && "Settler" in capital.constructionOptions && "Settler" !in capital.constructionQueue) {
            execute(turn, TurnCommandKind.QUEUE_CONSTRUCTION, capital.handle, "Settler", reason = "Common baseline: finish Library, then one Settler.")
            settlerQueued = true
        }
        turn = read()
        for (handle in turn.map.units.filter { it.name == "Worker" && it.combat?.order?.automated != true }.map { it.handle }) {
            val current = read()
            val worker = current.map.units.firstOrNull { it.handle == handle } ?: continue
            if (worker.combat?.orders?.any { it.name == "AUTOMATE" && it.available } == true) {
                execute(current, TurnCommandKind.SET_UNIT_ORDER, handle, "AUTOMATE", reason = "Common native worker automation in every branch.")
            }
        }
    }

    private fun resolveRequired() {
        repeat(60) {
            val turn = read()
            val alert = turn.alerts.firstOrNull()
            if (alert != null) {
                val capture = alert.capture
                when {
                    capture != null -> {
                        val choice = capture.options.firstOrNull { it.name == "Puppet" && it.available }
                            ?: capture.options.first { it.available && it.name in setOf("Annex", "Keep") }
                        execute(turn, TurnCommandKind.RESOLVE_CAPTURED_CITY, alert.handle, choice.name, reason = "Keep the objective as a puppet where available; avoid immediate annexation unhappiness.")
                        if (read().cities.any { it.name == "Paris" } && capturedTurn == null) capturedTurn = turn.turn
                    }

                    alert.friendship != null -> execute(turn, TurnCommandKind.RESPOND_FRIENDSHIP, alert.handle, "Decline", reason = "Common neutral diplomacy; no new binding friendships during the comparison.")

                    alert.canAcknowledge -> execute(turn, TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle, reason = "Read native notification.")

                    alert.nativeDiplomacy != null -> {
                        val native = alert.nativeDiplomacy
                        val choice = native.options.firstOrNull { it.available && it.name == "Acknowledge" }
                            ?: native.options.first { it.available && !it.declaresWar }
                        val request = obj(
                            "op" to "send",
                            "epoch" to turn.epoch,
                            "requestId" to UUID.randomUUID().toString(),
                            "to" to native.civilizationId,
                            "text" to "Acknowledged.",
                            "nativeAction" to obj("kind" to "RESPOND_DIPLOMACY", "target" to alert.handle, "selection" to choice.name, "revision" to turn.revision),
                        )
                        val result = api.communicate(token, request)
                        log(obj("phase" to "communication", "turn" to turn.turn, "request" to request, "result" to result))
                    }

                    else -> error("Unsupported alert: $alert")
                }
                return@repeat
            }
            val city = turn.cities.firstOrNull { !it.puppet && it.constructionQueue.isEmpty() }
            when {
                turn.freeTechnologies > 0 -> execute(turn, TurnCommandKind.CHOOSE_FREE_TECHNOLOGY, selection = research(turn), reason = "Common research priorities.")

                turn.researchQueue.isEmpty() -> execute(turn, TurnCommandKind.SET_RESEARCH_QUEUE, queue = listOf(research(turn)), reason = "Common research priorities: happiness, economic and military basics.")

                city != null -> {
                    val priority = listOf("Monument", "Granary", "Library", "Colosseum", "Water Mill", "Market", "Temple", "Barracks", "Walls", "Nothing")
                    val choice = priority.firstOrNull { it in city.constructionOptions } ?: city.constructionOptions.first()
                    execute(turn, TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, choice, reason = "Common infrastructure priority after the planned Library and Settler.")
                }

                turn.pending.any { it.kind == "Policy" } -> {
                    val priority = listOf("Liberty", "Citizenship", "Republic", "Collective Rule", "Meritocracy", "Representation", "Tradition")
                    execute(turn, TurnCommandKind.ADOPT_POLICY, selection = priority.firstOrNull { it in turn.policyOptions } ?: turn.policyOptions.first(), reason = "Common expansion policy priority.")
                }

                else -> return
            }
        }
        error("Required decisions did not converge")
    }

    private fun research(turn: PlayerTurnSnapshot) = listOf("Construction", "Calendar", "Iron Working", "Mathematics", "Trapping", "Sailing", "Currency", "Engineering")
        .firstOrNull { it in turn.researchOptions } ?: turn.researchOptions.first()

    private fun reconnoitre() {
        // Move the closest melee unit along the actual Paris approach, not native Explore.
        val turn = read()
        val scout = turn.map.units.filter { it.military && it.combat?.rangedStrength == 0 }.minByOrNull { distance(it.position, paris) } ?: return
        val move = scout.moves.filter { it.destination != scout.position && distance(it.destination, paris) <= 2 }
            .maxWithOrNull(compareBy<uncivai.player.UnitMoveOption>({ it.tilesOutsideCurrentSight }, { it.unexploredTiles }, { -distance(it.destination, paris) })) ?: return
        execute(turn, TurnCommandKind.MOVE_UNIT, scout.handle, destination = move.destination, reason = "One-turn reconnaissance of the Paris approach before the next decision.")
    }

    private fun fight() {
        val relocated = mutableSetOf<String>()
        repeat(80) {
            resolveRequired()
            val turn = read()
            val atWar = turn.diplomacy.knownCivilizations.any { it.atWar }
            if (!atWar) return
            val ownsParis = turn.cities.any { it.name == "Paris" }
            val attacking = declaredTurn != null && !ownsParis && policy != "develop"
            val cityAttack = turn.cities.flatMap { city -> city.attacks.map { city.handle to it } }
                .maxByOrNull { it.second.minDamageToDefender }
            if (cityAttack != null) {
                execute(turn, TurnCommandKind.CITY_ATTACK, cityAttack.first, destination = cityAttack.second.destination, reason = "Use native city bombardment against visible hostiles.")
                return@repeat
            }
            val offers = turn.map.units.filter { it.military }.flatMap { unit ->
                unit.combat?.attacks.orEmpty().filter { offer ->
                    (!offer.defenderIsCity || (attacking && offer.destination == paris)) &&
                        (
                            unit.health - offer.maxDamageToAttacker >= 25 || (
                                offer.defenderIsCity &&
                                    offer.minDamageToDefender >= (turn.map.tiles.firstOrNull { it.position == offer.destination }?.cityHealth ?: 10000) - 1
                                )
                            )
                }.map { unit to it }
            }
            val best = offers.maxWithOrNull(
                compareBy<Pair<OwnedUnit, AttackOption>>(
                    { (unit, offer) -> attackValue(turn, unit, offer) },
                    { (_, offer) -> -offer.movementCost },
                    { (unit, _) -> unit.handle },
                ),
            )
            if (best != null) {
                val (unit, offer) = best
                execute(
                    turn,
                    TurnCommandKind.ATTACK,
                    unit.handle,
                    destination = offer.destination,
                    attackFrom = offer.attackFrom,
                    reason = "Native offered attack: prefer ranged damage, certain kills/capture and a survivable retaliation bound.",
                )
                return@repeat
            }
            val wounded = turn.map.units.firstOrNull { it.military && it.health < 50 && it.handle !in relocated && it.moves.isNotEmpty() }
            if (wounded != null) {
                relocated += wounded.handle
                val cityPositions = turn.cities.mapNotNull { it.development?.position }
                val retreat = wounded.moves.filter { it.destination != wounded.position && it.afterMove?.context?.visibleHostiles.orEmpty().isEmpty() }
                    .minByOrNull { move -> cityPositions.minOfOrNull { distance(it, move.destination) } ?: 100 }
                if (retreat != null) {
                    execute(turn, TurnCommandKind.MOVE_UNIT, wounded.handle, destination = retreat.destination, reason = "Withdraw a wounded unit toward friendly territory for recovery.")
                    return@repeat
                }
            }
            if (attacking) {
                for (unit in turn.map.units.filter { it.military && it.health >= 50 && it.handle !in relocated }.sortedBy { it.combat?.rangedStrength == 0 }) {
                    relocated += unit.handle
                    val wanted = if ((unit.combat?.rangedStrength ?: 0) > 0) 2 else 1
                    val oldDistance = distance(unit.position, paris)
                    val move = unit.moves.filter { distance(it.destination, paris) in wanted until oldDistance }
                        .minWithOrNull(compareBy({ abs(distance(it.destination, paris) - wanted) }, { it.movementCost.toFloatOrNull() ?: 100f }, { it.destination.x }, { it.destination.y }))
                    if (move != null) {
                        execute(turn, TurnCommandKind.MOVE_UNIT, unit.handle, destination = move.destination, reason = "Concentrate healthy units into useful range of the selected objective.")
                        return@repeat
                    }
                }
            }
            return
        }
        error("Military action budget exhausted")
    }

    private fun attackValue(turn: PlayerTurnSnapshot, unit: OwnedUnit, offer: AttackOption): Int {
        val defenderHealth = if (offer.defenderIsCity) {
            turn.map.tiles.firstOrNull { it.position == offer.destination }?.cityHealth
        } else {
            turn.map.tiles.firstOrNull { it.position == offer.destination }?.units?.firstOrNull { it.atWar && it.military }?.health
        }
        val certainCapture = offer.defenderIsCity && unit.combat?.rangedStrength == 0 && offer.minDamageToDefender >= (defenderHealth ?: 10000) - 1
        val certainKill = !offer.defenderIsCity && offer.minDamageToDefender >= (defenderHealth ?: 10000)
        return (if (certainCapture) 1000 else 0) + (if (certainKill) 500 else 0) +
            (if ((unit.combat?.rangedStrength ?: 0) > 0) 200 else 0) + offer.minDamageToDefender - offer.maxDamageToAttacker +
            (if (offer.defenderIsCity) 50 else 0)
    }

    private fun moveSettler() {
        val turn = read()
        val settler = turn.map.units.firstOrNull { it.name == "Settler" } ?: return
        if ((turn.economy?.happiness ?: -1) < 4) return
        val candidates = turn.settlement?.candidates.orEmpty()
        if (settlementTarget == null || candidates.none { it.position == settlementTarget }) {
            settlementTarget = candidates.maxWithOrNull(compareBy({ it.score }, { -distance(settler.position, it.position) }))?.position
        }
        val target = settlementTarget ?: return
        if (settler.position == target && settler.canFoundCity) {
            execute(turn, TurnCommandKind.FOUND_CITY, settler.handle, reason = "Found the selected native-recommended site with the common happiness buffer.")
            settlementTarget = null
            return
        }
        val move = settler.moves.filter {
            distance(it.destination, target) < distance(settler.position, target) &&
                it.afterMove?.context?.visibleHostiles.orEmpty().isEmpty()
        }
            .minWithOrNull(compareBy({ distance(it.destination, target) }, { it.movementCost.toFloatOrNull() ?: 100f })) ?: return
        execute(turn, TurnCommandKind.MOVE_UNIT, settler.handle, destination = move.destination, reason = "Advance toward the selected settlement site on a legal route without visible hostiles.")
        val after = read()
        val current = after.map.units.firstOrNull { it.handle == settler.handle } ?: return
        if (current.position == target && current.canFoundCity) {
            execute(after, TurnCommandKind.FOUND_CITY, current.handle, reason = "Found after completing the legal route.")
            settlementTarget = null
        }
    }

    private fun holdUnits() {
        for (handle in read().map.units.filter { it.military }.map { it.handle }) {
            val turn = read()
            val unit = turn.map.units.firstOrNull { it.handle == handle } ?: continue
            val promotion = unit.combat?.promotions?.choices?.filter { it.available }?.let { choices ->
                choices.firstOrNull { unit.health < 60 && it.name.contains("Heal", ignoreCase = true) }
                    ?: choices.firstOrNull { it.name.startsWith(if (unit.combat.rangedStrength > 0) "Accuracy" else "Shock") }
            }
            if (promotion != null) execute(turn, TurnCommandKind.PROMOTE_UNIT, handle, promotion.name, reason = "Use an earned promotion for recovery or open-terrain combat.")
            val fresh = read()
            val now = fresh.map.units.firstOrNull { it.handle == handle } ?: continue
            if (now.combat?.order?.action?.startsWith("Fortify") == true) continue
            val fortify = now.combat?.orders?.firstOrNull { it.available && it.name == "FORTIFY" } ?: continue
            execute(fresh, TurnCommandKind.SET_UNIT_ORDER, handle, fortify.name, reason = "Hold and recover after completed actions; retain the army near its objective.")
        }
    }

    private fun distance(a: TilePosition, b: TilePosition): Int {
        val dx = a.x - b.x
        val dy = a.y - b.y
        return max(abs(dx), max(abs(dy), abs(dx - dy)))
    }
}
