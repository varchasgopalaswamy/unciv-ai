package uncivai

import com.unciv.json.json
import com.unciv.logic.GameStarter
import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.map.MapParameters
import com.unciv.logic.map.MapSize
import com.unciv.models.metadata.BaseRuleset
import com.unciv.models.metadata.GameParameters
import com.unciv.models.metadata.GameSetupInfo
import com.unciv.models.metadata.Player
import uncivai.player.PlayerHost
import uncivai.player.observeCivilizationProfile
import java.nio.file.Path

object CivilizationProfileProbe {
    fun vanilla(root: Path) = HeadlessRuntime.createVanillaGame(
        root.resolve("game"),
        seed = 412L,
        agentPlayers = listOf("Rome", "Greece", "France", "Egypt"),
    ).use { runtime ->
        val game = runtime.game
        val host = PlayerHost(runtime)
        val rome = host.bindPlayer("Rome")
        val france = host.bindPlayer("France")
        val before = json().toJson(game)
        val initial = rome.turn()
        check(initial.cities.isEmpty()) { "This profile must be available before founding a city" }
        val roman = initial.civilizationProfile
        check(roman.ability.name == "The Glory of Rome")
        check(roman.ability.effects.single().text.let { "25" in it && "Capital" in it })
        check(roman.uniqueBuildings.isEmpty())
        val legion = roman.uniqueUnits.single { it.name == "Legion" }
        check(legion.replaces == "Swordsman" && legion.unitType == "Sword")
        check(legion.baseStats.getValue("strength") == 13f)
        check(legion.comparison!!.baseStats.getValue("strength").ordinary == 11f)
        check(legion.comparison.baseStats.getValue("strength").unique == 13f)
        check(legion.missingTechnologies == listOf("Iron Working") && !legion.obsolete)
        check(legion.resourceRequirements == mapOf("Iron" to 1))
        check(legion.effects.any { "Road" in it } && legion.effects.any { "Fort" in it })

        val greek = host.bindPlayer("Greece").turn().civilizationProfile
        check(greek.ability.effects.size == 3)
        check(greek.ability.effects.any { "friendly territory" in it.text })
        val cavalry = greek.uniqueUnits.single { it.name == "Companion Cavalry" }
        check(cavalry.baseStats.getValue("movement") == 5f && cavalry.resourceRequirements == mapOf("Horses" to 1))
        check(cavalry.freePromotions.single { it.name == "Great Generals I" }.effects.any { "50" in it && "Great General" in it })
        check(cavalry.comparison!!.addedPromotions == listOf("Great Generals I"))

        val french = france.turn().civilizationProfile
        val culture = french.ability.effects.single()
        check("Culture" in culture.text && "Steam Power" in culture.text)
        check(culture.technologyConditions.single().met)
        val musketeer = french.uniqueUnits.single { it.name == "Musketeer" }
        check(musketeer.missingTechnologies == listOf("Gunpowder") && !musketeer.obsolete)
        val foreignLegion = french.uniqueUnits.single { it.name == "Foreign Legion" }
        check(foreignLegion.comparison!!.upgradesTo!!.ordinary == listOf("Infantry"))
        check(foreignLegion.comparison.upgradesTo.unique == listOf("Mechanized Infantry"))

        val egyptian = host.bindPlayer("Egypt").turn().civilizationProfile
        val tomb = egyptian.uniqueBuildings.single { it.name == "Burial Tomb" }
        check(tomb.replaces == "Temple" && tomb.requiredBuilding == "Monument")
        check(tomb.requiredTechnologies == listOf("Philosophy"))
        val differences = tomb.comparison!!.baseStats
        check(differences.getValue("maintenance").ordinary == 2f && differences.getValue("maintenance").unique == 0f)
        check(differences.getValue("yields.Culture").ordinary == 3f && differences.getValue("yields.Culture").unique == 2f)
        check(differences.getValue("yields.Happiness").ordinary == 0f && differences.getValue("yields.Happiness").unique == 2f)
        check(differences.getValue("specialistSlots.Artist").unique == 0f)
        check(tomb.comparison.addedEffects.any { "100" in it && "captured" in it })
        check(tomb.comparison.removedEffects.any { "Destroyed" in it && "captured" in it })
        check(json().toJson(game) == before) { "Profile reads mutated native game state" }

        GameplayTestApi(runtime, root.resolve("tls")).use { api ->
            check(api.turn(api.playerToken("Rome")).civilizationProfile == roman)
        }
        val player = game.getCivilization("France")
        player.tech.addTechnology("Gunpowder", false)
        check(france.turn().civilizationProfile.uniqueUnits.single { it.name == "Musketeer" }.missingTechnologies.isEmpty())
        player.tech.addTechnology("Rifling", false)
        check(france.turn().civilizationProfile.uniqueUnits.single { it.name == "Musketeer" }.obsolete)
        player.tech.addTechnology("Steam Power", false)
        val expired = france.turn().civilizationProfile.ability.effects.single()
        check(!expired.technologyConditions.single().met && expired.text == culture.text)
        check(culture.technologyConditions.single().met && musketeer.missingTechnologies == listOf("Gunpowder")) {
            "An earlier snapshot changed when the live civilization researched technologies"
        }
        check(rome.turn().civilizationProfile == roman) { "Foreign research changed our profile" }
    }

