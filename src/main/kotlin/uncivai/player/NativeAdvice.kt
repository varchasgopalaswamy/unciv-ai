package uncivai.player

import com.unciv.logic.automation.Automation
import com.unciv.logic.automation.AutomationSelection
import com.unciv.logic.civilization.Civilization
import com.unciv.models.ruleset.INonPerpetualConstruction
import com.unciv.models.ruleset.unique.UniqueType
import com.unciv.models.stats.Stat
import com.unciv.models.stats.Stats
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import uncivai.obj
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.FutureTask
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import com.unciv.logic.automation.ReadOnlyAutomation as Native

const val NATIVE_GUIDE_VERSION = "native-guidance-v1"

@Serializable
data class NativeAdviceRequest(val epoch: String, val revision: String, val cities: List<String> = emptyList(), val units: List<String> = emptyList())

@Serializable
data class NativeRecommendation(
    val id: String,
    val category: String,
    val subject: String,
    val explanation: String,
    val command: JsonObject?,
    val costs: JsonObject = obj(),
    val prerequisites: List<String> = emptyList(),
    val conflicts: List<String> = emptyList(),
)

@Serializable
data class NativeAdvice(
    val epoch: String,
    val turn: Int?,
    val revision: String,
    val available: Boolean,
    val version: String = NATIVE_GUIDE_VERSION,
    val recommendations: List<NativeRecommendation> = emptyList(),
    val limitations: List<String> = emptyList(),
    val basis: String = "Next-action alternatives from native branches using permitted observations. No outcomes or discoveries are simulated. Re-read after acting; overlapping budgets, subjects, destinations and targets require re-evaluation. Native command validation remains authoritative.",
)

/** Already observed values and owned/public metadata only; no engine object escapes capture. */
internal data class NativeAdviceInputs(
    val epoch: String,
    val revision: String,
    val turn: Int,
    val position: Native.Position,
    val commands: Map<Native.Action, NativeRecommendation>,
    val limitations: List<String>,
)

