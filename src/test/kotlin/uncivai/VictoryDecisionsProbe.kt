package uncivai

import com.unciv.json.json
import com.unciv.logic.civilization.CivFlags
import com.unciv.logic.civilization.PlayerTurnRequirements
import com.unciv.logic.map.mapunit.UnitSettlement
import uncivai.player.CityStateVoteWindow
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

/** Prepared late-game Vanilla positions; every tested decision uses the player API. */
object VictoryDecisionsProbe {
    private val players = listOf("Rome", "Greece", "France")
    private val tokens = players.associateWith { it.first().toString().repeat(43) }

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        when (args[1]) {
            "scientific" -> scientific(root)
            "spaceship-production" -> spaceshipProduction(root)
            "elections" -> elections(root)
            "city-state-vote-timing" -> cityStateVoteTiming(root)
            "privacy" -> privacy(root)
            "recovery" -> recovery(root)
            "cultural" -> cultural(root)
            "time" -> time(root)
            else -> error(args[1])
        }
    }

    internal fun create(root: Path, cityStates: Int = 0): HeadlessRuntime = HeadlessRuntime.createVanillaGame(root, 412L, cityStates = cityStates).also { runtime ->
        runtime.executor.accessGame { game ->
            for (id in players) {
                val player = game.getCivilization(id)
                val city = checkNotNull(UnitSettlement.tryFoundCity(player.units.getCivUnits().single { it.name == "Settler" }))
                city.cityConstructions.constructionQueue = ArrayList(List(12) { "Warrior" })
                player.tech.techsToResearch = arrayListOf("Pottery", "Writing", "Philosophy", "Theology", "Education")
                player.popupAlerts.clear()
                player.notifications.clear()
            }
        }
    }

    internal fun arrangeScience(runtime: HeadlessRuntime) {
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val capital = player.getCapital()!!
        capital.cityConstructions.addBuilding("Apollo Program")
        for (part in game.ruleset.victories.getValue("Scientific").requiredSpaceshipParts) player.victoryManager.currentsSpaceshipParts.add(part, 1)
        player.victoryManager.currentsSpaceshipParts.add("SS Booster", -1)
        capital.getCenterTile().civilianUnit?.destroy()
        val part = checkNotNull(game.tileMap.placeUnitNearTile(capital.location, "SS Booster", player))
        check(part.currentTile == capital.getCenterTile())
        part.currentMovement = 0f
    }

    internal fun arrangeVote(runtime: HeadlessRuntime, results: Boolean = false, meetAll: Boolean = true) {
        val game = runtime.game
        game.getCivilization("Rome").getCapital()!!.cityConstructions.addBuilding("United Nations")
        for (id in players) {
            val player = game.getCivilization(id)
            if (meetAll) {
                for (other in players.filter { it != id }.map(game::getCivilization)) {
                    if (!player.knows(other)) player.diplomacyFunctions.makeCivilizationsMeet(other)
                }
            }
            player.addFlag(
                CivFlags.TurnsTillNextDiplomaticVote.name,
                if (results) {
                    8
                } else if (id == game.currentPlayer) {
                    0
                } else {
                    1
                },
            )
            if (results || id == game.currentPlayer) player.addFlag(CivFlags.ShowDiplomaticVotingResults.name, if (results) 0 else 1)
            player.popupAlerts.clear()
            player.notifications.clear()
        }
    }

    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection).reviewedFor(turn)

    private fun finishTurn(api: GameplayTestApi, token: String) {
        repeat(60) {
            val turn = api.readAlerts(token)
            val city = turn.cities.firstOrNull { !it.puppet && it.constructionQueue.isEmpty() }
            val next = when {
                turn.freeTechnologies > 0 -> command(turn, TurnCommandKind.CHOOSE_FREE_TECHNOLOGY, selection = turn.researchOptions.first())
                turn.freeGreatPeople > 0 -> command(turn, TurnCommandKind.CHOOSE_FREE_GREAT_PERSON, selection = turn.greatPersonOptions.first())
                turn.researchQueue.isEmpty() -> command(turn, TurnCommandKind.SET_RESEARCH_QUEUE).copy(queue = listOf(turn.researchOptions.first()))
                city != null -> command(turn, TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, city.constructionOptions.first())
                turn.pending.any { it.kind == "Policy" } -> command(turn, TurnCommandKind.ADOPT_POLICY, selection = turn.policyOptions.first())
                turn.canEndTurn -> command(turn, TurnCommandKind.END_TURN)
                else -> error("Unexpected fixture blocker: ${turn.pending}; ${turn.phase}")
            }
            val receipt = api.execute(token, next)
            check(api.execute(token, next) == receipt)
            if (next.kind == TurnCommandKind.END_TURN) return
        }
        error("Fixture turn budget exhausted")
    }

    private fun scientific(root: Path) {
        val checkpoint = root.resolve("checkpoint.json")
        lateinit var request: TurnCommandRequest
        lateinit var expected: String
        create(root.resolve("initial")).use { runtime ->
            arrangeScience(runtime)
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls"), tokens).use { api ->
                val token = tokens.getValue("Rome")
                val before = api.turn(token)
                val part = before.map.units.single { it.spaceshipAssembly != null }
                check(part.movement.startsWith("0/") && part.spaceshipAssembly!!.available)
                check(before.result == null)
                request = command(before, TurnCommandKind.ADD_SPACESHIP_PART, part.handle)
                api.reject(tokens.getValue("Greece"), request, "NOT_YOUR_TURN")
                api.reject(token, request.copy(target = "guessed-part"), "OPTION_UNAVAILABLE")
                api.reject(token, request.copy(selection = "SS Booster"), "INVALID_ARGUMENT")
                val receipt = api.execute(token, request)
                check(receipt.victory!!.part == "SS Booster" && receipt.victory.assembled == 3)
                check(api.execute(token, request) == receipt)
                val after = api.turn(token)
                check(after.phase == "FINISHED" && !after.canEndTurn && after.result!!.victoryType == "Scientific")
                check(after.map.units.none { it.handle == part.handle })
                for (id in players) {
                    val result = PlayerHost(runtime).bindPlayer(id).result().result!!
                    check(result.victoryType == "Scientific" && result.outcome == if (id == "Rome") "Victory" else "Defeat")
                }
                api.reject(token, command(after, TurnCommandKind.ADD_SPACESHIP_PART, part.handle), "COMMAND_UNAVAILABLE")
                expected = PlayerTurnJson.encode(receipt)
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(PlayerTurnJson.encode(session.execute(request)) == expected)
            check(session.result().result!!.victoryType == "Scientific")
            check(runtime.game.getCivilization("Rome").victoryManager.currentsSpaceshipParts["SS Booster"] == 3)
        }
    }

    private fun spaceshipProduction(root: Path) = create(root.resolve("initial")).use { runtime ->
        val game = runtime.game
        game.gameParameters.noBarbarians = true
        val rome = game.getCivilization("Rome")
        val capital = rome.getCapital()!!
        val site = capital.getCenterTile().getTilesAtDistance(4).first { tile ->
            tile.isLand && !tile.isImpassible() && tile.getOwner() == null &&
                game.getCities().all { it.getCenterTile().aerialDistanceTo(tile) >= 4 } &&
                tile.neighbors.count { it.isLand && !it.isCityCenter() } >= 5
        }
        val factory = rome.addCity(site.position)
        GameplayTestApi(runtime, root.resolve("tls"), tokens).use { api ->
            val token = tokens.getValue("Rome")
            fun offers() = api.turn(token).cities.single { it.name == factory.name }.constructionOptions
            check(offers().none { it.startsWith("SS ") })
            // Prepare an industrial-era position; no part, production or assembly counters are supplied.
            for (id in players) {
                val player = game.getCivilization(id)
                for (technology in game.ruleset.technologies.keys.filterNot { it == "Future Tech" }) player.tech.addTechnology(technology, false)
                player.tech.techsToResearch = arrayListOf("Future Tech")
                player.addGold(10000)
                for (city in player.cities) city.cityConstructions.constructionQueue = arrayListOf("Gold")
                player.popupAlerts.clear()
                player.notifications.clear()
            }
            check(offers().none { it.startsWith("SS ") }) { "Parts were offered without Apollo" }
            capital.cityConstructions.addBuilding("Apollo Program")
            for (tile in rome.cities.flatMap { it.getTiles().toList() }) if (tile.resource == "Aluminum") tile.setTileResource(null)
            rome.cache.updateCivResources()
            check(offers().none { it.startsWith("SS ") }) { "Parts were offered without Aluminum" }
            val fields = factory.getTiles().filter { !it.isCityCenter() && it.isLand }.toList()
            check(fields.size >= 5)
            for ((index, tile) in fields.withIndex()) {
                tile.setBaseTerrain(game.ruleset.terrains.getValue("Grassland"))
                tile.setTerrainFeatures(if (index < 2) emptyList() else listOf("Hill"))
                tile.setTileResource(null)
                tile.setImprovement(if (index < 2) "Farm" else "Mine", rome)
            }
            fields[2].setTileResource("Aluminum")
            fields[2].resourceAmount = 2 // One for the factory, one for the part being transported.
            fields[3].setTileResource("Coal")
            fields[3].resourceAmount = 1
            for (building in listOf("Workshop", "Factory", "Spaceship Factory")) factory.cityConstructions.addBuilding(building)
            factory.population.setPopulation(5)
            factory.reassignPopulation(resetLocked = true)
            // Ordinary railroads connect the two cities; movement still uses advertised API routes.
            for (tile in capital.getCenterTile().getTilesInDistance(5).filter { it.isLand && !it.isImpassible() && (it.getOwner() == null || it.getOwner() === rome) }) {
                tile.setRoadStatus(com.unciv.logic.map.tile.RoadStatus.Railroad, rome)
            }
            factory.cityStats.update()
            rome.cache.updateCivResources()
            rome.cache.updateViewableTiles()
            check(rome.getResourceAmount("Aluminum") == 1)
            check(rome.victoryManager.currentsSpaceshipParts.isEmpty())
            val built = mutableListOf<String>()
            val sequence = listOf("SS Booster", "SS Booster", "SS Booster", "SS Cockpit", "SS Engine", "SS Stasis Chamber")
            for (name in sequence) {
                var turn = api.readAlerts(token)
                val city = turn.cities.single { it.name == factory.name }
                check(name in city.constructionOptions) { "$name not offered: ${city.constructionOptions}" }
                check(factory.cityConstructions.getWorkDone(name) == 0) { "Part started with supplied production" }
                api.reject(token, command(turn, TurnCommandKind.BUY_CONSTRUCTION, city.handle, name), "OPTION_UNAVAILABLE")
                api.execute(token, command(turn, TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, name))
                var rounds = 0
                while (rome.units.getCivUnits().none { it.name == name }) {
                    check(rounds++ < 70) { "$name did not complete: ${factory.cityConstructions.getWorkDone(name)} production; ${factory.cityConstructions.constructionQueue}" }
                    for (id in players) finishTurn(api, tokens.getValue(id))
                }
                built.add(name)
                check(rome.getResourceAmount("Aluminum") == 0)
                turn = api.turn(token)
                var part = turn.map.units.single { it.name == name }
                check(part.position != uncivai.player.TilePosition(capital.location.x, capital.location.y))
                check(part.spaceshipAssembly?.available == false)
                api.reject(token, command(turn, TurnCommandKind.ADD_SPACESHIP_PART, part.handle), "OPTION_UNAVAILABLE")
                check(turn.cities.single { it.name == factory.name }.constructionOptions.none { it.startsWith("SS ") }) { "Another part was offered without spare Aluminum" }
                var moves = 0
                while (part.spaceshipAssembly?.available != true) {
                    check(moves++ < 15) { "Part could not reach the capital" }
                    val destination = part.moves.minByOrNull { move ->
                        game.tileMap[com.unciv.logic.map.HexCoord(move.destination.x, move.destination.y)].aerialDistanceTo(capital.getCenterTile())
                    }
                    if (destination == null) {
                        for (id in players) finishTurn(api, tokens.getValue(id))
                    } else {
                        api.execute(token, command(turn, TurnCommandKind.MOVE_UNIT, part.handle).copy(destination = destination.destination))
                    }
                    turn = api.turn(token)
                    part = turn.map.units.single { it.handle == part.handle }
                }
                val receipt = api.execute(token, command(turn, TurnCommandKind.ADD_SPACESHIP_PART, part.handle))
                check(receipt.victory!!.assembled == built.count { it == name })
                check(rome.getResourceAmount("Aluminum") == 1) { "Assembly did not release the unit's Aluminum" }
                if (built.size < 6) check(game.victoryData == null)
                if (built.count { it == "SS Booster" } == 3) check("SS Booster" !in offers()) { "Completed boosters did not count toward the limit" }
                println("SPACE-PRODUCTION: $name built in $rounds rounds, transported and assembled; ${built.size}/6")
            }
            check(game.victoryData!!.victoryType == "Scientific")
            check(rome.units.getCivUnits().none { it.name.startsWith("SS ") })
            check(PlayerHost(runtime).bindPlayer("Rome").result().result!!.outcome == "Victory")
        }
    }

    private fun privacy(root: Path) = create(root.resolve("initial")).use { runtime ->
        arrangeVote(runtime, meetAll = false)
        val game = runtime.game
        val rome = game.getCivilization("Rome")
        rome.diplomacyFunctions.makeCivilizationsMeet(game.getCivilization("Greece"))
        rome.popupAlerts.clear()
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val beforeNative = json().toJson(game)
        val before = session.turn()
        check(before.victory.ballot!!.candidates.map { it.civilizationId } == listOf("Greece"))
        check(before.victory.votingResults == null)
        check(json().toJson(game) == beforeNative)
        game.diplomaticVictoryVotesCast["Greece"] = "France"
        game.diplomaticVictoryVotesCast["France"] = "France"
        check(session.turn() == before) { "Opponents' early ballots affected the observation or revision" }
        for (choice in listOf("Rome", "France", "not-a-player")) {
            reject(PlayerSessionFailure.OPTION_UNAVAILABLE) { session.execute(command(before, TurnCommandKind.CAST_DIPLOMATIC_VOTE, selection = choice)) }
        }
        rome.addFlag(CivFlags.ShowDiplomaticVotingResults.name, 0)
        game.diplomaticVictoryVotesCast["Rome"] = null
        val shown = session.turn().victory.votingResults!!
        check(shown.rows.map { it.civilizationId }.toSet() == setOf("Rome", "Greece"))
        check(shown.rows.single { it.civilizationId == "Greece" }.let { it.choice == "UNMET_CIVILIZATION" && it.votedFor == null })
        check(!PlayerTurnJson.json.encodeToString(shown).contains("France"))
        // A winning unmet candidate must not enter a formatted winner string either.
        game.diplomaticVictoryVotesCast["Rome"] = "France"
        val winner = session.turn().victory.votingResults!!
        check(winner.winner == null && winner.unmetWinner)
        check(!PlayerTurnJson.json.encodeToString(winner).contains("France"))
        // Requirements outside Vanilla remain explicit and unhandled, never silently dismissed.
        rome.religionManager.freeBeliefs["Follower"] = 1
        val unsupported = session.turn()
        check(unsupported.pending.any { it.kind == PlayerTurnRequirements.Kind.ReformReligion.name && !it.supported })
        reject(PlayerSessionFailure.TURN_BLOCKED) { session.execute(command(unsupported, TurnCommandKind.END_TURN)) }
    }

    private fun elections(root: Path) {
        val checkpoint = root.resolve("checkpoint.json")
        lateinit var ack: TurnCommandRequest
        lateinit var ackReceipt: String
        create(root.resolve("initial")).use { runtime ->
            arrangeVote(runtime)
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls"), tokens).use { api ->
                for (id in players) {
                    val token = tokens.getValue(id)
                    val turn = api.turn(token)
                    check(turn.victory.ballot!!.canVote && !turn.canEndTurn) { "$id: ${turn.victory}; ${turn.phase}" }
                    api.reject(token, command(turn, TurnCommandKind.END_TURN), "TURN_BLOCKED")
                    val ballot = command(
                        turn,
                        if (id == "Greece") TurnCommandKind.ABSTAIN_DIPLOMATIC_VOTE else TurnCommandKind.CAST_DIPLOMATIC_VOTE,
                        selection = if (id == "Rome") {
                            "Greece"
                        } else if (id == "France") {
                            "Rome"
                        } else {
                            null
                        },
                    )
                    val receipt = api.execute(token, ballot)
                    check(api.execute(token, ballot) == receipt)
                    check(api.turn(token).victory.ballot!!.let { it.hasVoted && !it.canVote })
                    finishTurn(api, token)
                }
                val turn = api.turn(tokens.getValue("Rome"))
                val results = turn.victory.votingResults!!
                check(results.winner == null && !results.unmetWinner && results.minimumVotes == 3)
                check(results.rows.single { it.civilizationId == "Greece" }.votesReceived == 2)
                check(results.rows.single { it.civilizationId == "Greece" }.choice == "ABSTAINED")
                check(!turn.canEndTurn)
                ack = command(turn, TurnCommandKind.ACKNOWLEDGE_DIPLOMATIC_RESULTS)
                val receipt = api.execute(tokens.getValue("Rome"), ack)
                check(receipt.victory!!.acknowledgedResults == results)
                ackReceipt = PlayerTurnJson.encode(receipt)
                check(api.turn(tokens.getValue("Rome")).victory.votingResults == null)
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(PlayerTurnJson.encode(session.execute(ack)) == ackReceipt)
            GameplayTestApi(runtime, root.resolve("tls-restored"), tokens).use { api ->
                var secondBallots = 0
                repeat(45) {
                    if (runtime.game.victoryData != null) return@repeat
                    val id = runtime.game.currentPlayer
                    val token = tokens.getValue(id)
                    var turn = api.turn(token)
                    if (turn.victory.votingResults?.canAcknowledge == true) {
                        api.execute(token, command(turn, TurnCommandKind.ACKNOWLEDGE_DIPLOMATIC_RESULTS))
                        turn = api.turn(token)
                    }
                    if (turn.victory.ballot?.canVote == true) {
                        secondBallots++
                        api.execute(token, command(turn, if (id == "Greece") TurnCommandKind.ABSTAIN_DIPLOMATIC_VOTE else TurnCommandKind.CAST_DIPLOMATIC_VOTE, selection = "Greece".takeUnless { id == "Greece" }))
                    }
                    finishTurn(api, token)
                }
                check(secondBallots == 3) { "Did not complete the second election: $secondBallots" }
                check(runtime.game.victoryData!!.victoryType == "Diplomatic")
                for (id in players) {
                    val final = PlayerHost(runtime).bindPlayer(id).result()
                    check(final.result!!.outcome == if (id == "Greece") "Victory" else "Defeat")
                    check(final.victory.votingResults!!.winner == "Greece")
                    check(!final.victory.votingResults.canAcknowledge)
                }
            }
        }
    }

    private fun cityStateVoteTiming(root: Path) = create(root.resolve("initial"), cityStates = 2).use { runtime ->
        val game = runtime.game
        val rome = game.getCivilization("Rome")
        val greece = game.getCivilization("Greece")
        val (known, unknown) = game.civilizations.filter { it.isCityState }
        runtime.executor.accessGame {
            game.turns = 224
            arrangeVote(runtime)
            for (civ in listOf(known, unknown)) {
                if (civ.cities.isEmpty()) checkNotNull(UnitSettlement.tryFoundCity(civ.units.getCivUnits().single { it.name == "Settler" }))
            }
            rome.diplomacyFunctions.makeCivilizationsMeet(known)
            greece.diplomacyFunctions.makeCivilizationsMeet(known)
            check(!rome.knows(unknown))
            known.getDiplomacyManager(greece)!!.setInfluence(70f)
            known.getDiplomacyManager(rome)!!.setInfluence(40f)
            rome.addGold(1_000)
            for (civ in game.civilizations.filter { it.isMajorCiv() || it.isCityState }) {
                civ.removeFlag(CivFlags.ShowDiplomaticVotingResults.name)
                civ.addFlag(CivFlags.TurnsTillNextDiplomaticVote.name, 2)
                civ.popupAlerts.clear()
            }
        }
        GameplayTestApi(runtime, root.resolve("tls"), tokens).use { api ->
            val token = tokens.getValue("Rome")
            val before = api.turn(token).victory.ballot!!.cityStateVoteTiming!!
            check(before.lastInfluenceTurn == 225 && before.window == CityStateVoteWindow.BEFORE_LAST_TURN)
            runtime.executor.accessGame { unknown.addFlag(CivFlags.TurnsTillNextDiplomaticVote.name, 1) }
            check(api.turn(token).victory.ballot!!.cityStateVoteTiming == before) { "An unmet city-state changed the cue" }
            runtime.executor.accessGame { unknown.addFlag(CivFlags.TurnsTillNextDiplomaticVote.name, 2) }

            for (id in players) finishTurn(api, tokens.getValue(id))
            val last = api.turn(token).victory.ballot!!
            check(last.turnsUntilNextVote == 1 && !last.canVote)
            check(last.cityStateVoteTiming!!.let { it.lastInfluenceTurn == 225 && it.window == CityStateVoteWindow.LAST_TURN })
            finishTurn(api, token)
            val waiting = api.turn(token).victory.ballot!!.cityStateVoteTiming!!
            check(waiting.lastInfluenceTurn == 225 && waiting.window == CityStateVoteWindow.CLOSED)
            for (id in players.drop(1)) {
                val other = tokens.getValue(id)
                val turn = api.turn(other)
                check(turn.victory.ballot!!.canVote)
                api.execute(other, command(turn, TurnCommandKind.ABSTAIN_DIPLOMATIC_VOTE))
                finishTurn(api, other)
            }

            val turn = api.turn(token)
            check(turn.turn == 226 && turn.victory.ballot!!.canVote && turn.victory.votingResults == null)
            check(turn.victory.ballot.cityStateVoteTiming!!.window == CityStateVoteWindow.CLOSED)
            check(game.diplomaticVictoryVotesCast[known.civID] == "Greece")
            val gift = command(turn, TurnCommandKind.CITY_STATE_ACTION, known.civID, "giftGold1000").copy(confirmAction = true)
            check(api.execute(token, gift).cityState!!.allyAfter!!.civilizationId == "Rome")
            check(game.diplomaticVictoryVotesCast[known.civID] == "Greece") { "A late gift changed a cast vote" }
        }
    }

    private fun recovery(root: Path) {
        for (kind in listOf(TurnCommandKind.ADD_SPACESHIP_PART, TurnCommandKind.CAST_DIPLOMATIC_VOTE, TurnCommandKind.ABSTAIN_DIPLOMATIC_VOTE, TurnCommandKind.ACKNOWLEDGE_DIPLOMATIC_RESULTS)) {
            for (afterReplace in listOf(false, true)) {
                val directory = root.resolve("$kind-$afterReplace")
                val checkpoint = directory.resolve("checkpoint.json")
                lateinit var request: TurnCommandRequest
                var failCommit = false
                create(directory.resolve("initial")).use { runtime ->
                    if (kind == TurnCommandKind.ADD_SPACESHIP_PART) arrangeScience(runtime) else arrangeVote(runtime, results = kind == TurnCommandKind.ACKNOWLEDGE_DIPLOMATIC_RESULTS)
                    runtime.executor.enableCheckpoints(CheckpointStore(checkpoint, beforeReplace = { if (failCommit && !afterReplace) error("Interrupted write") }, afterReplace = { if (failCommit && afterReplace) error("Lost reply") }))
                    val session = PlayerHost(runtime).bindPlayer("Rome")
                    val turn = session.turn()
                    request = command(turn, kind, turn.map.units.singleOrNull { it.spaceshipAssembly != null }?.handle, "Greece".takeIf { kind == TurnCommandKind.CAST_DIPLOMATIC_VOTE })
                    failCommit = true
                    check(runCatching { session.execute(request) }.isFailure)
                    check(runtime.executor.mutationFailure != null)
                    check(runCatching { session.turn() }.isFailure)
                }
                HeadlessRuntime.loadCheckpoint(checkpoint, directory.resolve("restored")).use { runtime ->
                    val session = PlayerHost(runtime).bindPlayer("Rome")
                    val retry = if (afterReplace) {
                        request
                    } else {
                        check(runCatching { session.execute(request) }.isFailure) { "Unread recovered decision executed" }
                        val fresh = session.turn()
                        request.copy(requestId = "fresh", revision = fresh.revision)
                    }
                    val receipt = session.execute(retry)
                    check(receipt.victory != null)
                    val saved = Files.readString(checkpoint)
                    check(session.execute(retry) == receipt)
                    check(Files.readString(checkpoint) == saved)
                    val rome = runtime.game.getCivilization("Rome")
                    when (kind) {
                        TurnCommandKind.ADD_SPACESHIP_PART -> check(rome.victoryManager.currentsSpaceshipParts["SS Booster"] == 3 && session.result().result!!.victoryType == "Scientific")
                        TurnCommandKind.ACKNOWLEDGE_DIPLOMATIC_RESULTS -> check(!rome.shouldShowDiplomaticVotingResults())
                        else -> check("Rome" in runtime.game.diplomaticVictoryVotesCast && runtime.game.diplomaticVictoryVotesCast["Rome"] == request.selection)
                    }
                }
            }
        }
    }

    private fun cultural(root: Path) = create(root.resolve("initial")).use { runtime ->
        val rome = runtime.game.getCivilization("Rome")
        for (name in listOf("Tradition", "Liberty", "Honor", "Piety", "Patronage")) {
            val branch = runtime.game.ruleset.policyBranches.getValue(name)
            rome.policies.freePolicies++
            rome.policies.adopt(branch)
            for (policy in branch.policies.filterNot { it.name.endsWith(" Complete") }) {
                rome.policies.freePolicies++
                rome.policies.adopt(policy)
            }
        }
        check(rome.policies.completedBranches.size == 5)
        val city = rome.getCapital()!!
        city.cityConstructions.constructionQueue.clear()
        city.cityConstructions.inProgressConstructions["Utopia Project"] = runtime.game.ruleset.buildings.getValue("Utopia Project").getProductionCost(rome, city) - 1
        GameplayTestApi(runtime, root.resolve("tls"), tokens).use { api ->
            val token = tokens.getValue("Rome")
            val turn = api.turn(token)
            check("Utopia Project" in turn.cities.single().constructionOptions)
            api.execute(token, command(turn, TurnCommandKind.QUEUE_CONSTRUCTION, turn.cities.single().handle, "Utopia Project"))
            // One production remains; completion is native at the next Roman turn start.
            for (id in players) finishTurn(api, tokens.getValue(id))
            check(runtime.game.victoryData!!.victoryType == "Cultural")
        }
    }

    private fun time(root: Path) = create(root.resolve("initial")).use { runtime ->
        val game = runtime.game
        game.gameParameters.maxTurns = 1
        game.getCivilization("Rome").getCapital()!!.population.setPopulation(20)
        GameplayTestApi(runtime, root.resolve("tls"), tokens).use { api ->
            for (id in players) finishTurn(api, tokens.getValue(id))
            check(game.victoryData!!.victoryType == "Time")
            check(PlayerHost(runtime).bindPlayer("Rome").result().result!!.outcome == "Victory")
        }
    }

    private fun reject(reason: PlayerSessionFailure, action: () -> Unit) {
        val failure = runCatching(action).exceptionOrNull()
        check(failure is PlayerSessionException && failure.reason == reason) { "Expected $reason, got $failure" }
    }
}
