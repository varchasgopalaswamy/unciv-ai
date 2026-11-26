package uncivai

import com.unciv.logic.GameInfo
import com.unciv.logic.city.CityFocus
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.map.HexCoord
import uncivai.player.GameplayScenario

/** Fixed, short live acceptance openings; all later changes use ordinary player APIs. */
internal object ManagerScenarioSetup {
    val variants = setOf("economy-manager", "military-manager")
    fun mapName(variant: String) = when (variant) {
        "economy-manager" -> "Economy manager acceptance"
        "military-manager" -> "Military manager acceptance"
        else -> error("Unknown manager exercise")
    }

    fun create(variant: String, seed: Long): GameInfo = WarScenarioMap.create(seed, mapName(variant)).also { game ->
        val france = game.getCivilization("France")
        france.playerType = PlayerType.AI
        game.gameParameters.players.single { it.chosenCiv == "France" }.playerType = PlayerType.AI
        val rome = game.getCivilization("Rome")
        val greece = game.getCivilization("Greece")
        for (player in listOf(rome, greece)) {
            fun research(name: String) {
                if (player.tech.isResearched(name)) return
                game.ruleset.technologies.getValue(name).prerequisites.forEach(::research)
                player.tech.addTechnology(name, false)
            }
            research("Education")
            player.tech.techsToResearch = arrayListOf(game.ruleset.technologies.keys.sorted().first { player.tech.canBeResearched(it) })
            for (city in player.cities) {
                city.population.setPopulation(4)
                city.cityConstructions.addBuilding("Colosseum")
                city.cityConstructions.addBuilding("Library")
                city.cityConstructions.addBuilding("University")
                city.cityConstructions.constructionQueue.clear()
                city.cityConstructions.addToQueue("The Pyramids")
                city.avoidGrowth = true
                city.setCityFocus(CityFocus.FoodFocus)
                city.population.foodStored = city.population.getFoodToNextPopulation() - 1
                city.getTiles().filterNot { it.isCityCenter() }.forEach { it.setImprovementBasic("Farm") }
                city.reassignPopulation()
                city.cityStats.update()
            }
        }
        if (variant == "military-manager") {
            rome.getDiplomacyManager(greece)!!.declareWar()
            // Prepared history: this war has already served its minimum duration.
            // No war flags or gameplay conditions are changed after startup.
            rome.getDiplomacyManager(greece)!!.removeFlag(DiplomacyFlags.DeclaredWar)
            greece.getDiplomacyManager(rome)!!.removeFlag(DiplomacyFlags.DeclaredWar)
            for (player in listOf(rome, greece)) player.units.getCivUnits().toList().forEach { it.destroy() }
            fun unit(civ: Civilization, name: String, x: Int, y: Int, label: String) = checkNotNull(game.tileMap.placeUnitNearTile(HexCoord(x, y), name, civ)).also { it.instanceName = label }
            unit(rome, "Spearman", -3, -2, "Border Guard")
            unit(rome, "Archer", -4, -2, "Border Archer")
            unit(greece, "Spearman", -1, -1, "Opposing Guard")
            unit(greece, "Archer", 0, -1, "Opposing Archer")
            unit(rome, "Worker", -4, -3, "Road Crew")
            unit(rome, "Spearman", -4, -3, "Escort Guard")
        }
        for (player in game.civilizations) {
            player.popupAlerts.clear()
            player.notifications.clear()
            player.cache.updateCivResources()
            player.cache.updateViewableTiles()
            player.updateStatsForNextTurn()
        }
    }

    fun seedHistory(runtime: GameExecutor, variant: String) {
        if (variant != "economy-manager") return
        val offered = runtime.communication.handle(
            "Rome",
            obj(
                "op" to "trade_offer",
                "epoch" to runtime.epoch,
                "requestId" to "prepared-manager-reservation",
                "partner" to "France",
                "give" to listOf(obj("type" to "Gold", "name" to "Gold", "amount" to 480, "duration" to -1)),
                "receive" to emptyList<String>(),
            ),
        )
        check(offered["trade"] != null) { "Could not prepare the treasury reservation" }
        runtime.accessGame { game ->
            val player = game.getCivilization("Rome")
            check(player.committedGold() == 480 && player.availableGold() == 20)
        }
    }