internal fun captureNativeAdvice(player: Civilization, turn: PlayerTurnSnapshot, state: TurnSessionState, deadlineNanos: Long = Long.MAX_VALUE): NativeAdviceInputs {
    fun checkDeadline() {
        if (Thread.currentThread().isInterrupted) throw InterruptedException()
        if (System.nanoTime() >= deadlineNanos) throw TimeoutException()
    }
    checkDeadline()
    val commands = linkedMapOf<Native.Action, NativeRecommendation>()
    val limits = linkedSetOf(
        "Diplomatic actions, declarations, peace and trades remain entirely player decisions.",
        "Air missions, religious/great-person abilities, road networks, multi-step worker plans and strategic enemy-city routing are not selected.",
        "Promotion entries are alternatives from the native selection pool; no live random draw is consumed.",
        "Combat recommendations apply native target values across legal damage bounds; overlapping ranges remain alternatives. Hidden turn/location damage rolls are never consulted. Risky, city and specialized attack branches may be unavailable.",
    )
    val observedTiles = turn.map.tiles.associateBy { it.position }
    val visibleHostiles = turn.map.tiles.filter { it.visible && it.units.any { unit -> unit.military && unit.atWar } }
    fun terrainDefinitions(tile: ObservedTile): List<com.unciv.models.ruleset.tile.Terrain>? {
        val base = tile.terrain ?: return null
        return (listOfNotNull(base, tile.naturalWonder) + tile.features.orEmpty()).map { name ->
            player.gameInfo.ruleset.terrains[name] ?: return null
        }
    }
    fun terrainDamage(tile: ObservedTile): Int? = terrainDefinitions(tile)?.sumOf { it.damagePerTurn }
    fun terrainHeight(tile: ObservedTile): Int? = terrainDefinitions(tile)?.let { terrains ->
        val elevation = terrains.flatMap { it.getMatchingUniques(UniqueType.VisibilityElevation).toList() }.sumOf { it.params[0].toInt() }
        elevation + if (terrains.any { it.hasUnique(UniqueType.BlocksLineOfSightAtSameElevation) }) 1 else 0
    }
    fun action(
        kind: Native.Kind,
        subject: String,
        command: String?,
        selection: String? = null,
        destination: TilePosition? = null,
        origin: TilePosition? = null,
        cost: JsonObject = obj(),
        prerequisites: List<String> = emptyList(),
        confirm: Boolean = false,
    ): Native.Action {
        checkDeadline()
        val key = Native.Action(kind, subject, selection, destination?.let { "${it.x},${it.y}" }, origin?.let { "${it.x},${it.y}" })
        val args = linkedMapOf<String, Any?>("kind" to command, "target" to subject)
        selection?.let { args["selection"] = it }
        destination?.let { args["destination"] = obj("x" to it.x, "y" to it.y) }
        origin?.let { args["attackFrom"] = obj("x" to it.x, "y" to it.y) }
        if (command == "SET_WORKED_TILE") args["worked"] = true
        commands[key] = NativeRecommendation(
            "",
            kind.name,
            subject,
            "",
            command?.let { obj("tool" to "player_command", "arguments" to obj(*args.entries.map { it.key to it.value }.toTypedArray())) },
            cost,
            prerequisites + if (confirm) listOf("Read the action's consequences and explicitly confirm before execution.") else emptyList(),
            listOfNotNull("subject:$subject", destination?.let { "destination:${it.x},${it.y}" }, if ("gold" in cost) "treasury" else null),
        )
        return key
    }
    val cities = turn.cities.mapNotNull { observed ->
        checkDeadline()
        val cityId = state.cityHandles.entries.firstOrNull { it.value == observed.handle }?.key ?: return@mapNotNull null
        val city = player.cities.firstOrNull { it.id == cityId } ?: return@mapNotNull null
        val current = observed.development?.constructions?.firstOrNull()
        val constructions = if (current != null && !current.perpetual) {
            emptyList()
        } else {
            limits.add("${observed.handle}: construction ranks only permitted, observable candidates. World-wonder competition, foreign-capital defensive bonuses, strategic military/Work Boat/space-part production branches are unavailable.")
            val buildings = observed.constructionOptions.mapNotNull { name ->
                val estimate = observed.constructionEstimates[name] ?: return@mapNotNull null
                val preference = state.constructionEstimates.nativePreference(city.id, name) ?: return@mapNotNull null
                val definition = player.gameInfo.ruleset.buildings[name] ?: return@mapNotNull null
                if (definition.isWonder && (observed.puppet || city.cityStats.currentCityStats.production < player.cities.map { it.cityStats.currentCityStats.production }.average() || player.cities.sumOf { it.population.population } < 12)) return@mapNotNull null
                Native.Construction(
                    action(
                        Native.Kind.Construction,
                        observed.handle,
                        "QUEUE_CONSTRUCTION",
                        name,
                        cost = obj("production" to estimate.production.cost, "investedProduction" to estimate.production.stored),
                        prerequisites = estimate.requiredTechnologies,
                    ),
                    estimate.production.cost - estimate.production.stored,
                    definition.getWeightForAiDecision(city.state),
                    preference,
                )
            }
            val automation = com.unciv.logic.automation.city.ConstructionAutomation(city.cityConstructions)
            val worker = observed.constructionOptions.mapNotNull { player.gameInfo.ruleset.units[it] }
                .filter { it.hasUnique(UniqueType.BuildImprovements) && automation.permitsCandidate(it) && Automation.allowAutomatedConstruction(player, city, it) }.minByOrNull { it.cost }
            val workers = player.units.getCivUnits().count { it.cache.hasUniqueToBuildImprovements }.toFloat()
            val modifier = AutomationSelection.workerConstructionModifier(player.cities.size, workers)
            val workerEstimate = worker?.let { observed.constructionEstimates[it.name] }
            buildings + if (worker == null || modifier == null || workerEstimate == null || observed.puppet) {
                emptyList()
            } else {
                listOf(
                    Native.Construction(
                        action(
                            Native.Kind.Construction,
                            observed.handle,
                            "QUEUE_CONSTRUCTION",
                            worker.name,
                            cost = obj("production" to workerEstimate.production.cost, "investedProduction" to workerEstimate.production.stored),
                            prerequisites = workerEstimate.requiredTechnologies,
                        ),
                        workerEstimate.production.cost - workerEstimate.production.stored,
                        worker.getWeightForAiDecision(city.state),
                        modifier = modifier,
                    ),
                )
            }
        }
        if (current != null && !current.perpetual) {
            action(
                Native.Kind.ContinueConstruction,
                observed.handle,
                null,
                current.name,
                cost = obj("production" to current.productionCost, "investedProduction" to current.productionStored),
                prerequisites = listOf("Continue current construction; native automation does not independently reconsider this investment."),
            )
        }
        val purchases = observed.purchases?.constructions.orEmpty().filter { it.available && it.name == current?.name && it.goldCost != null }.mapNotNull { option ->
            val definition: INonPerpetualConstruction = player.gameInfo.ruleset.buildings[option.name]
                ?: player.gameInfo.ruleset.units[option.name] ?: return@mapNotNull null
            Native.Purchase(
                action(
                    Native.Kind.Purchase,
                    observed.handle,
                    "BUY_CONSTRUCTION",
                    option.name,
                    cost = obj("gold" to option.goldCost),
                    prerequisites = option.requiredTechnologies,
                    confirm = option.confirmation != null,
                ),
                option.goldCost!!,
                definition.hurryCostModifier,
                current?.productionStored ?: 0,
            )
        }
        val bombardments = observed.attacks.filter { attack ->
            attack.defenderIsCity || turn.map.tiles.firstOrNull { it.position == attack.destination }?.units?.any { it.military && it.atWar } == true
        }.map { attack ->
            val health = attack.consequences?.defender?.current
            AutomationSelection.Range(
                action(
                    Native.Kind.Bombard,
                    observed.handle,
                    "CITY_ATTACK",
                    destination = attack.destination,
                    cost = obj("attacks" to 1),
                ),
                attack.minDamageToDefender.toFloat(),
                if (health == null || attack.maxDamageToDefender >= health) Float.POSITIVE_INFINITY else attack.maxDamageToDefender.toFloat(),
            )
        }
        // These native yield previews read owned tiles and the player's own stats;
        // no foreign city/unit object or hidden tile is supplied to the selector.
        val citizenTiles = if ((observed.development?.unassignedPopulation ?: 0) <= 0) {
            emptyList()
        } else {
            observed.development!!.tiles.filter { it.canWork && !it.worked }.map { tile ->
                val yields = Stats().apply { for (yield in tile.yields) this[Stat.valueOf(yield.stat)] = yield.amount.toFloat() }
                val geometry = player.gameInfo.tileMap[tile.position.x, tile.position.y]
                AutomationSelection.Citizen(
                    action(Native.Kind.AssignCitizen, observed.handle, "SET_WORKED_TILE", destination = tile.position),
                    Automation.rankStatsForCityWork(yields, city, false, nativePreferences = true),
                    geometry.longitude,
                    geometry.latitude,
                )
            }
        }
        val specialists = if ((observed.development?.unassignedPopulation ?: 0) <= 0) {
            emptyList()
        } else {
            observed.management?.specialists.orEmpty().filter { it.assign.available && observed.management?.manualSpecialists == false }.map { specialist ->
                AutomationSelection.Ranked(action(Native.Kind.AssignCitizen, observed.handle, "ASSIGN_SPECIALIST", specialist.name), Automation.rankSpecialist(specialist.name, city, nativePreferences = true))
            }
        }
        Native.City(observed.handle, current?.name, current?.perpetual ?: true, purchases, bombardments, citizenTiles, specialists, constructions)
    }
    val units = turn.map.units.mapNotNull { observed ->
        checkDeadline()
        val id = state.unitHandles.entries.firstOrNull { it.value == observed.handle }?.key ?: return@mapNotNull null
        val unit = player.units.getCivUnits().firstOrNull { it.id == id } ?: return@mapNotNull null
        val combat = observed.combat
        val upgrades = observed.economy?.upgrades.orEmpty().filter { it.available }.map { option ->
            Native.Upgrade(
                action(
                    Native.Kind.Upgrade,
                    observed.handle,
                    "UPGRADE_UNIT",
                    option.targetName,
                    cost = obj("gold" to option.goldCost),
                    prerequisites = option.requiredTechnologies,
                    confirm = option.confirmation != null,
                ),
                player.gameInfo.ruleset.units.getValue(option.targetName).cost,
                option.additionalResourceRequirements.keys.any { !unit.requiresResource(it) },
            )
        }
        val promotions = combat?.promotions?.choices.orEmpty().filter { it.available }.map { option ->
            val definition = player.gameInfo.ruleset.unitPromotions.getValue(option.name)
            Native.Promotion(
                action(
                    Native.Kind.Promote,
                    observed.handle,
                    "PROMOTE_UNIT",
                    option.name,
                    cost = obj("experience" to option.experienceCost),
                    prerequisites = option.prerequisites,
                ),
                definition.hasUnique(UniqueType.OneTimeUnitHeal),
                definition.hasUnique(UniqueType.SkipPromotion),
                definition.hasUnique(UniqueType.FreePromotion),
            )
        }
        val rest = combat?.orders?.firstOrNull { it.available && it.name in setOf("FORTIFY_UNTIL_HEALED", "SLEEP_UNTIL_HEALED") }
        val heal = rest?.let { action(Native.Kind.Heal, observed.handle, "SET_UNIT_ORDER", it.name) }
        val attacks = combat?.attacks.orEmpty().filterNot { it.defenderIsCity }.mapNotNull { attack ->
            val target = turn.map.tiles.firstOrNull { it.position == attack.destination }?.units?.firstOrNull { it.military && it.atWar } ?: return@mapNotNull null
            val origin = observedTiles[attack.attackFrom]?.takeIf { it.visible } ?: return@mapNotNull null
            val originDamage = terrainDamage(origin) ?: return@mapNotNull null
            Native.Attack(
                action(
                    Native.Kind.Attack,
                    observed.handle,
                    "ATTACK",
                    destination = attack.destination,
                    origin = attack.attackFrom,
                    cost = obj("movement" to attack.movementCost, "attacks" to 1, "maximumRetaliation" to attack.maxDamageToAttacker),
                ),
                target.health,
                attack.minDamageToDefender,
                attack.maxDamageToDefender,
                attack.maxDamageToAttacker,
                (observed.movement.toFloatOrNull() ?: 0f) - attack.movementCost,
                originDamage,
                true,
            )
        }
        if (combat?.attacks?.any { it.defenderIsCity } == true) {
            limits.add("${observed.handle}: city attacks unavailable; native city target valuation needs a complete surrounding-force count.")
        }
        val advance = combat?.attacks.orEmpty().mapNotNull { attack ->
            if (attack.attackFrom == observed.position || attack.maxDamageToAttacker >= observed.health ||
                (unit.baseUnit.isRanged() && attack.defenderIsCity && attack.consequences?.defender?.current == 1)
            ) {
                return@mapNotNull null
            }
            val move = observed.moves.firstOrNull { it.destination == attack.attackFrom && it.tilesOutsideCurrentSight == 0 && it.unexploredTiles == 0 }
                ?: return@mapNotNull null
            val origin = turn.map.tiles.firstOrNull { it.position == move.destination && it.visible } ?: return@mapNotNull null
            if (terrainDamage(origin) != 0) return@mapNotNull null
            val distance = unit.getTile().aerialDistanceTo(player.gameInfo.tileMap[attack.destination.x, attack.destination.y])
            if (distance > 5) return@mapNotNull null
            AutomationSelection.NearbyEnemy(
                action(
                    Native.Kind.Move,
                    observed.handle,
                    "MOVE_UNIT",
                    destination = move.destination,
                    cost = obj("movement" to move.movementCost),
                ),
                distance,
            )
        }
        val worker = observed.worker
        val workerTile = turn.map.tiles.firstOrNull { it.position == observed.position }
        val workerResource = workerTile?.resource?.let { player.gameInfo.ruleset.tileResources[it] }
        val currentImproves = workerTile?.improvement?.let { workerResource?.isImprovedBy(it) } == true
        val resourceRequired = workerResource != null && workerResource.resourceType.name != "Bonus" && workerResource.getImprovements().any()
        val improvements = if (worker?.currentJob != null) {
            limits.add("${observed.handle}: continue the current worker job; native automation retains work already in progress.")
            emptyList()
        } else if (resourceRequired && currentImproves) {
            emptyList()
        } else {
            worker?.options.orEmpty().filter { it.available && it.steps.size == 1 && it.prerequisites.isEmpty() }.mapNotNull { option ->
                val tile = turn.map.tiles.firstOrNull { it.position == observed.position } ?: return@mapNotNull null
                val definition = player.gameInfo.ruleset.tileImprovements.getValue(option.name)
                if (tile.owner != player.civName || definition.hasUnique(UniqueType.ImprovementStatsForAdjacencies) || option.name.startsWith("Remove") || option.name in setOf("Road", "Railroad", "Repair")) {
                    limits.add("${observed.handle}: outside-border, adjacency-planning, road, repair and removal improvement ranking unavailable.")
                    return@mapNotNull null
                }
                fun yield(name: String) = option.yieldChanges[name]?.toFloatOrNull() ?: 0f
                val yields = AutomationSelection.statsValue(yield("Food"), yield("Production"), yield("Gold"), yield("Science"), yield("Culture"), yield("Happiness"), yield("Faith"), turn.economy?.treasury ?: 0, turn.economy?.goldPerTurn?.toFloat() ?: 0f)
                val geometry = player.gameInfo.tileMap[tile.position.x, tile.position.y]
                val similar = turn.map.tiles.any { it.improvement == option.name && geometry.aerialDistanceTo(player.gameInfo.tileMap[it.position.x, it.position.y]) <= 4 }
                val rank = AutomationSelection.improvementValue(
                    yields,
                    workerResource?.let { it.resourceType.name == "Bonus" },
                    currentImproves,
                    option.resourceProvided != null,
                    similar,
                    tile.improvement?.let { player.gameInfo.ruleset.tileImprovements[it]?.hasUnique(UniqueType.DefensiveBonus) } == true,
                )
                Native.RankedAction(
                    action(
                        Native.Kind.Improve,
                        observed.handle,
                        "START_WORKER_JOB",
                        option.name,
                        cost = obj("turns" to option.totalTurns),
                    ),
                    rank,
                    option.resourceProvided != null,
                )
            }
        }
        val exploration = if (unit.baseUnit.isAirUnit() || observed.worker != null || observed.settlement != null) {
            emptyList()
        } else {
            observed.moves.filter { it.tilesOutsideCurrentSight == 0 && it.unexploredTiles == 0 }.mapNotNull { move ->
                val shown = turn.map.tiles.firstOrNull { it.position == move.destination && it.visible } ?: return@mapNotNull null
                val tile = player.gameInfo.tileMap[shown.position.x, shown.position.y]
                if (shown.owner?.let { player.gameInfo.ruleset.nations[it]?.isCityState } == true || terrainDamage(shown) != 0) return@mapNotNull null
                if (visibleHostiles.any { other -> tile.aerialDistanceTo(player.gameInfo.tileMap[other.position.x, other.position.y]) <= 3 }) return@mapNotNull null
                val height = terrainHeight(shown) ?: return@mapNotNull null
                val unseen = tile.getTilesAtDistance(unit.getVisibilityRange()).count { TilePosition(it.position.x, it.position.y) !in observedTiles }
                if (tile.getTilesInDistance(unit.getVisibilityRange()).none { TilePosition(it.position.x, it.position.y) !in observedTiles }) return@mapNotNull null
                Native.RankedAction(
                    action(
                        Native.Kind.Move,
                        observed.handle,
                        "MOVE_UNIT",
                        destination = move.destination,
                        cost = obj("movement" to move.movementCost),
                    ),
                    AutomationSelection.explorationRank(height, unseen).toFloat(),
                )
            }
        }
        val settlement = if (observed.settlement == null) {
            emptyList()
        } else {
            limits.add("${observed.handle}: settlement uses retained terrain and legal one-turn destinations only. Long routes, undisclosed city locations and foreign diplomatic flags are unavailable; no arrival or discovery is simulated.")
            nativeSettlementValues(player, turn, observed, state.settlementKnowledge).map { (position, values) ->
                val found = position == observed.position
                Native.Settlement(
                    action(
                        if (found) Native.Kind.FoundCity else Native.Kind.Move,
                        observed.handle,
                        if (found) "FOUND_CITY" else "MOVE_UNIT",
                        destination = position.takeUnless { found },
                        cost = if (found) obj("consumesUnit" to true) else obj("movement" to observed.moves.first { it.destination == position }.movementCost),
                    ),
                    values,
                    player.gameInfo.ruleset.modOptions.constants.minimumCityLocationTileValue,
                    turn.turn == 0 && turn.cities.isEmpty() && turn.map.units.count { it.settlement != null } == 1,
                )
            }
        }
        val safeCity = turn.cities.any { it.development?.position == observed.position && it.health > 50 }
        // nearbyHostiles is a two-tile UI warning. Native resting excludes enemies
        // within three tiles, so derive its input from the complete permitted map.
        // Neither a live foreign unit nor the engine's omniscient threat cache is read.
        val nearestObservedEnemy = visibleHostiles.minOfOrNull { hostile ->
            player.gameInfo.tileMap[observed.position.x, observed.position.y].aerialDistanceTo(player.gameInfo.tileMap[hostile.position.x, hostile.position.y])
        } ?: Int.MAX_VALUE
        Native.Unit(
            observed.handle, observed.health, combat?.promotions?.recovery?.healingPerRestTurn ?: 0,
            unit.hasUnique(UniqueType.HealsEvenAfterAction), unit.baseUnit.isAirUnit() || unit.hasUnique(UniqueType.CanMoveAfterAttacking),
            safeCity, nearestObservedEnemy,
            heal, upgrades, promotions, attacks, improvements, exploration, settlement, resourceRequired, advance,
        )
    }
    return NativeAdviceInputs(turn.epoch, turn.revision, turn.turn, Native.Position(turn.economy?.treasury ?: 0, cities, units), commands.toMap(), limits.toList())
}

