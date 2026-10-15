package uncivai

import com.unciv.json.json
import com.unciv.logic.automation.ReadOnlyAutomation
import com.unciv.logic.map.HexCoord
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import uncivai.player.NativeAdvice
import uncivai.player.NativeAdvicePlanner
import uncivai.player.NativeAdviceRequest
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import uncivai.player.captureNativeAdvice
import java.nio.file.Path

object NativeAdviceProbe {
    @JvmStatic fun main(args: Array<String>) {
        try {
            SettlementPreviewProbe.fixture(Path.of(args[0]).resolve("game")).use { runtime ->
                val game = runtime.game
                val player = game.getCivilization("Rome")
                player.addGold(1000)
                player.units.placeUnitNearTile(HexCoord(1, 0), "Warrior")!!.apply {
                    health = 85
                    promotions.XP = 13
                }
                val session = PlayerHost(runtime).bindPlayer("Rome")
                val turn = session.turn()
                val state = runtime.executor.turnSessions.getValue("Rome")
                val counters = state.identifiers.snapshot()
                val handles = state.cityHandles.toMap() to state.unitHandles.toMap()
                val fingerprint = state.fingerprint
                val before = json().toJson(game)
                val request = NativeAdviceRequest(turn.epoch, turn.revision)
                val advice = session.nativeAdvice(request)
                check(advice.available) { advice.toString() }
                check(advice.epoch == turn.epoch && advice.revision == turn.revision && advice.turn == turn.turn)
                check(advice.recommendations.isNotEmpty())
                check(advice.recommendations.any { it.category == "ContinueConstruction" })
                check(advice.recommendations.filter { it.category == "ContinueConstruction" }.all { it.command == null })
                check(advice.recommendations.all { "subject:${it.subject}" in it.conflicts })
                check(advice.recommendations.none { it.command.toString().contains("confirmAction") })
                val city = turn.cities.first().handle
                val filtered = session.nativeAdvice(request.copy(cities = listOf(city)))
                check(filtered.recommendations == advice.recommendations.filter { it.subject == city })
                check(session.nativeAdvice(request) == advice)
                check(!session.nativeAdvice(request.copy(revision = "stale")).available)
                check(!session.nativeAdvice(request.copy(epoch = "old")).available)
                check(json().toJson(game) == before) { "Advice changed the live save" }
                check(state.identifiers.snapshot() == counters && handles == (state.cityHandles.toMap() to state.unitHandles.toMap()))
                check(state.fingerprint === fingerprint) { "Advice refreshed the observation cache" }

                // Deliberately break a capture dependency after caching. Selective
                // reads must still reuse the complete result without recapturing.
                val promotion = turn.map.units.first { it.name == "Warrior" }.combat!!.promotions!!.choices.first { it.available }.name
                val definition = game.ruleset.unitPromotions.remove(promotion)!!
                try {
                    check(runCatching { captureNativeAdvice(player, turn, state) }.isFailure)
                    check(session.nativeAdvice(request.copy(cities = listOf(city))) == filtered)
                    check(session.nativeAdvice(request) == advice)
                } finally {
                    game.ruleset.unitPromotions[promotion] = definition
                }

                // Compare detached inputs and uncached selections, not a cache hit.
                // These changes are deliberately withheld from the observation.
                val captured = captureNativeAdvice(player, turn, state)
                val enemy = game.getCivilization("France")
                enemy.addGold(917)
                enemy.cities.first().population.setPopulation(12)
                val hidden = game.tileMap.values.first { it.aerialDistanceTo(player.cities.first().getCenterTile()) > 9 && it.isLand && it.getOwner() == null }
                hidden.setTileResource(game.ruleset.tileResources.getValue("Iron"))
                enemy.units.placeUnitNearTile(hidden.position, "Warrior")!!.health = 7
                val after = captureNativeAdvice(player, turn, state)
                check(captured == after) { "Undelivered foreign state affected native inputs" }
                check(ReadOnlyAutomation.select(captured.position) == ReadOnlyAutomation.select(after.position))
                val timedOut = NativeAdvicePlanner.compute("timeout-fixture", captured, request, deadlineNanos = System.nanoTime() - 1)
                check(!timedOut.available && timedOut.recommendations.isEmpty())
                check(timedOut.limitations.single().contains("deadline"))
                val expiredCache = NativeAdvicePlanner.compute("Rome", captured, request, deadlineNanos = System.nanoTime() - 1)
                check(!expiredCache.available && expiredCache.recommendations.isEmpty()) { "An expired request reused a cached answer" }
                val failedSelector = NativeAdvicePlanner.compute("selector-failure-fixture", captured.copy(commands = emptyMap()), request)
                check(!failedSelector.available && failedSelector.recommendations.isEmpty())
                check(failedSelector.limitations.single().contains("permitted inputs"))
                check(NativeAdvicePlanner.compute("selector-failure-fixture", captured, request).available) { "A selector failure poisoned the revision cache" }
                try {
                    ReadOnlyAutomation.select(captured.position) { true }
                    error("Selector ignored cancellation")
                } catch (_: InterruptedException) { }
            }
            observedHealingBoundary(Path.of(args[0]).resolve("healing"))
            observedTerrainBoundary(Path.of(args[0]).resolve("terrain"))
            captureFailureBoundary(Path.of(args[0]).resolve("capture-failure"))
            citizenCommandBoundary(Path.of(args[0]).resolve("citizens"))
            cityFocusPreviews(Path.of(args[0]).resolve("focus"))
            println("NATIVE_ADVICE_PASS")
        } catch (failure: Throwable) {
            failure.printStackTrace()
            kotlin.system.exitProcess(1)
        }
    }

