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
                "map-knowledge" -> MapKnowledgeProbe.run(root)
                "map-knowledge-movement" -> MapKnowledgeProbe.movement(root)
                "map-knowledge-resources" -> MapKnowledgeProbe.resources(root)
                "map-knowledge-cities" -> MapKnowledgeProbe.cities(root)
                "settlement" -> settlement(root)
                "settlement-before-settler" -> settlementBeforeSettler(root)
                "automation" -> UnitAutomationProbe.run(root)
                "mechanics" -> MechanicsOverviewProbe.run(root)
                "coverage-controls" -> CoverageCompletionProbe.controls(root)
                "coverage-information" -> CoverageCompletionProbe.information(root)
                "civilization-profile" -> CivilizationProfileProbe.vanilla(root)
                "civilization-profile-gnk" -> CivilizationProfileProbe.godsAndKings(root)
                "expansion" -> MechanicsOverviewProbe.expansion(root)
                "construction" -> construction(root)
                "building-estimates" -> BuildingEstimatesProbe.run(root)
                "building-estimates-saved" -> BuildingEstimatesProbe.saved(root, Path.of(args[2]))
                "development" -> development(root)
                "development-tech" -> developmentTechnologies(root)
                "diplomacy" -> diplomacy(root)
                "settler-progress" -> settlerProgress(root)
                "military" -> military(root)
                "production-conversion" -> ConstructionOpportunitiesProbe.conversion(root)
                "production-policy" -> ConstructionOpportunitiesProbe.productionPolicy(root)
                "wonders" -> ConstructionOpportunitiesProbe.wonders(root)
                else -> error("Unknown fixture")
            }
            println("OPPORTUNITIES_${args[1]}_PASS")
        } catch (failure: Throwable) {
            failure.printStackTrace()
            kotlin.system.exitProcess(1)
        }
    }

    private fun settlementBeforeSettler(root: Path) = SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        player.units.getCivUnits().toList().forEach { it.destroy() }
        player.viewableTiles = game.tileMap.values.toSet()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val before = json().toJson(game)
        val turn = session.turn()
        val sites = checkNotNull(turn.settlement)
        check(json().toJson(game) == before) { "Advice without settlers mutated native state" }
        check(sites.settlers.isEmpty() && sites.eligibleSites > 0)
        check(sites.eligibleSites == game.tileMap.values.count { it.canBeSettled(player) })
        check(sites.candidates.all { it.nearestSettler == null && it.route == null })
        check(sites.candidates.all { it.nearestCity == turn.cities.single().handle && it.distance != null })

        // Fog changes neither native eligibility nor remembered site scores.
        val confirmed = sites.candidates.last().position
        val radius = maxOf(game.ruleset.modOptions.constants.minimalCityDistance, game.ruleset.modOptions.constants.minimalCityDistanceOnDifferentContinents)
        player.viewableTiles = (game.tileMap[confirmed.x, confirmed.y].getTilesInDistance(radius) + player.cities.single().getTiles()).toSet()
        val mixed = session.turn().settlement!!
        check(mixed.eligibleSites == sites.eligibleSites && mixed.recommendations == sites.recommendations)

        // Native restrictions affect the shortlist even before a settler exists.
        player.viewableTiles = player.cities.single().getTiles().toSet()
        val fogged = session.turn().settlement!!
        game.getCivilization("France").addCity(HexCoord(6, 0))
        player.viewableTiles = player.cities.single().getTiles().toSet()
        val hidden = session.turn().settlement!!
        check(hidden.eligibleSites < fogged.eligibleSites)
        check(hidden.candidates.all { game.tileMap[it.position.x, it.position.y].canBeSettled(player) })
        player.viewableTiles = game.tileMap.values.toSet()
        val revealed = session.turn().settlement!!
        check(revealed.eligibleSites == hidden.eligibleSites)

        // The same field remains present when the explored map is exhausted.
        val foreign = game.getCivilization("France").cities.first()
        for (tile in game.tileMap.values.filter { it.getOwner() == null }) foreign.expansion.takeOwnership(tile)
        player.viewableTiles = game.tileMap.values.toSet()
        val crowded = session.turn().settlement!!
        check(crowded.status == "NO_SITES_IN_EXPLORED_TERRITORY" && crowded.candidates.isEmpty())
        check(crowded.settlers.isEmpty())
    }

    private fun settlement(root: Path) = SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
        val player = runtime.game.getCivilization("Rome")
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val before = json().toJson(runtime.game)
        val sites = session.turn().settlement!!
        check(sites.eligibleSites == runtime.game.tileMap.values.count { it.canBeSettled(player) })
        check(sites.eligibleSites > 0)
        check(sites.candidates.zipWithNext().all { (a, b) -> a.score >= b.score })
        check(before == json().toJson(runtime.game)) { "Recommendations mutated native state" }
        // A crowded map has no remaining sites, even when settlers can still move.
        val foreign = runtime.game.getCivilization("France").cities.single()
        for (tile in runtime.game.tileMap.values.filter { it.getOwner() == null }) foreign.expansion.takeOwnership(tile)
        player.viewableTiles = runtime.game.tileMap.values.toSet()
        val crowded = session.turn().settlement!!
        check(runtime.game.tileMap.values.none { it.canBeSettled(player) })
        check(crowded.status == "NO_SITES_IN_EXPLORED_TERRITORY" && crowded.candidates.isEmpty())
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
            check("Granary" in turn.cities.single().constructionChoice!!.availableSinceChoice)
            runtime.game.turns = 2
            player.tech.addTechnology("Writing", false)
            turn = session.turn()
            expected = turn.cities.single().constructionChoice!!
            check(expected.availableSinceChoice.containsAll(listOf("Granary", "Library")))
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
            check("Walls" in session.turn().cities.single().constructionChoice!!.availableSinceChoice)
            command(TurnCommandKind.QUEUE_CONSTRUCTION, "Worker")
            command(TurnCommandKind.MOVE_CONSTRUCTION, index = 1, toIndex = 0)
            check(session.turn().cities.single().constructionChoice!!.chosenTurn == 3)
            check("Walls" !in session.turn().cities.single().constructionChoice!!.availableSinceChoice)
            command(TurnCommandKind.REMOVE_CONSTRUCTION, index = 0)
            check(session.turn().cities.single().constructionChoice!!.chosenTurn == 2)
            check("Walls" in session.turn().cities.single().constructionChoice!!.availableSinceChoice)
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

    private fun developmentTechnologies(root: Path) = SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val tiles = player.cities.single().getTiles().filter { !it.isCityCenter() }.toList()
        val spices = tiles[0]
        spices.setTileResource(game.ruleset.tileResources.getValue("Spices"))
        spices.setTerrainFeatures(listOf("Forest"))
        spices.setTerrainTransients()
        val fish = tiles[1]
        fish.setBaseTerrain(game.ruleset.terrains.getValue("Coast"))
        fish.setTileResource(game.ruleset.tileResources.getValue("Fish"))
        fish.setTerrainTransients()
        player.viewableTiles = game.tileMap.values.toSet()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        fun resources() = session.turn().economy!!.development!!.unimprovedResources.associateBy { it.resource }
        fun plantation() = resources().getValue("Spices").improvements.single { it.name == "Plantation" }

        val nativeBefore = json().toJson(game)
        val initial = resources()
        check(json().toJson(game) == nativeBefore) { "Resource prerequisites mutated native state" }
        check(initial.getValue("Spices").builder == "Worker")
        check(initial.getValue("Fish").builder == "Work Boats")
        check(plantation().missingTechnologies == listOf("Calendar", "Mining"))
        check(plantation().firstJob == "Remove Forest" && !plantation().siteAvailable)
        val fishingBoats = initial.getValue("Fish").improvements.single { it.name == "Fishing Boats" }
        check(fishingBoats.missingTechnologies == listOf("Sailing") && !fishingBoats.siteAvailable)

        player.tech.addTechnology("Calendar", false)
        val stillBlocked = plantation()
        check(stillBlocked.missingTechnologies == listOf("Mining"))
        check(stillBlocked.blockers == listOf("MissingTech") && !stillBlocked.siteAvailable)

        player.tech.addTechnology("Mining", false)
        val readyToClear = plantation()
        check(readyToClear.missingTechnologies.isEmpty() && readyToClear.blockers.isEmpty())
        check(readyToClear.firstJob == "Remove Forest" && !readyToClear.siteAvailable)
        spices.removeTerrainFeature("Forest")
        check(plantation().siteAvailable && plantation().firstJob == null)

        player.tech.addTechnology("Sailing", false)
        val unlockedBoats = resources().getValue("Fish").improvements.single { it.name == "Fishing Boats" }
        check(unlockedBoats.siteAvailable && unlockedBoats.missingTechnologies.isEmpty())
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
        check(opportunity.missingLuxuriesAvailableForTrade.single().name == "Silk")
        check(session.turn().diplomacyOpportunities!!.luxuryTradeBasis!!.contains("consent"))
    }

    private fun settlerProgress(root: Path) {
        val checkpoint = root.resolve("progress.json")
        lateinit var expected: uncivai.player.SettlerReview
        SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
            val game = runtime.game
            val player = game.getCivilization("Rome")
            val settler = player.units.getCivUnits().single()
            settler.removeFromTile()
            settler.putInTile(game.tileMap[0, 4])
            player.viewableTiles = setOf(settler.currentTile)
            runtime.executor.enableCheckpoints(checkpoint)
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val before = json().toJson(game)
            val initial = session.turn().settlement!!
            check(json().toJson(game) == before)
            val current = initial.settlers.single().currentSite
            check(current.canFoundNow == true && current.siteStatus == "ELIGIBLE")
            check(current.uncertainty == null && current.siteBlockers.isEmpty())
            val recommendations = initial.candidates.filter { it.position in initial.recommendations }
            check(recommendations.isNotEmpty() && recommendations.all { it.route != null })

            // A target is retained even when it is blocked and outside the top candidates.
            settler.action = "moveTo 0,1"
            val blocked = session.turn().settlement!!.settlers.single()
            check(blocked.destination!!.site.siteStatus == "BLOCKED")
            check(blocked.destination.score == null && blocked.destination.route.status == "ROUTE_OFFERED")
            check(blocked.recentDestinationChanges.size == 1)
            check(session.turn().settlement!!.settlers.single() == blocked)
            game.turns = 5
            val waiting = session.turn().settlement!!.settlers.single()
            check(waiting.firstObservedTurn == 0 && waiting.turnsObserved == 5 && waiting.turnsWithoutMovement == 5)
            settler.action = "moveTo 0,7"
            val replanned = session.turn().settlement!!.settlers.single()
            check(replanned.recentDestinationChanges.size == 2)
            check(replanned.destination!!.route.estimatedTurns != null)
            settler.currentMovement = 0f
            val exhausted = session.turn().settlement!!.settlers.single()
            check(exhausted.destination!!.route.status == "NO_MOVEMENT_REMAINING")
            check(exhausted.destination.route.estimatedTurns == null)
            settler.currentMovement = 2f
            settler.removeFromTile()
            settler.putInTile(game.tileMap[0, 5])
            player.viewableTiles = setOf(settler.currentTile)
            expected = session.turn().settlement!!.settlers.single()
            check(expected.turnsWithoutMovement == 0 && expected.turnsObserved == 5)
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val player = runtime.game.getCivilization("Rome")
            // Restore this fixture's deliberately restricted view, normally maintained by the engine.
            player.viewableTiles = setOf(player.units.getCivUnits().single().currentTile)
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(session.turn().settlement!!.settlers.single() == expected)
        }
    }

    private fun military(root: Path) = SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        player.units.placeUnitNearTile(HexCoord(0, 0), "Scout")!!
        player.units.placeUnitNearTile(HexCoord(1, 0), "Warrior")!!
        player.units.placeUnitNearTile(HexCoord(0, 1), "Archer")!!
        val neighbor = game.getCivilization("Greece").units.placeUnitNearTile(HexCoord(2, 1), "Warrior")!!
        val hidden = game.getCivilization("France").units.placeUnitNearTile(HexCoord(2, 2), "Archer")!!
        player.viewableTiles = game.tileMap.values.filter { it !== hidden.currentTile }.toSet()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val native = json().toJson(game)
        val turn = session.turn()
        check(json().toJson(game) == native)
        val readiness = turn.militaryReadiness!!
        check(readiness.roleCounts == mapOf("MELEE" to 1, "RANGED" to 1, "RECON" to 1))
        check(readiness.unitsExcludingRecon == 2)
        val scout = turn.map.units.single { it.name == "Scout" }
        check(readiness.cities.single().garrison == scout.handle)
        check(readiness.cities.single().nearbyDefenders.none { it.unit == scout.handle })
        check(readiness.cities.single().nearbyDefenders.size == 2)
        val visible = readiness.visibleBorderForces.single()
        check(visible.civilization == neighbor.civ.civName && !visible.atWar && visible.observedTurn == game.turns)
        check(readiness.recruitment.single().units.none { it == "Archer" })
        hidden.health = 1
        check(session.turn().militaryReadiness == readiness) { "Hidden unit changed readiness" }
        player.tech.addTechnology("Archery", false)
        check("Archer" in session.turn().militaryReadiness!!.recruitment.single().units)
    }
}
