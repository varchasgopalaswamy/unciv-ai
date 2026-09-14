package uncivai

import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.civilization.diplomacy.DiplomaticModifiers
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.models.metadata.GameSetupInfo

/** Every bilateral trade prerequisite is reachable immediately, without pre-signing agreements. */
internal object BilateralScenarioSetup {
    const val MAP_NAME = "Bilateral diplomacy exercise"

    fun create(setup: GameSetupInfo): GameInfo = TradeScenarioSetup.create(setup).also { game ->
        game.tileMap.mapParameters.name = MAP_NAME
        for (player in game.civilizations.filter { it.isMajorCiv() }) {
            fun research(name: String) {
                if (player.tech.isResearched(name)) return
                game.ruleset.technologies.getValue(name).prerequisites.forEach(::research)
                player.tech.addTechnology(name, false)
            }
            research("Education")
            research("Chivalry")
            player.tech.techsToResearch = arrayListOf("Machinery", "Printing Press")
            val position = if (player.civID == "Rome") HexCoord(-4, -4) else HexCoord(4, 4)
            val settler = checkNotNull(game.tileMap.placeUnitNearTile(position, "Settler", player))
            check(settler.currentTile.position == position)
            val city = checkNotNull(UnitSettlement.tryFoundCity(settler))
            city.name = if (player.civID == "Rome") "Antium" else "Sparta"
            city.population.setPopulation(3)
            for (owned in player.cities) {
                owned.avoidGrowth = true
                owned.cityConstructions.addBuilding("Granary")
                owned.cityConstructions.constructionQueue.clear()
                owned.cityConstructions.addToQueue("Gold")
                owned.getTiles().filterNot { it.isCityCenter() }.filter { it.resource == null }.forEach { it.setImprovementBasic("Trading post") }
                owned.reassignPopulation()
                owned.cityStats.update()
            }
            player.popupAlerts.clear()
            player.notifications.clear()
            player.cache.updateCivResources()
            player.cache.updateViewableTiles()
            player.updateStatsForNextTurn()
            check(player.cities.size == 2 && !city.isCapital())
        }
    }

    fun phase(game: GameInfo): String {
        val rome = game.getCivilization("Rome")
        val greece = game.getCivilization("Greece")
        val relation = rome.getDiplomacyManager(greece)!!
        return when {
            !relation.hasFlag(DiplomacyFlags.DeclarationOfFriendship) -> "FRIENDSHIP"

            !relation.hasModifier(DiplomaticModifiers.SharedEmbassies) -> "EMBASSIES"

            !relation.hasFlag(DiplomacyFlags.ResearchAgreement) || !relation.hasFlag(DiplomacyFlags.DefensivePact) ||
                !relation.hasOpenBorders || !greece.getDiplomacyManager(rome)!!.hasOpenBorders ||
                greece.cities.none { it.name == "Antium" } -> "FINAL_DEAL"

            else -> "ENACTED"
        }
    }
}