    private fun cityFocusPreviews(root: Path) = SettlementPreviewProbe.fixture(root).use { runtime ->
        val game = runtime.game
        // This fixture clears serialized river flags directly. Use the native
        // setter to clear cached river adjacency before comparing a saved copy.
        for (tile in game.tileMap.values) for (neighbor in tile.neighbors) {
            tile.setConnectedByRiver(neighbor, true)
            tile.setConnectedByRiver(neighbor, false)
        }
        val player = game.getCivilization("Rome")
        val city = player.cities.first()
        city.cityConstructions.addBuilding("University")
        city.population.setPopulation(3)
        city.manualSpecialists = true
        city.population.specialistAllocations.add("Scientist", 1)
        city.reassignPopulation()
        city.lockedTiles.add(city.workedTiles.first())
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val turn = session.turn()
        val target = turn.cities.single { it.name == city.name }.handle
        val request = uncivai.player.PlayerInspectionRequest(uncivai.player.InspectionSubject.CITY_FOCUS, target = target)
        val before = json().toJson(game)
        val result = session.inspect(request)
        check(json().toJson(game) == before) { "Focus preview changed the live save" }
        val options = result.data.getValue("options") as kotlinx.serialization.json.JsonArray
        check(options.size == turn.cities.single { it.handle == target }.management!!.focusOptions.count { it.action.available })
        check(options.isNotEmpty())
        check(session.inspect(request).data == result.data) { "Focus preview depended on preceding alternatives" }
        for (value in options) {
            val option = value.jsonObject
            check(option.getValue("lockedTiles").toString().contains("x"))
            check(option.getValue("manualSpecialists").toString() == "true")
            check(option.getValue("specialists").jsonObject.getValue("Scientist").toString() == "1")
        }
        val enemy = game.getCivilization("France")
        enemy.addGold(957)
        enemy.cities.first().population.setPopulation(13)
        check(session.inspect(request).data == result.data) { "Private foreign state affected focus outcomes" }
        val unavailable = runCatching { session.inspect(request.copy(target = "unknown-city")) }.exceptionOrNull()
        check(unavailable is uncivai.player.PlayerSessionException && unavailable.reason == uncivai.player.PlayerSessionFailure.CITY_UNAVAILABLE)
        val option = options.first().jsonObject
        val selection = option.getValue("name").toString().trim('"')
        val fresh = session.turn()
        session.execute(TurnCommandRequest("choose-focus", fresh.epoch, fresh.revision, TurnCommandKind.SET_CITY_FOCUS, target = target, selection = selection))
        val after = session.turn().cities.single { it.handle == target }.development!!
        check(option.getValue("foodPerTurn").toString() == after.foodPerTurn.toString())
        check(option.getValue("yields").jsonObject.mapValues { it.value.toString().trim('"') } == after.yields.associate { it.stat to it.amount }) { "Predicted ${option.getValue("yields")}, actual ${after.yields}, sources ${city.cityStats.finalStatList}" }
        check(city.lockedTiles.isNotEmpty() && city.manualSpecialists)
        check(city.population.getNewSpecialists()["Scientist"] == 1)
        val other = PlayerHost(runtime).bindPlayer("France")
        val otherCity = other.turn().cities.first().handle
        check(other.inspect(request.copy(target = otherCity)).data.getValue("options").toString() == "[]")
    }

