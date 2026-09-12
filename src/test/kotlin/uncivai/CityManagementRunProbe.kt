package uncivai

import com.unciv.json.json
import com.unciv.logic.city.City
import com.unciv.logic.city.CityFocus
import com.unciv.logic.city.GreatPersonPointsBreakdown
import com.unciv.logic.city.managers.CityFounder
import com.unciv.logic.civilization.PlayerCityOperations
import com.unciv.logic.map.HexCoord
import com.unciv.models.stats.Stat
import kotlinx.serialization.Serializable
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import uncivai.player.observeCityManagement
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

@Serializable
private data class CityManagementAction(val civilization: String, val request: TurnCommandRequest, val receipt: TurnCommandReceipt)

@Serializable
private data class CityManagementFacts(val cities: Map<String, List<String>>, val gold: Map<String, Int>, val greatPeople: Map<String, Map<String, Int>>)

@Serializable
private data class CityManagementRecovery(val actions: List<CityManagementAction>, val before: CityManagementFacts, val after: CityManagementFacts, val activationId: String)

/** Setup and assertions read the native game; gameplay goes through authenticated HTTPS. */
object CityManagementRunProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val stage = args[1]
        val checkpoint = root.resolve(if (stage == "prepare") "exploration.json" else "checkpoint.json")
        if (stage == "prepare") require(!Files.exists(checkpoint)) { "Checkpoint already exists" }
        val fresh = stage in setOf("start", "prepare", "observe")
        val runtime = if (fresh) {
            HeadlessRuntime.createDuel(root.resolve(stage), 412L, combatScenario = true)
        } else {
            HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve(stage))
        }
        runtime.use {
            if (fresh) arrange(runtime)
            if (stage == "observe") {
                observations(runtime)
                return
            }
            if (fresh) {
                if (stage == "prepare") prepareDuelCredentials(root)
                runtime.executor.enableCheckpoints(checkpoint)
            }
            if (stage == "prepare") {
                println("Prepared city management exercise: $checkpoint")
                return
            }
            GameplayTestApi(runtime, root.resolve("$stage-tls")).use { api ->
                when (stage) {
                    "start" -> start(runtime, api, root)
                    "restore" -> restore(runtime, api, root)
                    "verify" -> verify(runtime, api, root)
                    else -> error("Unknown stage $stage")
                }
            }
        }
    }

    private fun arrange(runtime: HeadlessRuntime) {
        val game = runtime.game
        game.tileMap.mapParameters.name = "City management exercise"
        val players = listOf("Rome", "Greece").map(game::getCivilization)
        players.forEach { player ->
            player.units.getCivUnits().toList().forEach { it.destroy() }
            player.cities.filterNot { it.isOriginalCapital }.toList().forEach { it.destroyCity() }
        }
        players.first().getDiplomacyManager(players.last())!!.makePeace()
        // Mirror terrain and city positions, so both players can exercise every objective.
        for (tile in game.tileMap.values) {
            tile.baseTerrain = "Plains"
            tile.removeTerrainFeatures()
            tile.improvement = "Farm"
            tile.setTileResource(null)
            tile.setTerrainTransients()
        }
        for ((index, player) in players.withIndex()) {
            val sign = if (index == 0) -1 else 1
            val neighbor = CityFounder().foundCity(player, HexCoord(sign * 4, sign * 3))
            neighbor.name = if (index == 0) "Antium" else "Sparta"
            player.addGold(1000 - player.gold)
            player.popupAlerts.clear()
            player.notifications.clear()
            player.tech.techsToResearch = arrayListOf("Writing", "The Wheel", "Bronze Working")
            for (city in player.cities) {
                city.health = 200
                city.population.setPopulation(if (city.isOriginalCapital) 4 else 3)
                city.setCityFocus(CityFocus.FoodFocus)
                city.avoidGrowth = true
                city.manualSpecialists = true
                for (building in listOf("Granary", "Library", "University", "Market", "Colosseum")) city.cityConstructions.addBuilding(building)
                city.cityConstructions.constructionQueue = arrayListOf("The Pyramids")
                city.population.specialistAllocations.clear()
                city.clearWorkedTiles()
                city.population.foodStored = city.population.getFoodToNextPopulation() - 1
            }
            val capital = player.cities.single { it.isOriginalCapital }
            val shared = capital.getWorkableTiles().filter { it in neighbor.tilesInRange && !it.isCityCenter() }
                .sortedWith(compareBy({ it.position.x }, { it.position.y })).first()
            check(neighbor.workTile(shared))
            neighbor.lockedTiles.add(shared.position)
            capital.cityStats.update()
            neighbor.cityStats.update()
            player.cache.updateCivResources()
            player.cache.updateViewableTiles()
            player.updateStatsForNextTurn()
            check(PlayerCityOperations(player).isAssignableTile(capital, shared).not())
        }
        check(game.victoryData == null)
        check(players.none { it.isAtWar() })
    }

    private fun observations(runtime: HeadlessRuntime) {
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val city = player.cities.single { it.isOriginalCapital }
        val before = json().toJson(game)
        val observed = observeCityManagement(city, true) { it.id }
        repeat(3) { check(observed == observeCityManagement(city, true) { it.id }) }
        check(json().toJson(game) == before) { "City management observation mutated the save" }
        check(observed.sharedTiles.isNotEmpty())
        check(observed.sharedTiles.all { shared -> player.cities.any { it.id == shared.workingCityHandle } })
        check(observed.specialists.single { it.name == "Scientist" }.slots == 2)
        for (specialist in observed.specialists) {
            for (yield in specialist.yields) {
                check(yield.amount == city.cityStats.getStatsOfSpecialist(specialist.name)[Stat.valueOf(yield.stat)].toInt().toString())
            }
            check(specialist.greatPersonPoints == game.ruleset.specialists.getValue(specialist.name).greatPersonPoints)
        }
        for (sale in observed.buildingSales) {
            check(sale.gold == city.getGoldForSellingBuilding(sale.name))
            check(sale.confirmation.contains(sale.name))
        }
        check(observed.buildingSales.single { it.name == "Palace" }.action.unavailableReasons.isNotEmpty())
        val waiting = observeCityManagement(city, false) { it.id }
        check(waiting.focusOptions.none { it.action.available })
        check(waiting.specialists.none { it.assign.available || it.unassign.available })
        check(waiting.buildingSales.none { it.action.available })
        check(waiting.sharedTiles.none { it.action.available })
        val foreign = game.getCivilization("Greece")
        foreign.greatPeople.greatPersonPointsCounter["Great Scientist"] = 83
        foreign.cities.forEach { it.population.specialistAllocations["Scientist"] = 2 }
        foreign.cities.first().cityConstructions.removeBuilding("Market")
        check(observed == observeCityManagement(city, true) { it.id }) { "Foreign private city state changed the observation" }
        city.population.specialistAllocations["Scientist"] = 1
        city.cityStats.update()
        val after = observeCityManagement(city, true) { it.id }
        val scientist = after.greatPeople.single { it.name == "Great Scientist" }
        check(scientist.pointsPerTurn == GreatPersonPointsBreakdown(city).sum()["Great Scientist"])
        check(scientist.empirePoints == 0 && scientist.pointsPerTurn > 0)
        check(scientist.baseSources.isNotEmpty())
        check(observed.specialists.single { it.name == "Scientist" }.assigned == 0)
        try {
            (observed.buildingSales as MutableList).clear()
            error("Returned city management collections are mutable")
        } catch (_: UnsupportedOperationException) { }
        city.population.specialistAllocations.clear()
        city.cityConstructions.removeBuilding("University")
        city.cityConstructions.removeBuilding("Market")
        check(!observeCityManagement(city, true) { it.id }.specialistControl.available)
    }

    private fun start(runtime: HeadlessRuntime, api: GameplayTestApi, root: Path) {
        api.acquire()
        val activation = api.activate()
        val before = facts(runtime)
        val actions = actions(runtime, api, checkNotNull(activation.playerToken), "Rome")
        save(root, CityManagementRecovery(actions, before, facts(runtime), checkNotNull(activation.activationId)))
        api.operator(OperatorRequest("release", api.lease))
    }

    private fun actions(runtime: HeadlessRuntime, api: GameplayTestApi, token: String, civ: String): List<CityManagementAction> {
        val player = runtime.game.getCivilization(civ)
        val capital = player.cities.single { it.isOriginalCapital }
        val neighbor = player.cities.single { !it.isOriginalCapital }
        val result = arrayListOf<CityManagementAction>()
        var turn = api.readAlerts(token)
        check(turn.scenario?.name == "CITY_MANAGEMENT_EXERCISE")
        check(turn.cities.size == 2)
        fun offered(city: City) = turn.cities.single { it.name == city.name }
        fun act(kind: TurnCommandKind, city: City, selection: String? = null, enabled: Boolean? = null, destination: TilePosition? = null, worked: Boolean? = null): TurnCommandReceipt {
            turn = api.turn(token)
            val request = command(turn, kind, offered(city).handle, selection).copy(enabled = enabled, destination = destination, worked = worked, confirmAction = kind == TurnCommandKind.SELL_BUILDING)
            val gold = player.gold
            val receipt = api.execute(token, request)
            if (kind != TurnCommandKind.SET_WORKED_TILE) check(receipt.cityManagement!!.goldChange == player.gold - gold)
            val committed = facts(runtime)
            check(api.execute(token, request) == receipt)
            api.reject(token, request.copy(enabled = !(request.enabled ?: false)), "REQUEST_ID_CONFLICT")
            api.reject(token, request.copy(requestId = UUID.randomUUID().toString()), "STALE_REVISION")
            check(facts(runtime) == committed)
            result += CityManagementAction(civ, request, receipt)
            turn = api.turn(token)
            return receipt
        }
        val focus = command(turn, TurnCommandKind.SET_CITY_FOCUS, offered(capital).handle, "ProductionFocus")
        api.reject(token, focus.copy(selection = "HappinessFocus"), "OPTION_UNAVAILABLE")
        api.reject(token, focus.copy(enabled = true), "INVALID_ARGUMENT")
        api.reject(token, command(turn, TurnCommandKind.SET_AVOID_GROWTH, offered(capital).handle), "INVALID_ARGUMENT")
        api.reject(api.playerToken(if (civ == "Rome") "Greece" else "Rome"), focus, "COMMAND_UNAVAILABLE")

        if (capital.population.getFreePopulation() == 0) {
            act(TurnCommandKind.SET_WORKED_TILE, capital, destination = offered(capital).development!!.tiles.first { it.canUnwork }.position, worked = false)
        }
        val shared = checkNotNull(offered(capital).management!!.sharedTiles.firstOrNull { it.action.available }) {
            "$civ has no transferable tile: ${offered(capital).management!!.sharedTiles}"
        }
        check(player.cities.single { it.name == shared.workingCityName } === neighbor)
        val tile = runtime.game.tileMap[HexCoord(shared.position.x, shared.position.y)]
        val owner = tile.getCity()
        val transferred = act(TurnCommandKind.REASSIGN_CITY_TILE, capital, destination = shared.position)
        check(transferred.cityManagement!!.cities.map { it.name }.toSet().containsAll(listOf(capital.name, neighbor.name)))
        check(tile.getWorkingCity() === capital && tile.getCity() === owner && !tile.isLocked())
        api.reject(token, command(turn, TurnCommandKind.REASSIGN_CITY_TILE, offered(capital).handle).copy(destination = shared.position), "OPTION_UNAVAILABLE")
        api.reject(token, command(turn, TurnCommandKind.REASSIGN_CITY_TILE, offered(capital).handle).copy(destination = TilePosition(999, 999)), "OPTION_UNAVAILABLE")
        act(TurnCommandKind.SET_CITY_FOCUS, capital, "ProductionFocus")
        check(capital.getCityFocus() == CityFocus.ProductionFocus)
        act(TurnCommandKind.SET_CITY_FOCUS, capital, "FoodFocus")
        act(TurnCommandKind.SET_AVOID_GROWTH, capital, enabled = false)
        check(!capital.avoidGrowth)
        act(TurnCommandKind.SET_AVOID_GROWTH, capital, enabled = true)
        act(TurnCommandKind.SET_MANUAL_SPECIALISTS, capital, enabled = false)
        act(TurnCommandKind.SET_MANUAL_SPECIALISTS, capital, enabled = true)
        check(capital.manualSpecialists)
        // Disabling automatic allocation must not silently free a citizen.
        if (capital.population.getFreePopulation() == 0) {
            api.reject(token, command(turn, TurnCommandKind.ASSIGN_SPECIALIST, offered(capital).handle, "Scientist"), "OPTION_UNAVAILABLE")
            act(TurnCommandKind.SET_WORKED_TILE, capital, destination = offered(capital).development!!.tiles.first { it.canUnwork }.position, worked = false)
        }
        act(TurnCommandKind.ASSIGN_SPECIALIST, capital, "Scientist")
        check(capital.population.getNewSpecialists()["Scientist"] > 0)
        val gp = offered(capital).management!!.greatPeople.single { it.name == "Great Scientist" }
        check(gp.pointsPerTurn == GreatPersonPointsBreakdown(capital).sum()["Great Scientist"])
        act(TurnCommandKind.UNASSIGN_SPECIALIST, capital, "Scientist")
        act(TurnCommandKind.ASSIGN_SPECIALIST, capital, "Scientist")

        val sale = offered(capital).management!!.buildingSales.single { it.name == "University" }
        check(sale.action.available && sale.description.isNotBlank())
        val request = command(turn, TurnCommandKind.SELL_BUILDING, offered(capital).handle, sale.name)
        api.reject(token, request, "INVALID_ARGUMENT")
        api.reject(token, request.copy(selection = "Palace", confirmAction = true), "OPTION_UNAVAILABLE")
        val sold = act(TurnCommandKind.SELL_BUILDING, capital, sale.name)
        check(sold.cityManagement!!.goldChange == sale.gold && sold.economy!!.goldChange == sale.gold)
        check(sold.cityManagement.cities.single { it.name == capital.name }.removedBuildings == listOf("University"))
        check(capital.population.getNewSpecialists()["Scientist"] == 0 && !capital.cityConstructions.isBuilt("University"))
        check(capital.population.getFreePopulation() == 0) { "Fixture tiles must remain useful with Avoid Growth enabled" }
        api.reject(token, command(turn, TurnCommandKind.SELL_BUILDING, offered(capital).handle, "Market").copy(confirmAction = true), "OPTION_UNAVAILABLE")
        check(offered(capital).management!!.buildingSales.single { it.name == "Market" }.action.unavailableReasons.isNotEmpty())

        // A command receipt alone does not acknowledge the next management options.
        val mutation = command(turn, TurnCommandKind.SET_CITY_FOCUS, offered(neighbor).handle, "FoodFocus")
        val receipt = api.execute(token, mutation)
        result += CityManagementAction(civ, mutation, receipt)
        api.reject(token, mutation.copy(requestId = UUID.randomUUID().toString(), revision = receipt.revision, selection = "ScienceFocus"), "OBSERVATION_REQUIRED")
        turn = api.turn(token)
        act(TurnCommandKind.RESET_CITIZENS, neighbor)
        check(neighbor.lockedTiles.isEmpty())
        if (neighbor.population.getFreePopulation() == 0) {
            act(TurnCommandKind.SET_WORKED_TILE, neighbor, destination = offered(neighbor).development!!.tiles.first { it.canUnwork }.position, worked = false)
        }
        act(TurnCommandKind.ASSIGN_SPECIALIST, neighbor, "Scientist")
        act(TurnCommandKind.SET_AVOID_GROWTH, neighbor, enabled = true)
        check(neighbor.cityStats.currentCityStats.food > 0) { "Fixture needs a food surplus to exercise Avoid Growth" }
        check(neighbor.population.getNewSpecialists()["Scientist"] == 1)
        return result
    }

    private fun restore(runtime: HeadlessRuntime, api: GameplayTestApi, root: Path) {
        val saved = expected(root)
        check(facts(runtime) == saved.after) { "City changes did not survive restart" }
        replay(api, root, saved.actions)
        api.acquire()
        val status = api.status()
        api.operator(OperatorRequest("rollback", api.lease, status.epoch, status.serverInstance, status.handoff, "Rome", saved.activationId))
        check(facts(runtime) == saved.before) { "Rollback did not restore city assignments, buildings, food, points and gold" }
        val activation = api.activate()
        var token = checkNotNull(activation.playerToken)
        api.reject(token, saved.actions.first().request, "STALE_REVISION")
        val actions = actions(runtime, api, token, "Rome").toMutableList()
        api.finishTurn(token)
        token = checkNotNull(api.activate().playerToken)
        val greekTurn = api.readAlerts(token)
        val foreignHandle = actions.first().request.target
        api.reject(token, command(greekTurn, TurnCommandKind.SET_CITY_FOCUS, foreignHandle, "FoodFocus"), "CITY_UNAVAILABLE")
        actions += actions(runtime, api, token, "Greece")
        api.finishTurn(token)
        for (name in listOf("Rome", "Greece")) {
            val player = runtime.game.getCivilization(name)
            val city = player.cities.single { !it.isOriginalCapital }
            check(city.population.population == 3)
            check(city.population.foodStored == city.population.getFoodToNextPopulation()) { "$name did not cap food" }
            check(player.greatPeople.greatPersonPointsCounter["Great Scientist"] > 0)
            token = checkNotNull(api.activate().playerToken)
            var turn = api.readAlerts(token)
            val cityHandle = turn.cities.single { it.name == city.name }.handle
            val request = command(turn, TurnCommandKind.SET_AVOID_GROWTH, cityHandle).copy(enabled = false)
            actions += CityManagementAction(name, request, api.execute(token, request))
            turn = api.turn(token)
            check(turn.cities.single { it.name == city.name }.development!!.growing)
            val capital = turn.cities.single { it.name == player.cities.single { it.isOriginalCapital }.name }
            check(capital.management!!.buildingSales.single { it.name == "Market" }.action.available) { "Sale limit did not reset on the next turn" }
            api.finishTurn(token)
        }
        for (name in listOf("Rome", "Greece")) {
            val player = runtime.game.getCivilization(name)
            check(player.cities.single { !it.isOriginalCapital }.population.population == 4) { "$name did not grow after disabling Avoid Growth" }
            check(player.greatPeople.greatPersonPointsCounter["Great Scientist"] >= 6)
        }
        save(root, CityManagementRecovery(actions, saved.before, facts(runtime), checkNotNull(activation.activationId)))
        api.operator(OperatorRequest("release", api.lease))
    }

    private fun verify(runtime: HeadlessRuntime, api: GameplayTestApi, root: Path) {
        val saved = expected(root)
        check(facts(runtime) == saved.after) { "Scheduled growth and great-person progress did not survive restart" }
        replay(api, root, saved.actions)
        check(facts(runtime) == saved.after)
        api.acquire()
        api.finishTurn(checkNotNull(api.activate().playerToken))
        check(api.status().activeCivilization == "Greece")
    }

    private fun replay(api: GameplayTestApi, root: Path, actions: List<CityManagementAction>) {
        val before = Files.readString(root.resolve("checkpoint.json"))
        for (action in actions) check(api.execute(api.playerToken(action.civilization), action.request) == action.receipt)
        check(Files.readString(root.resolve("checkpoint.json")) == before) { "Receipt replay changed the checkpoint" }
    }

    private fun facts(runtime: HeadlessRuntime): CityManagementFacts {
        val players = listOf("Rome", "Greece").map(runtime.game::getCivilization)
        return CityManagementFacts(
            players.associate { player ->
                player.civName to player.cities.sortedBy { it.name }.map { city ->
                    listOf(
                        city.name, city.getCityFocus(), city.avoidGrowth, city.manualSpecialists, city.population.population,
                        city.population.foodStored, city.population.getFreePopulation(), city.hasSoldBuildingThisTurn,
                        city.workedTiles.sortedBy { it.toString() }, city.lockedTiles.sortedBy { it.toString() },
                        city.population.getNewSpecialists().toSortedMap(), city.cityConstructions.getBuiltBuildings().map { it.name }.sorted().toList(),
                        Stat.entries.associate { it.name to city.cityStats.currentCityStats[it] },
                    ).joinToString("|")
                }
            },
            players.associate { it.civName to it.gold },
            players.associate { it.civName to it.greatPeople.greatPersonPointsCounter.toSortedMap() },
        )
    }

    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection)

    private fun save(root: Path, value: CityManagementRecovery) = Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(value))
    private fun expected(root: Path) = PlayerTurnJson.json.decodeFromString<CityManagementRecovery>(Files.readString(root.resolve("expected.json")))
}
