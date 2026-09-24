package uncivai

import com.unciv.json.json
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.tile.RoadStatus
import uncivai.player.PlayerHost
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path

object ObservationOpportunitiesProbe {
    @JvmStatic fun main(args: Array<String>) {
        try {
            val root = Path.of(args[0])
            when (args[1]) {
                "settlement" -> settlement(root)
                "construction" -> construction(root)
                "development" -> development(root)
                "diplomacy" -> diplomacy(root)
                else -> error("Unknown fixture")
            }
            println("OPPORTUNITIES_${args[1]}_PASS")
        } catch (failure: Throwable) {
            failure.printStackTrace()
            kotlin.system.exitProcess(1)
        }
    }

    private fun settlement(root: Path) = SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
        val player = runtime.game.getCivilization("Rome")
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val before = json().toJson(runtime.game)
        val sites = session.turn().settlement!!
        check(sites.knownEligibleSites == runtime.game.tileMap.values.count { it.canBeSettled(player) })
        check(sites.knownEligibleSites > 0 && sites.uncertainSites == 0)
        check(sites.candidates.zipWithNext().all { (a, b) -> a.score >= b.score })
        check(before == json().toJson(runtime.game)) { "Recommendations mutated native state" }
        // A crowded map has no remaining sites, even when settlers can still move.
        val foreign = runtime.game.getCivilization("France").cities.single()
        for (tile in runtime.game.tileMap.values.filter { it.getOwner() == null }) foreign.expansion.takeOwnership(tile)
        player.viewableTiles = runtime.game.tileMap.values.toSet()
        val crowded = session.turn().settlement!!
        check(runtime.game.tileMap.values.none { it.canBeSettled(player) })
        check(crowded.status == "NO_KNOWN_SITES" && crowded.candidates.isEmpty())
        check(crowded.blockedSites == crowded.exploredTiles)
    }

    private fun construction(root: Path) {
        val checkpoint = root.resolve("checkpoint.json")
        var expected: uncivai.player.ConstructionChoice? = null
        SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
            val player = runtime.game.getCivilization("Rome")
            player.cities.single().cityConstructions.constructionQueue.clear()
            runtime.executor.enableCheckpoints(checkpoint)
            val session = PlayerHost(runtime).bindPlayer("Rome")
            var turn = session.turn()
            val city = turn.cities.single()
            session.execute(TurnCommandRequest("select", turn.epoch, turn.revision, TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, "Nothing"))
            turn = session.turn()
            check(turn.cities.single().constructionChoice!!.chosenTurn == 0)
            runtime.game.turns = 1
            player.tech.addTechnology("Pottery", false)
            turn = session.turn()
            check("Granary" in turn.cities.single().constructionChoice!!.newOptions)
            runtime.game.turns = 2
            player.tech.addTechnology("Writing", false)
            turn = session.turn()
            expected = turn.cities.single().constructionChoice!!
            check(expected.newOptions.containsAll(listOf("Granary", "Library")))
            check(expected.chosenTurn == 0 && expected.turnsObserved == 2)
            // Unchanged reads do not reset the original comparison baseline.
            check(session.turn().cities.single().constructionChoice == expected)
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(session.turn().cities.single().constructionChoice == expected)
            var number = 0
            fun command(kind: TurnCommandKind, selection: String? = null, index: Int? = null, toIndex: Int? = null) {
                val turn = session.turn()
                session.execute(TurnCommandRequest("queue-${number++}", turn.epoch, turn.revision, kind, turn.cities.single().handle, selection, index = index, toIndex = toIndex))
            }
            command(TurnCommandKind.QUEUE_CONSTRUCTION, "Worker")
            runtime.game.turns = 3
            runtime.game.getCivilization("Rome").tech.addTechnology("Masonry", false)
            check("Walls" in session.turn().cities.single().constructionChoice!!.newOptions)
            command(TurnCommandKind.QUEUE_CONSTRUCTION, "Worker")
            command(TurnCommandKind.MOVE_CONSTRUCTION, index = 1, toIndex = 0)
            check(session.turn().cities.single().constructionChoice!!.chosenTurn == 3)
            check("Walls" !in session.turn().cities.single().constructionChoice!!.newOptions)
            command(TurnCommandKind.REMOVE_CONSTRUCTION, index = 0)
            check(session.turn().cities.single().constructionChoice!!.chosenTurn == 2)
            check("Walls" in session.turn().cities.single().constructionChoice!!.newOptions)
        }
    }

    private fun development(root: Path) = SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val capital = player.cities.single()
        val second = player.addCity(HexCoord(0, 5))
        val tiles = capital.getTiles().filter { !it.isCityCenter() }.sortedBy { it.position.x }.toList()
        tiles[0].setTileResource(game.ruleset.tileResources.getValue("Wheat"))
        tiles[1].setTileResource(game.ruleset.tileResources.getValue("Dyes"))
        tiles[2].setTileResource(game.ruleset.tileResources.getValue("Uranium"))
        player.viewableTiles = game.tileMap.values.toSet()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        var development = session.turn().economy!!.development!!
        check(development.unimprovedResources.map { it.resource }.containsAll(listOf("Wheat", "Dyes")))
        check(development.unimprovedResources.none { it.resource == "Uranium" })
        val dyes = development.unimprovedResources.single { it.resource == "Dyes" }
        check(dyes.improvements.any { !it.siteAvailable && "Calendar" in it.missingTechnologies })
        check(!development.roadsAvailable && development.unconnectedCities.isEmpty())
        player.tech.addTechnology("Calendar", false)
        player.tech.addTechnology(game.ruleset.roadImprovement!!.techRequired!!, false)
        player.cache.updateCitiesConnectedToCapital()
        development = session.turn().economy!!.development!!
        check(development.unimprovedResources.single { it.resource == "Dyes" }.improvements.any { it.siteAvailable })
        check(development.roadsAvailable && second.name in development.unconnectedCities)
        for (y in 0..5) game.tileMap[0, y].setRoadStatus(RoadStatus.Road, player)
        player.cache.updateCitiesConnectedToCapital()
        check(second.name !in session.turn().economy!!.development!!.unconnectedCities)
        player.tech.addTechnology(game.ruleset.railroadImprovement!!.techRequired!!, false)
        development = session.turn().economy!!.development!!
        check(development.railroadsAvailable && second.name in development.citiesWithoutRailroadConnection)
        for (y in 0..5) game.tileMap[0, y].setRoadStatus(RoadStatus.Railroad, player)
        player.cache.updateCitiesConnectedToCapital()
        check(second.name !in session.turn().economy!!.development!!.citiesWithoutRailroadConnection)
        tiles[0].setImprovement(game.ruleset.tileImprovements.getValue("Farm"), player)
        check(session.turn().economy!!.development!!.unimprovedResources.none { it.resource == "Wheat" })
    }

    private fun diplomacy(root: Path) = SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val other = game.getCivilization("Greece")
        fun sources(civ: com.unciv.logic.civilization.Civilization, resource: String, amount: Int) {
            civ.tech.addTechnology("Calendar", false)
            civ.cities.single().getTiles().filter { !it.isCityCenter() }.take(amount).forEach {
                it.setTileResource(game.ruleset.tileResources.getValue(resource))
                it.setImprovement(game.ruleset.tileImprovements.getValue("Plantation"), civ)
            }
            civ.cache.updateCivResources()
        }
        sources(player, "Dyes", 2)
        sources(other, "Silk", 1)
        val session = PlayerHost(runtime).bindPlayer("Rome")
        check(session.turn().diplomacyOpportunities!!.luxuryTrades.none { it.civilizationId == other.civID })
        player.diplomacyFunctions.makeCivilizationsMeet(other)
        val opportunity = session.turn().diplomacyOpportunities!!.luxuryTrades.single { it.civilizationId == other.civID }
        check(opportunity.mode == "SHARED")
        check(opportunity.spareLuxuriesToOffer.single().name == "Dyes" && opportunity.spareLuxuriesToOffer.single().amount == 1)
        check(opportunity.missingLuxuriesOffered.single().name == "Silk")
    }
}