    private fun captureFailureBoundary(root: Path) = SettlementPreviewProbe.fixture(root).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        player.units.placeUnitNearTile(HexCoord(1, 0), "Warrior")!!.promotions.XP = 13
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val turn = session.turn()
        val request = NativeAdviceRequest(turn.epoch, turn.revision)
        val promotion = turn.map.units.first { it.name == "Warrior" }.combat!!.promotions!!.choices.first { it.available }.name
        val definition = game.ruleset.unitPromotions.remove(promotion)!!
        try {
            val failed = session.nativeAdvice(request)
            check(!failed.available && failed.recommendations.isEmpty())
            check(failed.limitations == listOf("Native advice unavailable: permitted inputs could not be captured."))
        } finally {
            game.ruleset.unitPromotions[promotion] = definition
        }
        check(session.nativeAdvice(request).available) { "Capture failure prevented a later valid refresh" }
    }

    private fun observedHealingBoundary(root: Path) = SettlementPreviewProbe.fixture(root).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val enemy = game.getCivilization("France")
        if (!player.knows(enemy)) player.diplomacyFunctions.makeCivilizationsMeet(enemy)
        player.getDiplomacyManager(enemy)!!.declareWar()
        val healer = player.units.placeUnitNearTile(HexCoord(1, 0), "Warrior")!!.apply { health = 85 }
        val hostile = enemy.units.placeUnitNearTile(HexCoord(4, 0), "Warrior")!!
        check(healer.currentTile.aerialDistanceTo(hostile.currentTile) == 3)
        player.viewableTiles = game.tileMap.values.toSet()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val visible = session.turn()
        val observed = visible.map.units.single { it.name == "Warrior" }
        check(observed.nearbyHostiles.isEmpty()) { "Fixture must exercise the two-tile warning's missing third ring" }
        val state = runtime.executor.turnSessions.getValue("Rome")
        val visibleInputs = captureNativeAdvice(player, visible, state)
        check(visibleInputs.position.units.single { it.id == observed.handle }.nearestEnemy == 3)
        check(ReadOnlyAutomation.select(visibleInputs.position).none { it.action.subject == observed.handle && it.action.kind == ReadOnlyAutomation.Kind.Heal })

        val concealed = setOf(game.tileMap[3, 0], game.tileMap[4, 0])
        player.viewableTiles = game.tileMap.values.filter { it !in concealed }.toSet()
        val hidden = session.turn()
        val hiddenInputs = captureNativeAdvice(player, hidden, state)
        check(hiddenInputs.position.units.single { it.id == observed.handle }.nearestEnemy == Int.MAX_VALUE)
        val hiddenAdvice = session.nativeAdvice(NativeAdviceRequest(hidden.epoch, hidden.revision))
        check(hiddenAdvice.recommendations.single { it.subject == observed.handle && it.category == "Heal" }.explanation.contains("no observed hostile"))
        hostile.removeFromTile()
        hostile.putInTile(game.tileMap[3, 0])
        hostile.health = 1
        check(captureNativeAdvice(player, hidden, state) == hiddenInputs) { "Hidden enemy movement or health changed advice inputs" }

        // The native safe-city exception still permits resting near an enemy;
        // its explanation must describe that exception accurately.
        healer.removeFromTile()
        healer.putInTile(player.cities.single().getCenterTile())
        player.viewableTiles = game.tileMap.values.toSet()
        val cityTurn = session.turn()
        val cityAdvice = session.nativeAdvice(NativeAdviceRequest(cityTurn.epoch, cityTurn.revision))
        val cityRest = cityAdvice.recommendations.single { it.subject == observed.handle && it.category == "Heal" }
        check(cityRest.explanation.contains("owned city") && !cityRest.explanation.contains("no observed hostile"))
    }

    private fun observedTerrainBoundary(root: Path) = SettlementPreviewProbe.fixture(root).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val unit = player.units.placeUnitNearTile(HexCoord(1, 0), "Warrior")!!
        game.tileMap[4, 0].setExplored(player, false)
        val turn = PlayerHost(runtime).bindPlayer("Rome").turn()
        val state = runtime.executor.turnSessions.getValue("Rome")
        val before = captureNativeAdvice(player, turn, state)
        val warrior = turn.map.units.single { it.name == "Warrior" }
        check(before.position.units.single { it.id == warrior.handle }.exploration.isNotEmpty())
        player.viewableTiles = setOf(unit.currentTile)
        game.tileMap[3, 0].setTerrainFeatures(listOf("Forest"))
        game.getCivilization("France").cities.single().expansion.takeOwnership(game.tileMap[3, 0])
        game.tileMap[4, 0].setExplored(player, true)
        check(before == captureNativeAdvice(player, turn, state)) { "Undelivered terrain, ownership or discovery changed advice inputs" }
    }

    private fun citizenCommandBoundary(root: Path) = SettlementPreviewProbe.fixture(root.resolve("game")).use { runtime ->
        val city = runtime.game.getCivilization("Rome").cities.single()
        city.clearWorkedTiles()
        city.cityStats.update()
        GameplayTestApi(runtime, root.resolve("tls")).use { api ->
            val token = api.playerToken("Rome")
            val turn = api.turn(token)
            check(turn.cities.single().development!!.unassignedPopulation > 0)
            val arguments = obj("epoch" to turn.epoch, "revision" to turn.revision)
            val advice = PlayerTurnJson.json.decodeFromString<NativeAdvice>(api.tool(token, "native_advice", arguments).toString())
            check(advice.available)
            check(advice == PlayerHost(runtime).bindPlayer("Rome").nativeAdvice(NativeAdviceRequest(turn.epoch, turn.revision)))
            val foreign = PlayerTurnJson.json.decodeFromString<NativeAdvice>(api.tool(api.playerToken("Greece"), "native_advice", arguments).toString())
            check(!foreign.available && foreign.recommendations.isEmpty())

            // The suggested command must pass the ordinary wire schema and native
            // command validation, with no special execution path for advice.
            val commands = advice.recommendations.mapNotNull { recommendation ->
                recommendation.command?.let { command ->
                    val supplied = command.getValue("arguments").jsonObject
                    PlayerTurnJson.json.decodeFromString<TurnCommandRequest>(
                        JsonObject(supplied + arguments + obj("requestId" to recommendation.id)).toString(),
                    )
                }
            }
            val assignment = commands.single { it.kind == TurnCommandKind.SET_WORKED_TILE }
            check(assignment.worked == true && assignment.enabled == null)
            val receipt = api.execute(token, assignment)
            check(api.execute(token, assignment) == receipt)
            val after = api.turn(token)
            check(after.cities.single().development!!.unassignedPopulation == turn.cities.single().development!!.unassignedPopulation - 1)
            check(city.workedTiles.any { it.x == assignment.destination!!.x && it.y == assignment.destination.y })
            val stale = PlayerTurnJson.json.decodeFromString<NativeAdvice>(api.tool(token, "native_advice", arguments).toString())
            check(!stale.available && stale.recommendations.isEmpty())
        }
    }
}
