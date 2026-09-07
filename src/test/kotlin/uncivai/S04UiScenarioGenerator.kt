package uncivai

import com.badlogic.gdx.Gdx
import com.badlogic.gdx.files.FileHandle
import com.unciv.Constants
import com.unciv.UncivGame
import com.unciv.logic.GameStarter
import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.files.UncivFiles
import com.unciv.logic.map.MapParameters
import com.unciv.logic.map.MapSize
import com.unciv.models.metadata.BaseRuleset
import com.unciv.models.metadata.GameParameters
import com.unciv.models.metadata.GameSettings
import com.unciv.models.metadata.GameSetupInfo
import com.unciv.models.metadata.Player
import com.unciv.models.ruleset.RulesetCache
import com.unciv.models.ruleset.unique.UniqueType
import com.unciv.utils.Concurrency
import java.nio.file.Files
import java.nio.file.Path

/** Manual UI fixtures use only APIs present before the selection change. */
object S04UiScenarioGenerator {
    private val cases = listOf(
        "ordinary-vanilla",
        "ordinary",
        "maya",
        "blocked-placement",
        "no-city",
        "empty-maya-pool",
    )

    @JvmStatic
    fun main(args: Array<String>) {
        require(args.size == 1) { "Usage: S04UiScenarioGenerator <absolute-output-directory>" }
        val output = Path.of(args.single())
        require(output.isAbsolute)
        val assets = Path.of(requireNotNull(System.getProperty("uncivai.assetsDir")))
        Files.createDirectories(output)
        val data = Files.createTempDirectory(output, ".generator-")
        try {
            Gdx.files = S01Files(assets, data)
            val game = UncivGame(isConsoleMode = true)
            UncivGame.Current = game
            game.files = UncivFiles(Gdx.files, data.toString())
            game.settings = GameSettings().apply {
                autoAssignCityProduction = false
                automatedUnitsMoveOnTurnStart = false
                showTutorials = false
            }
            val errors = RulesetCache.loadRulesets(consoleMode = false, noMods = true)
            check(errors.isEmpty()) { errors.joinToString("\n") }
            for (case in cases) generate(case, output.resolve("$case.json"))

            // Exercise the actual loader and placement rules without changing the persisted fixtures.
            for (case in cases) verify(case, output.resolve("$case.json"), data.resolve(case))
        } finally {
            Concurrency.stopThreadPools()
            deleteTreeWithoutFollowingLinks(data)
        }
    }

    private fun generate(case: String, output: Path) {
        val baseRuleset = if (case == "ordinary-vanilla") BaseRuleset.Civ_V_Vanilla else BaseRuleset.Civ_V_GnK
        val humanName = if (case == "ordinary-vanilla") "Mongolia" else "The Maya"
        val parameters = GameParameters().apply {
            this.baseRuleset = baseRuleset.fullName
            numberOfCityStates = 0
            noBarbarians = true
            shufflePlayerOrder = false
            players.clear()
            players.add(Player(humanName, PlayerType.Human))
            players.add(Player("Greece", PlayerType.AI))
        }
        val map = MapParameters().apply {
            this.baseRuleset = baseRuleset.fullName
            mapSize = MapSize.Tiny
            seed = 40_004L
            noNaturalWonders = true
            noRuins = true
        }
        val game = GameStarter.startNewGame(GameSetupInfo(parameters, map))
        game.turns = 17
        game.currentPlayer = humanName
        game.currentTurnStartTime = 1_600_000_000_000L
        val human = game.getCivilization(humanName)
        game.currentPlayerCiv = human

        for (civ in listOf(human, game.getCivilization("Greece"))) {
            if (civ === human && case == "no-city") continue
            val settler = civ.units.getCivUnits().first { it.hasUnique(UniqueType.FoundCity) }
            civ.addCity(settler.getTile().position, settler)
            settler.destroy()
        }
        for (civ in game.civilizations) {
            civ.policies.freePolicies = 0
            civ.tech.freeTechs = 0
            civ.greatPeople.freeGreatPeople = 0
            civ.greatPeople.mayaLimitedFreeGP = 0
            civ.greatPeople.longCountGPPool.clear()
            civ.notifications.clear()
            civ.popupAlerts.clear()
        }
        human.addGold(500)
        human.greatPeople.freeGreatPeople = if (case == "maya") 2 else 1
        if (case in setOf("maya", "blocked-placement", "empty-maya-pool")) {
            human.greatPeople.mayaLimitedFreeGP = 1
            if (case != "empty-maya-pool") human.greatPeople.longCountGPPool.add("Great Scientist")
            if (case == "maya") human.greatPeople.longCountGPPool.add("Great Engineer")
        }

        if (case == "blocked-placement") {
            val city = human.cities.single()
            city.name = "Blocked Harbor"
            for (unit in human.units.getCivUnits().toList()) unit.destroy()
            for (tile in city.getCenterTile().neighbors) {
                check(!tile.isCityCenter()) { "Fixture isolation would overwrite another city" }
                tile.setTileResource(null)
                tile.improvement = null
                tile.setTerrainFeatures(emptyList())
                tile.baseTerrain = Constants.ocean
                tile.setTerrainTransients()
            }
            val worker = requireNotNull(human.units.addUnit("Worker", city))
            check(worker.getTile() === city.getCenterTile())
            worker.due = true
        }

        UncivFiles.saveZipped = false
        UncivGame.Current.files.saveGame(game, FileHandle(output.toFile()))
    }

    private fun verify(case: String, save: Path, data: Path) {
        HeadlessRuntime.load(save, data).use { runtime ->
            val game = runtime.game
            val human = game.civilizations.single { it.isHuman() }
            check(game.currentPlayerCiv === human)
            check(!human.isDefeated())
            check(human.greatPeople.freeGreatPeople == if (case == "maya") 2 else 1)
            when (case) {
                "blocked-placement" -> {
                    check(human.cities.single().getCenterTile().civilianUnit?.name == "Worker")
                    check(human.units.addUnit("Great Scientist", human.getCapital()) == null) {
                        "Blocked fixture unexpectedly allows a Great Scientist to spawn"
                    }
                    val worker = human.units.getCivUnits().single { it.name == "Worker" }
                    worker.destroy()
                    check(human.units.addUnit("Great Scientist", human.getCapital()) != null) {
                        "Removing the worker did not allow retrying placement"
                    }
                }

                "no-city" -> {
                    check(human.cities.isEmpty())
                    check(human.units.getCivUnits().any { it.hasUnique(UniqueType.FoundCity) })
                    check(human.units.addUnit("Great Scientist") == null)
                }

                "empty-maya-pool" -> {
                    check(human.greatPeople.mayaLimitedFreeGP == 1)
                    check(human.greatPeople.longCountGPPool.isEmpty())
                }

                else -> {
                    val name = if (case == "ordinary-vanilla") "Khan" else "Great Scientist"
                    check(human.greatPeople.getGreatPeople().any { it.name == name })
                    check(human.units.addUnit(name, human.getCapital()) != null)
                }
            }
            println("S04-UI-SAVE:${save.fileName}:${game.gameParameters.baseRuleset}")
        }
    }
}
