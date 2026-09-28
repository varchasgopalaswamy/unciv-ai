package uncivai

import com.unciv.json.json
import com.unciv.logic.map.HexCoord
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.PolicyDetails
import uncivai.player.ResearchDetails
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

@Serializable
private data class GreatPersonAction(val civilization: String, val request: TurnCommandRequest, val receipt: TurnCommandReceipt)

@Serializable
private data class GreatPeopleRecovery(val actions: List<GreatPersonAction>, val before: String, val after: String, val activationId: String)

/** Gameplay uses HTTPS; assertions and recovery comparisons inspect authoritative engine state. */
object GreatPeopleRunProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val stage = args[1]
        val checkpoint = root.resolve(if (stage == "observe") "observe.json" else "checkpoint.json")
        val fresh = stage in setOf("observe", "start")
        val runtime = if (fresh) HeadlessRuntime.createGreatPeopleExercise(root.resolve(stage)) else HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve(stage))
        runtime.use {
            if (fresh) runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("$stage-tls")).use { api ->
                when (stage) {
                    "observe" -> observe(runtime, api)

                    "start" -> {
                        api.acquire()
                        val activation = api.activate()
                        val before = facts(runtime)
                        captureTestTurn(runtime, root)
                        val actions = actions(runtime, api, checkNotNull(activation.playerToken), "Rome")
                        save(root, GreatPeopleRecovery(actions, before, facts(runtime), checkNotNull(activation.activationId)))
                        api.operator(OperatorRequest("release", api.lease))
                    }

                    "restore" -> restore(runtime, api, root)

                    "verify" -> verify(runtime, api, root)

                    else -> error(stage)
                }
            }
        }
    }

    private fun observe(runtime: HeadlessRuntime, api: GameplayTestApi) {
        val player = runtime.game.getCivilization("Rome")
        val other = runtime.game.getCivilization("Greece")
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val token = api.playerToken("Rome")
        val before = json().toJson(runtime.game)
        val turn = api.turn(token)
        check(turn.scenario == null)
        check(turn.cities.size == 1 && turn.map.units.size == 7)
        check(turn.map.units.all { it.greatPerson != null })
        val research = research(api, token, "Currency")
        val construction = research.technologies.single()
        check(construction.description!!.isNotBlank())
        check(construction.prerequisites.containsAll(listOf("Mathematics")))
        check(!construction.researchNow.available && construction.researchNow.reasons.any { it.contains("Mathematics") })
        check(construction.prerequisiteQueue == listOf("Mathematics", "Currency") && construction.queueAvailable == true)
        val policies = policies(api, token, "Liberty", "Meritocracy", "Liberty Complete", "Rationalism")
        check(policies.freePolicies == 1)
        check(policies.policies.single { it.name == "Liberty" }.branchCompletionDescription!!.contains("Great Person"))
        check(policies.policies.single { it.name == "Meritocracy" }.adoption.available)
        check(!policies.policies.single { it.name == "Liberty Complete" }.adoption.available)
        check(policies.policies.single { it.name == "Rationalism" }.adoption.reasons.any { it.contains("era") })
        check(research(api, token).technologies.all { it.description == null && it.prerequisiteQueue == null })
        check(policies(api, token).policies.all { it.description == null })
        repeat(2) {
            check(session.turn() == turn)
            check(session.researchDetails(listOf("Currency")) == research)
        }
        check(json().toJson(runtime.game) == before) { "Observation mutated native game state" }
        other.greatPeople.greatPersonPointsCounter["Great Scientist"] = 911
        other.policies.storedCulture += 900
        other.tech.scienceOfLast8Turns.fill(999)
        check(session.turn() == turn) { "Hidden rival counters changed observations or revisions" }
        check(session.researchDetails(listOf("Currency")) == research)
        check(session.policyDetails(listOf("Liberty", "Meritocracy", "Liberty Complete", "Rationalism")) == policies)
        for (query in listOf<() -> Any>({ session.researchDetails(listOf("invented")) }, { session.policyDetails(listOf("invented")) })) {
            try {
                query()
                error("Unknown choice accepted")
            } catch (e: PlayerSessionException) {
                check(e.reason == PlayerSessionFailure.OPTION_UNAVAILABLE)
            }
        }
        try {
            session.policyDetails(List(17) { "Liberty" })
            error("Unbounded query accepted")
        } catch (e: PlayerSessionException) {
            check(e.reason == PlayerSessionFailure.INVALID_ARGUMENT)
        }
        val greek = PlayerHost(runtime).bindPlayer("Greece")
        check(greek.turn().map.units.flatMap { it.greatPerson!!.abilities }.none { it.available })
        check(greek.policyDetails(listOf("Meritocracy")).policies.none { it.adoption.available })
        check(greek.researchDetails(listOf("Mathematics")).technologies.none { it.researchNow.available })
        val foreign = other.units.getCivUnits().first().id.toString()
        val rejected = command(turn, TurnCommandKind.ACTIVATE_GREAT_PERSON, foreign, "research1")
        api.reject(token, rejected, "UNIT_UNAVAILABLE")
        api.reject(api.playerToken("Greece"), rejected, "NOT_YOUR_TURN")
        check(player.units.getCivUnitsSize() == 7)
        try {
            (session.researchDetails(listOf("Currency")).technologies as MutableList).clear()
            error("Mutable details")
        } catch (_: UnsupportedOperationException) { }
    }

    private fun actions(runtime: HeadlessRuntime, api: GameplayTestApi, token: String, civ: String): List<GreatPersonAction> {
        val player = runtime.game.getCivilization(civ)
        val result = ArrayList<GreatPersonAction>()
        fun execute(request: TurnCommandRequest): TurnCommandReceipt {
            val receipt = api.execute(token, request)
            val state = facts(runtime)
            check(api.execute(token, request) == receipt)
            check(facts(runtime) == state) { "Retry repeated a Great Person effect" }
            api.reject(token, request.copy(selection = "different"), "REQUEST_ID_CONFLICT")
            result.add(GreatPersonAction(civ, request, receipt))
            return receipt
        }
        fun use(label: String, kind: String): TurnCommandReceipt {
            val turn = api.readAlerts(token)
            val unit = turn.map.units.single { it.displayName.contains(label) }
            val option = unit.greatPerson!!.abilities.single { it.kind == kind }
            check(option.available) { "$label: ${option.reasons}" }
            val native = player.units.getCivUnits().single { it.instanceName == label }
            val request = command(turn, TurnCommandKind.ACTIVATE_GREAT_PERSON, unit.handle, option.name)
            api.reject(token, request.copy(destination = unit.position), "INVALID_ARGUMENT")
            val receipt = execute(request)
            check(receipt.greatPerson!!.consumed && native.isDestroyed)
            if (option.improvement != null) check(native.currentTile.improvement == option.improvement)
            return receipt
        }
        var turn = api.readAlerts(token)
        val details = research(api, token, "Currency").technologies.single()
        api.reject(token, command(turn, TurnCommandKind.SET_RESEARCH_QUEUE).copy(queue = listOf("Currency")), "OPTION_UNAVAILABLE")
        val queue = execute(command(turn, TurnCommandKind.SET_RESEARCH_QUEUE).copy(queue = details.prerequisiteQueue!!))
        val scientist = turn.map.units.single { it.name == "Great Scientist" }
        val detailsAfter = research(api, token, "Mathematics")
        check(detailsAfter.revision == queue.revision)
        api.reject(token, command(turn, TurnCommandKind.ACTIVATE_GREAT_PERSON, scientist.handle, "research1").copy(revision = detailsAfter.revision), "OBSERVATION_REQUIRED")
        val researchResult = use("Research scientist", "HurryResearch")
        check(player.tech.isResearched("Mathematics"))
        check(researchResult.greatPerson!!.research.any { it.name == "Mathematics" && it.completed })
        turn = api.turn(token)
        val discovery = turn.alerts.single { it.kind == "TechResearched" && it.subject == "Mathematics" }
        check(discovery.content!!.paragraphs.any { it.isNotBlank() })
        val engineer = use("Rush engineer", "HurryBuilding")
        check(engineer.greatPerson!!.production.single().construction == "National College")
        val city = player.cities.single()
        check(city.cityConstructions.getRemainingWork("National College") == 1)
        check(!city.cityConstructions.isBuilt("National College"))
        use("Manufactory engineer", "CreateImprovement")
        use("Customs merchant", "CreateImprovement")
        use("Landmark artist", "CreateImprovement")
        val citadel = use("Citadel general", "CreateImprovement")
        check(citadel.greatPerson!!.gainedTiles.isNotEmpty())
        val golden = use("Golden Age artist", "TriggerUnique")
        check(golden.greatPerson!!.goldenAgeTurnsAfter > golden.greatPerson.goldenAgeTurnsBefore)
        check(player.popupAlerts.any { it.type.name == "GoldenAge" })
        turn = api.readAlerts(token)
        val meritocracy = policies(api, token, "Meritocracy", "Liberty Complete")
        check(meritocracy.policies.single { it.name == "Meritocracy" }.adoption.available)
        execute(command(turn, TurnCommandKind.ADOPT_POLICY, selection = "Meritocracy"))
        check(player.policies.isAdopted("Liberty Complete"))
        check(player.greatPeople.freeGreatPeople == 1)
        turn = api.readAlerts(token)
        check(turn.freeGreatPeople == 1 && "Great General" in turn.greatPersonOptions)
        execute(command(turn, TurnCommandKind.CHOOSE_FREE_GREAT_PERSON, selection = "Great General"))
        check(player.greatPeople.freeGreatPeople == 0)
        turn = api.turn(token)
        val general = turn.map.units.single { it.name == "Great General" }
        val ability = general.greatPerson!!.abilities.single { it.kind == "TriggerUnique" }
        val duration = player.goldenAges.turnsLeftForCurrentGoldenAge
        execute(command(turn, TurnCommandKind.ACTIVATE_GREAT_PERSON, general.handle, ability.name))
        check(player.goldenAges.turnsLeftForCurrentGoldenAge == duration + ability.goldenAgeTurns!!)
        check(player.units.getCivUnits().none { it.isGreatPerson() })
        return result
    }

    private fun restore(runtime: HeadlessRuntime, api: GameplayTestApi, root: Path) {
        val saved = expected(root)
        check(facts(runtime) == saved.after) { "Mid-turn effects did not survive checkpoint reload" }
        replay(api, root, saved.actions)
        api.acquire()
        api.restore(root)
        check(facts(runtime) == saved.before) { "Rollback failed to restore people, tiles, research, policies or production" }
        val actions = ArrayList<GreatPersonAction>()
        for (civ in listOf("Rome", "Greece")) {
            val token = checkNotNull(api.activate().playerToken)
            if (civ == "Rome") api.reject(token, saved.actions.first().request, "STALE_EPOCH")
            actions += actions(runtime, api, token, civ)
            api.finishTurn(token)
        }
        check(runtime.game.turns == 1)
        save(root, GreatPeopleRecovery(actions, saved.before, facts(runtime), saved.activationId))
        api.operator(OperatorRequest("release", api.lease))
    }

    private fun verify(runtime: HeadlessRuntime, api: GameplayTestApi, root: Path) {
        val saved = expected(root)
        check(facts(runtime) == saved.after) { "Committed Great Person effects changed across restart" }
        replay(api, root, saved.actions)
        api.acquire()
        for (civ in listOf("Rome", "Greece")) {
            val token = checkNotNull(api.activate().playerToken)
            val player = runtime.game.getCivilization(civ)
            var turn = api.readAlerts(token)
            check(player.cities.single().cityConstructions.isBuilt("National College"))
            check(player.greatPeople.getPointsRequiredForGreatPerson("Great Scientist") > 100)
            check(player.cities.single().population.getNewSpecialists()["Scientist"] == 1)
            var scientist = turn.map.units.single { it.name == "Great Scientist" }
            check(!scientist.displayName.contains("Research scientist"))
            if (scientist.greatPerson!!.abilities.none { it.improvement == "Academy" && it.available }) {
                val move = scientist.moves.filter { it.movementCost.toFloat() < scientist.movement.substringBefore('/').toFloat() }.first { option ->
                    turn.map.tiles.any { it.position == option.destination && it.owner == civ && it.cityName == null && it.improvement == "Farm" }
                }
                api.execute(token, command(turn, TurnCommandKind.MOVE_UNIT, scientist.handle).copy(destination = move.destination))
                turn = api.turn(token)
                scientist = turn.map.units.single { it.name == "Great Scientist" }
            }
            val academy = scientist.greatPerson!!.abilities.single { it.improvement == "Academy" }
            check(academy.available) { academy.reasons.toString() }
            val request = command(turn, TurnCommandKind.ACTIVATE_GREAT_PERSON, scientist.handle, academy.name)
            val receipt = api.execute(token, request)
            check(receipt.greatPerson!!.improvementAfter == "Academy" && receipt.greatPerson.consumed)
            check(api.execute(token, request) == receipt)
            check(runtime.game.tileMap[HexCoord(scientist.position.x, scientist.position.y)].improvement == "Academy")
            api.finishTurn(token)
            val improvements = player.cities.single().getTiles().mapNotNull { it.improvement }.toSet()
            check(improvements.containsAll(listOf("Academy", "Manufactory", "Customs house", "Landmark", "Citadel")))
        }
        repeat(2) { api.finishTurn(checkNotNull(api.activate().playerToken)) }
        check(runtime.game.turns == 3)
        check(runtime.game.civilizations.filter { it.isMajorCiv() }.all { !it.isAtWar() && it.goldenAges.turnsLeftForCurrentGoldenAge > 0 })
        api.operator(OperatorRequest("release", api.lease))
    }

    private fun research(api: GameplayTestApi, token: String, vararg names: String): ResearchDetails = PlayerTurnJson.json.decodeFromString(api.tool(token, "research_details", names(names.toList())).toString())
    private fun policies(api: GameplayTestApi, token: String, vararg names: String): PolicyDetails = PlayerTurnJson.json.decodeFromString(api.tool(token, "policy_details", names(names.toList())).toString())
    private fun names(names: List<String>) = JsonObject(mapOf("names" to JsonArray(names.map(::JsonPrimitive))))
    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection)
    private fun replay(api: GameplayTestApi, root: Path, actions: List<GreatPersonAction>) {
        val checkpoint = gameplayCheckpoint(root.resolve("checkpoint.json"))
        for (action in actions) check(api.execute(api.playerToken(action.civilization), action.request) == action.receipt)
        check(checkpoint == gameplayCheckpoint(root.resolve("checkpoint.json"))) { "Receipt replay rewrote the checkpoint" }
    }
    private fun facts(runtime: HeadlessRuntime): String = runtime.game.civilizations.filter { it.isMajorCiv() }.joinToString("\n") { player ->
        listOf(
            player.civName, player.gold, player.tech.techsResearched.sorted(), player.tech.techsInProgress.toSortedMap(), player.tech.getOverflowScience(),
            player.policies.getAdoptedPolicies().sorted(), player.policies.storedCulture, player.policies.freePolicies,
            player.greatPeople.freeGreatPeople, player.greatPeople.greatPersonPointsCounter.toSortedMap(), player.greatPeople.pointsForNextGreatPersonCounter.toSortedMap(),
            player.goldenAges.turnsLeftForCurrentGoldenAge,
            player.units.getCivUnits().sortedBy { it.id }.map { "${it.id}|${it.name}|${it.currentTile.position}|${it.currentMovement}" }.toList(),
            player.cities.map { city -> "${city.name}|${city.cityConstructions.constructionQueue}|${city.cityConstructions.inProgressConstructions.toSortedMap()}|${city.cityConstructions.getBuiltBuildings().map { it.name }.sorted().toList()}|${city.population.getNewSpecialists().toSortedMap()}" },
            runtime.game.tileMap.values.filter { it.getOwner() === player }.sortedWith(compareBy({ it.position.x }, { it.position.y })).map { "${it.position}|${it.improvement}" },
        ).joinToString(";")
    }
    private fun save(root: Path, value: GreatPeopleRecovery) = Files.writeString(root.resolve("great-people-expected.json"), PlayerTurnJson.json.encodeToString(value))
    private fun expected(root: Path) = PlayerTurnJson.json.decodeFromString<GreatPeopleRecovery>(Files.readString(root.resolve("great-people-expected.json")))
}
