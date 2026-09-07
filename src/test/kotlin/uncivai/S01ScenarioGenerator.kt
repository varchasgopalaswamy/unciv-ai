package uncivai

import com.badlogic.gdx.Gdx
import com.badlogic.gdx.files.FileHandle
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

/** Generates the two persisted, playable S01 scenarios used by integration tests and CLI examples. */
object S01ScenarioGenerator {
    private const val ASSETS_PROPERTY = "uncivai.assetsDir"

    @JvmStatic
    fun main(args: Array<String>) {
        require(args.size == 1) { "Usage: S01ScenarioGenerator <absolute-output-directory>" }
        val outputDirectory = Path.of(args.single())
        require(outputDirectory.isAbsolute) { "Output directory must be absolute" }
        val assetsDirectory = Path.of(
            requireNotNull(System.getProperty(ASSETS_PROPERTY)) {
                "Missing -D$ASSETS_PROPERTY"
            },
        )
        require(assetsDirectory.isAbsolute) { "$ASSETS_PROPERTY must be absolute" }

        Files.createDirectories(outputDirectory)
        val dataDirectory = Files.createTempDirectory(outputDirectory, ".generator-data-")
        try {
            Gdx.files = S01Files(assetsDirectory, dataDirectory)
            val uncivGame = UncivGame(isConsoleMode = true)
            UncivGame.Current = uncivGame
            uncivGame.files = UncivFiles(Gdx.files, dataDirectory.toString())
            uncivGame.settings = safeSettings()

            val errors = RulesetCache.loadRulesets(consoleMode = false, noMods = true)
            check(errors.isEmpty()) { errors.joinToString("\n") }

            generate(BaseRuleset.Civ_V_Vanilla, outputDirectory.resolve("vanilla.json"))
            generate(BaseRuleset.Civ_V_GnK, outputDirectory.resolve("gods-and-kings.json"))
        } finally {
            Concurrency.stopThreadPools()
            deleteTreeWithoutFollowingLinks(dataDirectory)
        }
    }

    private fun generate(baseRuleset: BaseRuleset, output: Path) {
        val gameParameters = GameParameters().apply {
            this.baseRuleset = baseRuleset.fullName
            numberOfCityStates = 0
            noBarbarians = true
            shufflePlayerOrder = false
            players.clear()
            players.add(Player("Rome", PlayerType.Human))
            players.add(Player("Greece", PlayerType.Human))
        }
        val mapParameters = MapParameters().apply {
            this.baseRuleset = baseRuleset.fullName
            mapSize = MapSize.Tiny
            seed = if (baseRuleset == BaseRuleset.Civ_V_Vanilla) 10_001L else 10_002L
            noNaturalWonders = true
            noRuins = true
        }

        val game = GameStarter.startNewGame(GameSetupInfo(gameParameters, mapParameters))
        game.turns = 17
        game.currentPlayer = "Rome"
        game.currentTurnStartTime = 1_600_000_000_000L

        for ((index, civName) in listOf("Rome", "Greece").withIndex()) {
            val civ = game.getCivilization(civName)
            val settler = civ.units.getCivUnits().first { it.hasUnique(UniqueType.FoundCity) }
            val city = civ.addCity(settler.getTile().position, settler)
            settler.destroy()
            city.cityConstructions.constructionQueue.clear()
            city.cityConstructions.currentConstructionIsUserSet = false
            val unit = civ.units.getCivUnits().first()
            unit.health = 91 - index
            unit.currentMovement = 0.5f + index
            unit.due = true
        }
        S01ScenarioState.addPendingChoices(game)
        S01ScenarioState.verify(game)

        UncivFiles.saveZipped = false
        UncivGame.Current.files.saveGame(game, FileHandle(output.toFile()))
        check(Files.readString(output).startsWith("{")) { "Scenario must be uncompressed JSON" }
        println("S01-GENERATED:${output.fileName}:${S01ScenarioState.digest(game)}")
    }

    private fun safeSettings() = GameSettings().apply {
        autoAssignCityProduction = false
        automatedUnitsMoveOnTurnStart = false
        automatedUnitsCanUpgrade = false
        automatedUnitsChoosePromotions = false
        citiesAutoBombardAtEndOfTurn = false
        turnsBetweenAutosaves = Int.MAX_VALUE
    }
}
