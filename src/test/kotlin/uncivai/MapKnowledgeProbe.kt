package uncivai

import com.unciv.logic.battle.CityCombatant
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.tile.RoadStatus
import uncivai.player.ObservedTile
import uncivai.player.PlayerHost
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path

object MapKnowledgeProbe {
    fun cities(root: Path) {
        val checkpoint = root.resolve("checkpoint.json")
        lateinit var remembered: ObservedTile
        lateinit var position: TilePosition
        SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
            val game = runtime.game
            val player = game.getCivilization("Rome")
            val capital = game.getCivilization("France").cities.first()
            val center = capital.getCenterTile()
            position = TilePosition(center.position.x, center.position.y)
            val session = PlayerHost(runtime).bindPlayer("Rome")
            player.viewableTiles = player.cities.flatMap { it.getTiles() }.toSet()
            center.setExplored(player, false)
            check(session.turn().map.tiles.none { it.position == position })
            center.setExplored(player, true)
            fun observed() = session.turn().map.tiles.single { it.position == position }
            val distantReveal = observed()
            check(!distantReveal.visible && distantReveal.cityName == capital.name && distantReveal.cityIsOriginalMajorCapital == true)
            check(distantReveal.terrain == center.baseTerrain && distantReveal.cityHealth == null && distantReveal.units.isEmpty())

            val remote = game.tileMap[0, 5]
            val remotePosition = TilePosition(0, 5)
            val knowledge = runtime.executor.turnSessions.getValue(player.civID).settlementKnowledge
            knowledge.remove(remotePosition)
            player.setLastSeenImprovement(remote.position, null)
            remote.setImprovement("Farm")
            remote.setExplored(player, true)
            val revealedTerrain = knowledge.getValue(remotePosition)
            check(revealedTerrain.surface!!.improvement == null && revealedTerrain.surface.improvementPillaged == null)
            remote.setImprovement("Mine")
            remote.improvementIsPillaged = true
            remote.setExplored(player, true)
            check(knowledge.getValue(remotePosition) == revealedTerrain) { "A map reveal exposed an undisclosed improvement" }
            player.setLastSeenImprovement(remote.position, "Farm")
            remote.setExplored(player, true)
            val revealedImprovement = knowledge.getValue(remotePosition).surface!!
            check(revealedImprovement.improvement == "Farm" && revealedImprovement.improvementPillaged == null)

            player.viewableTiles = player.viewableTiles + center
            val visible = observed()
            check(visible.cityName == capital.name && visible.owner == "France" && visible.cityIsOriginalMajorCapital == true)
            check(visible.cityPopulation == capital.population.population && visible.cityHealth == capital.health)
            check(visible.cityMaxHealth == CityCombatant(capital).getMaxHealth())
            check(visible.cityDefensiveStrength == CityCombatant(capital).getDefendingStrength(null))
            player.viewableTiles = player.cities.flatMap { it.getTiles() }.toSet()
            remembered = observed()
            check(remembered.cityHealth == null && remembered.cityPopulation == null && remembered.cityDefensiveStrength == null && remembered.cityMaxHealth == null)
            check(remembered.cityName == capital.name && remembered.cityIsOriginalMajorCapital == true)

            game.turns++
            capital.name = "Changed under fog"
            capital.population.setPopulation(8)
            capital.cityConstructions.addBuilding("Walls")
            capital.moveToCiv(game.getCivilization("Greece"))
            capital.health = 61
            player.viewableTiles = player.cities.flatMap { it.getTiles() }.toSet()
            check(observed() == remembered) { "Hidden city growth, defenses, renaming or capture leaked" }
            runtime.executor.enableCheckpoints(checkpoint)
            runtime.executor.commitCheckpoint()
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val game = runtime.game
            val player = game.getCivilization("Rome")
            val center = game.tileMap[position.x, position.y]
            val session = PlayerHost(runtime).bindPlayer("Rome")
            fun observed() = session.turn().map.tiles.single { it.position == position }
            check(observed() == remembered) { "Restart refreshed hidden city information" }
            player.viewableTiles = player.viewableTiles + center
            val revealed = observed()
            val capital = checkNotNull(center.getCity())
            check(revealed.cityName == "Changed under fog" && revealed.owner == "Greece" && revealed.cityIsOriginalMajorCapital == true)
            check(revealed.observedTurn == game.turns && revealed.cityPopulation == 8 && revealed.cityHealth == 61) {
                "Visible city did not refresh: turn=${revealed.observedTurn}/${game.turns}, population=${revealed.cityPopulation}, health=${revealed.cityHealth}"
            }
            check(revealed.cityDefensiveStrength == CityCombatant(capital).getDefendingStrength(null))
            check(revealed.cityMaxHealth == CityCombatant(capital).getMaxHealth())

            player.viewableTiles = player.cities.flatMap { it.getTiles() }.toSet()
            val beforeRazing = observed()
            capital.destroyCity(overrideSafeties = true)
            player.viewableTiles = player.cities.flatMap { it.getTiles() }.toSet()
            check(observed() == beforeRazing) { "Hidden razing leaked" }
            player.viewableTiles = player.viewableTiles + center
            val empty = observed()
            check(empty.cityName == null && empty.cityIsOriginalMajorCapital == null && empty.cityDefensiveStrength == null)
        }
    }

    fun movement(root: Path) {
        val checkpoint = root.resolve("checkpoint.json")
        lateinit var brieflySeen: List<ObservedTile>
        SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
            val player = runtime.game.getCivilization("Rome")
            player.units.getCivUnits().toList().forEach { it.destroy() }
            player.units.placeUnitNearTile(HexCoord(0, 5), "Horseman")!!
            for (tile in runtime.game.tileMap.values) tile.setExplored(player, false)
            player.cache.updateViewableTiles()
            player.popupAlerts.clear()
            runtime.executor.enableCheckpoints(checkpoint)
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val before = session.turn()
            runtime.executor.scheduled = true
            runtime.executor.communicationPaused = true
            runtime.executor.pause()
            val boundary = runtime.executor.turnSnapshot()
            runtime.executor.resume()
            val unit = before.map.units.single { it.name == "Horseman" }
            session.execute(
                TurnCommandRequest(
                    "move",
                    before.epoch,
                    before.revision,
                    TurnCommandKind.MOVE_UNIT,
                    target = unit.handle,
                    destination = TilePosition(0, 9),
                ),
            )
            val old = before.map.tiles.map { it.position }.toSet()
            brieflySeen = session.turn().map.tiles.filter { it.position !in old && !it.visible }
            check(brieflySeen.isNotEmpty())
            check(brieflySeen.all { it.terrain == "Grassland" && it.features == emptyList<String>() && it.observedTurn != null })
            // Improvements do not alter sight range when native caches rebuild.
            for (tile in brieflySeen) runtime.game.tileMap[tile.position.x, tile.position.y].setImprovement("Farm")
            check(session.turn().map.tiles.filter { it.position in brieflySeen.map { seen -> seen.position } } == brieflySeen)
            runtime.executor.pause()
            runtime.executor.restoreTurn(boundary, "rewound")
            runtime.executor.resume()
            val retry = session.turn()
            check(retry.map.tiles.none { tile -> brieflySeen.any { it.position == tile.position } })
            session.execute(
                TurnCommandRequest(
                    "retry",
                    retry.epoch,
                    retry.revision,
                    TurnCommandKind.MOVE_UNIT,
                    target = unit.handle,
                    destination = TilePosition(0, 9),
                ),
            )
            check(session.turn().map.tiles.filter { it.position in brieflySeen.map { seen -> seen.position } } == brieflySeen)
            runtime.executor.commitCheckpoint()
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(session.turn().map.tiles.filter { it.position in brieflySeen.map { seen -> seen.position } } == brieflySeen)
        }
    }

    fun resources(root: Path) = SettlementPreviewProbe.fixture(root).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val tile = game.tileMap[0, 5]
        val resource = game.ruleset.tileResources.getValue("Iron")
        tile.setTileResource(resource)
        tile.setTerrainFeatures(listOf("Forest"))
        val position = TilePosition(tile.position.x, tile.position.y)
        val session = PlayerHost(runtime).bindPlayer("Rome")
        check(session.turn().map.tiles.single { it.position == position }.resource == null)
        val known = runtime.executor.turnSessions.getValue(player.civID).settlementKnowledge.getValue(position)
        player.viewableTiles = player.cities.flatMap { it.getTiles() }.toSet()
        // Hidden development must not be used in the technology-triggered refresh.
        tile.setTerrainFeatures(emptyList())
        tile.setImprovement("Mine")
        game.getCivilization("France").cities.first().expansion.takeOwnership(tile)
        player.tech.addTechnology(resource.revealedBy!!, false)
        player.viewableTiles = player.cities.flatMap { it.getTiles() }.toSet()
        val revealed = session.turn()
        val observed = revealed.map.tiles.single { it.position == position }
        check(!observed.visible && observed.resource == "Iron" && observed.owner == null && observed.features == listOf("Forest"))
        val candidates = revealed.settlement!!.candidates.filter { candidate ->
            game.tileMap[candidate.position.x, candidate.position.y].aerialDistanceTo(tile) <= 3
        }
        check(candidates.isNotEmpty())
        check(candidates.all { candidate -> candidate.resources.any { it.position == position && it.name == "Iron" && it.owner == null } })
        val refreshed = runtime.executor.turnSessions.getValue(player.civID).settlementKnowledge.getValue(position)
        check(refreshed.surface == known.surface && refreshed.observedTurn == known.observedTurn && refreshed.owner == known.owner)
        check(refreshed.yieldScore > known.yieldScore)
        check(refreshed.resourceImprovements.single { it.name == "Mine" }.firstJob == "Remove Forest")
        check(session.turn().settlement == revealed.settlement) { "Repeated reads changed resource scores" }
    }

    fun run(root: Path) {
        val checkpoint = root.resolve("checkpoint.json")
        lateinit var position: TilePosition
        lateinit var remembered: ObservedTile
        SettlementPreviewProbe.fixture(root.resolve("initial")).use { runtime ->
            val game = runtime.game
            val player = game.getCivilization("Rome")
            val tile = game.tileMap.values.first { it.getOwner() == null && player.cities.all { city -> city.getCenterTile().aerialDistanceTo(it) > 4 } }
            position = TilePosition(tile.position.x, tile.position.y)
            tile.setTerrainFeatures(arrayListOf("Forest"))
            tile.setImprovement("Farm")
            tile.roadStatus = RoadStatus.Road
            player.viewableTiles = player.cities.flatMap { it.getTiles() }.toSet()
            // Simulate an imported save with explored terrain but no retained observation.
            runtime.executor.turnSessions.getValue(player.civID).settlementKnowledge.remove(position)
            runtime.executor.enableCheckpoints(checkpoint)
            val session = PlayerHost(runtime).bindPlayer("Rome")
            fun observed() = session.turn().map.tiles.single { it.position == position }
            // A loaded save may contain explored tiles with no host observation.
            val unknown = observed()
            check(!unknown.visible && unknown.observedTurn == null)
            check(unknown.features == null && unknown.road == null && unknown.improvementPillaged == null)
            player.viewableTiles = game.tileMap.values.toSet()
            player.setLastSeenImprovement(tile.position, "Farm")
            check(observed().features == listOf("Forest"))
            player.viewableTiles = player.cities.flatMap { it.getTiles() }.toSet()
            remembered = observed()
            check(remembered.road == "Road" && remembered.improvement == "Farm")
            val mutations = listOf<() -> Unit>(
                { tile.setTerrainFeatures(arrayListOf()) },
                { tile.roadStatus = RoadStatus.Railroad },
                { tile.roadIsPillaged = true },
                { tile.improvementIsPillaged = true },
                { tile.setBaseTerrain(game.ruleset.terrains.getValue("Plains")) },
                { tile.setImprovement("Mine") },
            )
            for (mutate in mutations) {
                mutate()
                check(observed() == remembered) { "Hidden terrain or infrastructure change leaked" }
            }
            // Native notifications can reveal an improvement without revealing
            // its tile's terrain or pillage state.
            player.setLastSeenImprovement(tile.position, "Barbarian encampment")
            val notified = observed()
            check(notified.improvement == "Barbarian encampment" && notified.improvementPillaged == null)
            check(notified.copy(improvement = remembered.improvement, improvementPillaged = remembered.improvementPillaged) == remembered)
            player.setLastSeenImprovement(tile.position, "Farm")
            runtime.executor.commitCheckpoint()
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val player = runtime.game.getCivilization("Rome")
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(session.turn().map.tiles.single { it.position == position } == remembered)
            runtime.game.turns++
            player.viewableTiles = runtime.game.tileMap.values.toSet()
            val revealed = session.turn().map.tiles.single { it.position == position }
            check(revealed.visible && revealed.observedTurn == runtime.game.turns)
            check(revealed.features == emptyList<String>() && revealed.terrain == "Plains")
            check(revealed.road == "Railroad" && revealed.roadPillaged == true)
            check(revealed.improvement == "Mine")
        }
    }
}