internal object NativeAdvicePlanner {
    private val executor = ThreadPoolExecutor(2, 2, 0, TimeUnit.SECONDS, ArrayBlockingQueue(8)) { work -> Thread(work, "native-advice").apply { isDaemon = true } }
    private val cache = ConcurrentHashMap<String, NativeAdvice>()

    fun cached(player: String, request: NativeAdviceRequest, deadlineNanos: Long): NativeAdvice? {
        if (Thread.currentThread().isInterrupted || System.nanoTime() >= deadlineNanos) return null
        return cache["$player:${request.epoch}:${request.revision}"]?.filtered(request)
    }

    private fun NativeAdvice.filtered(request: NativeAdviceRequest): NativeAdvice = copy(
        recommendations = recommendations.filter {
            (request.cities.isEmpty() && request.units.isEmpty()) || it.subject in request.cities || it.subject in request.units
        },
    )

    fun compute(player: String, inputs: NativeAdviceInputs, request: NativeAdviceRequest, deadlineNanos: Long = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)): NativeAdvice {
        if (Thread.currentThread().isInterrupted || System.nanoTime() >= deadlineNanos) {
            return NativeAdvice(inputs.epoch, inputs.turn, inputs.revision, false, limitations = listOf("Native advice unavailable: computation deadline exceeded or request cancelled."))
        }
        val key = "$player:${inputs.epoch}:${inputs.revision}"
        val complete = cache[key] ?: run {
            val task = FutureTask {
                val recommendations = Native.select(inputs.position) { System.nanoTime() >= deadlineNanos }.mapIndexed { index, choice ->
                    inputs.commands.getValue(choice.action).copy(
                        id = "native-${inputs.revision}-$index",
                        explanation = if (choice.action.kind != Native.Kind.Heal) {
                            choice.branch
                        } else if (inputs.position.units.single { it.id == choice.action.subject }.safeCity) {
                            "Heal within two resting turns in the owned city (health above 50). Unseen threats remain unknown."
                        } else {
                            "Heal within two resting turns; no observed hostile military unit within three tiles. Unseen threats remain unknown."
                        },
                    )
                }
                NativeAdvice(inputs.epoch, inputs.turn, inputs.revision, true, recommendations = recommendations, limitations = inputs.limitations)
            }
            try {
                executor.execute(task)
                task.get((deadlineNanos - System.nanoTime()).coerceAtLeast(0), TimeUnit.NANOSECONDS).also {
                    if (cache.size > 128) cache.clear()
                    cache[key] = it
                }
            } catch (failure: Exception) {
                task.cancel(true)
                val reason = when (failure) {
                    is TimeoutException -> "computation deadline exceeded"

                    is InterruptedException -> {
                        Thread.currentThread().interrupt()
                        "request cancelled"
                    }

                    is RejectedExecutionException -> "planner is busy; retry after refreshing the position"

                    else -> "selector could not establish advice from permitted inputs"
                }
                NativeAdvice(inputs.epoch, inputs.turn, inputs.revision, false, limitations = listOf("Native advice unavailable: $reason."))
            }
        }
        return complete.filtered(request)
    }
}