    fun scenario(game: GameInfo, player: Civilization): GameplayScenario {
        val economy = game.tileMap.mapParameters.name == mapName("economy-manager")
        val shared = listOf(
            "Controlled manager acceptance exercise. Follow the instructions for your current role; another role's task is not permission to use its tools. Instructions are objectives, not evidence of success.",
            "Economy: give every owned city a city_plan explaining inherited and chosen construction, growth, citizens/specialists and spending; cite successful actions with intent.cityPlan. Replace the inherited wonder queues with useful development or defense, allow sensible growth and keep citizens fed. Preserve good choices and explain deferrals.",
            "All roles: use native facts and receipts, distinguish assessments from facts, avoid duplicate actions after restart, and finish_role when your work is complete. Do not start additional wars or capture cities in this exercise.",
        )
        val specific = if (economy) {
            if (player.civID == "Rome") {
                listOf(
                    "Prepared background: an outgoing native-AI gift offer to France commits 480 of your 500 Gold. It was seeded by the fixture, not negotiated by you. It does not suspend gameplay.",
                    "Economy on turn zero: inspect Worker purchase cost, availableGold and committedGold. Make exactly one deliberate BUY_CONSTRUCTION Worker attempt in your capital while the offer is pending, after inspecting and accepting its purchase consequences. This is the exercise's expected failed action. Record the actual failure; never claim a purchase succeeded. Send main a role_message describing the rejection, then finish_role. Manage the cities normally while funds are unavailable.",
                    "Main on turn zero: do research/worker strategy, then read war_goals for internal role messages. Wait for economy's rejection message AND economyCoordination.rolesFinished.economy=true in that tool's response before withdrawing the prepared France offer via trade_decide. Inspect its actual terms first. This ordering deliberately tests reopening a finished economy pass. Send an urgent economy_request to buy one Worker in your capital and finish useful city management. Remain active while waiting: do not finish_role until you have withdrawn the offer and sent that urgent request. Let economy do the purchase; you deploy the purchased unit if it has movement. Do not replace the gift offer.",
                    "Main on turn one: use city_override on your frontier city with a concrete reason, queue a defensive unit there, then send economy an urgent request covering that city. Economy must respect the override and report completed/deferred/rejected with a reason. Main keeps the override only for this turn.",
                    "After a service restart: economy must use its stored city plans, revise only when current reasons change, and retain outstanding requests. Main must not repeat the prepared offer or its withdrawal.",
                )
            } else {
                listOf("Main and diplomacy: remain peaceful and let Rome complete its private economic exercise. Economy: develop your own two cities and persist their reasons. No gift or special purchase is required of Greece.")
            }
        } else {
            listOf(
                "The prepared Rome–Greece war is already old enough for native peace. France is a native AI observer. Do not infer hidden positions; use your own visible map.",
                "Main on turn zero: establish initial war_goals against the other agent civilization: protect frontier cities and preserve the army, with completion and cost criteria. Military may defend while the brief is pending. Main for Rome must use escort_assignment for Escort Guard and Road Crew, then move BOTH units safely using separate main-issued moves, one for each unit. Do not pair them in this exercise; this tests independent escort control. Military must leave the escort alone. Do not put the escort into battle.",
                "Military on turn zero: use conventional tactics for your other combat units, maintain a military_operation with current progress, uncertainties and reconsideration conditions, and request economic support if needed. Main retains all civilian and capture decisions.",
                "Military on turn one: propose one substantive war_goals revision against the current version, reducing offensive ambition and prioritizing preservation before peace. Main must respond briefly with its assessment; either finalize agreement, or main resolves disagreement with a reason. No silent amendment. Complete this discussion before offering peace.",
                "Main and diplomacy from turn two: Rome initiates a mutual native Peace Treaty with Greece using trade_offer. Greece reads the exact terms and accepts if only mutual peace. The service must obtain a military overview and main approval before each binding action, including the off-turn acceptance. Do not use a social promise instead. Do not republish an existing proposal or duplicate an enacted treaty.",
                "After native peace: military keeps control of combat units and scouts except main's assigned escorts. Assess peacetime readiness and retain useful standing orders; later turns continue peaceful city/worker management and preserve goals, operation notes, escort duties and the consultation record across restart.",
            )
        }
        return GameplayScenario(
            if (economy) "ECONOMY_MANAGER_ACCEPTANCE" else "MILITARY_MANAGER_ACCEPTANCE",
            "TURN_${game.turns}",
            shared + specific,
        )
    }
}