    fun godsAndKings(root: Path) = HeadlessRuntime.create(root.resolve("game")) {
        val parameters = GameParameters().apply {
            baseRuleset = BaseRuleset.Civ_V_GnK.fullName
            numberOfCityStates = 0
            shufflePlayerOrder = false
            players = ArrayList(listOf("Rome", "The Maya", "Arabia", "Egypt", "The Netherlands").map { Player(it, PlayerType.Human) })
        }
        val map = MapParameters().apply {
            baseRuleset = parameters.baseRuleset
            mapSize = MapSize.Small
            seed = 412L
        }
        GameStarter.startNewGame(GameSetupInfo(parameters, map))
    }.use { runtime ->
        val game = runtime.game
        val before = json().toJson(game)
        val roman = observeCivilizationProfile(game.getCivilization("Rome"))
        val legion = roman.uniqueUnits.single { it.name == "Legion" }
        check(legion.baseStats.getValue("strength") == 17f) { "Profile used Vanilla stats for Gods & Kings" }
        check(legion.comparison!!.obsoletedBy!!.ordinary == listOf("Steel"))
        check(legion.comparison.obsoletedBy.unique == listOf("Gunpowder"))
        val atlatlist = observeCivilizationProfile(game.getCivilization("The Maya")).uniqueUnits.single { it.name == "Atlatlist" }
        check(atlatlist.comparison!!.requiredTechnologies!!.ordinary == listOf("Archery"))
        check(atlatlist.comparison.requiredTechnologies.unique == listOf("Agriculture"))
        check(atlatlist.missingTechnologies.isEmpty())
        val camel = observeCivilizationProfile(game.getCivilization("Arabia")).uniqueUnits.single { it.name == "Camel Archer" }
        check(camel.unitType == "Archery" && camel.comparison!!.unitType!!.ordinary == "Mounted")
        val tomb = observeCivilizationProfile(game.getCivilization("Egypt")).uniqueBuildings.single { it.name == "Burial Tomb" }
        check(tomb.baseStats.getValue("yields.Faith") == 2f && "yields.Culture" !in tomb.baseStats)
        val dutch = game.getCivilization("The Netherlands")
        val polder = observeCivilizationProfile(dutch).uniqueImprovements.single { it.name == "Polder" }
        check(polder.requiredTechnology == "Guilds" && !polder.unlocked)
        check(polder.yields["Food"] == 3f && "Marsh" in polder.terrains)
        check(polder.effects.any { "Economics" in it })
        check(json().toJson(game) == before) { "Profile reads mutated native game state" }
        dutch.tech.addTechnology("Guilds")
        check(observeCivilizationProfile(dutch).uniqueImprovements.single().unlocked)
        check(!polder.unlocked) { "Research changed an earlier profile" }
    }
}
