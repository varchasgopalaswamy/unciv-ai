package uncivai

import com.badlogic.gdx.Gdx
import com.unciv.UncivGame
import java.nio.file.Files
import java.nio.file.Path

/** Runs lifecycle assertions in the display-less child JVM that owns an engine runtime. */
object S01RuntimeProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        require(args.size == 2) { "Usage: S01RuntimeProbe <absolute-save> <absolute-data-directory>" }
        val save = Path.of(args[0])
        val dataDirectory = Path.of(args[1])
        require(save.isAbsolute && dataDirectory.isAbsolute) { "Probe paths must be absolute" }
        check(System.getenv("DISPLAY") == null) { "DISPLAY must be unset" }
        check(System.getenv("WAYLAND_DISPLAY") == null) { "WAYLAND_DISPLAY must be unset" }
        check(Gdx.app == null && Gdx.graphics == null && Gdx.gl == null) {
            "A graphics backend existed before load"
        }

        val filesBeforeLoad = Gdx.files
        val saveBefore = Files.readAllBytes(save)
        val runtime = HeadlessRuntime.load(save, dataDirectory)
        val game = runtime.game
        val loadedDigest = S01ScenarioState.digest(game)
        S01ScenarioState.verify(game)
        verifyRuntimeIsolation(dataDirectory)
        check(Gdx.app == null && Gdx.graphics == null && Gdx.gl == null) {
            "Load created a graphics backend"
        }
        check(UncivGame.Current.worldScreen == null) { "Load created a WorldScreen" }

        runtime.close()

        S01ScenarioState.verify(game)
        check(S01ScenarioState.digest(game) == loadedDigest) { "Close changed ordinary game state" }
        check(saveBefore.contentEquals(Files.readAllBytes(save))) { "Load or close rewrote the save" }
        check(Gdx.app == null && Gdx.graphics == null && Gdx.gl == null) {
            "Close left a graphics backend"
        }
        check(UncivGame.Current.worldScreen == null) { "Close left a WorldScreen" }
        check(UncivGame.getGameInfoOrNull() == null) { "Close left the game in global state" }
        check(Gdx.files === filesBeforeLoad) { "Close did not restore the prior GDX file service" }
        println("S01-LOADED:${save.fileName}:$loadedDigest")
    }

    private fun verifyRuntimeIsolation(dataDirectory: Path) {
        val settings = UncivGame.Current.settings
        check(!settings.autoAssignCityProduction) { "Runtime accepted auto production from disk" }
        check(!settings.automatedUnitsMoveOnTurnStart) { "Runtime accepted auto movement from disk" }
        check(!settings.automatedUnitsCanUpgrade) { "Runtime accepted auto upgrades from disk" }
        check(!settings.automatedUnitsChoosePromotions) { "Runtime accepted auto promotions from disk" }
        check(!settings.citiesAutoBombardAtEndOfTurn) { "Runtime accepted auto bombardment from disk" }
        val actualDataDirectory = UncivGame.Current.files.getDataFolder().file().toPath()
            .toAbsolutePath().normalize()
        check(actualDataDirectory == dataDirectory.toAbsolutePath().normalize()) {
            "Runtime data directory is $actualDataDirectory, expected $dataDirectory"
        }
    }
}
