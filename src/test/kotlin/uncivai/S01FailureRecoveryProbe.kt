package uncivai

import com.badlogic.gdx.Gdx
import com.unciv.UncivGame
import java.nio.file.Path

/** Verifies a post-preview load failure restores global state and allows another runtime. */
object S01FailureRecoveryProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        require(args.size == 3) {
            "Usage: S01FailureRecoveryProbe <malformed-save> <valid-save> <data-directory>"
        }
        val malformedSave = Path.of(args[0])
        val validSave = Path.of(args[1])
        val dataDirectory = Path.of(args[2])
        require(malformedSave.isAbsolute && validSave.isAbsolute && dataDirectory.isAbsolute)

        val filesBeforeLoad = Gdx.files
        val failure = runCatching {
            HeadlessRuntime.load(malformedSave, dataDirectory.resolve("failed"))
        }.exceptionOrNull()
        check(failure != null) { "Malformed full game unexpectedly loaded" }
        check(UncivGame.getGameInfoOrNull() == null) { "Failed load left a game in global state" }
        check(Gdx.files === filesBeforeLoad) { "Failed load did not restore the GDX file service" }
        check(Gdx.app == null && Gdx.graphics == null && Gdx.gl == null)

        HeadlessRuntime.load(validSave, dataDirectory.resolve("recovered")).use { runtime ->
            S01ScenarioState.verify(runtime.game)
        }
        check(UncivGame.getGameInfoOrNull() == null) { "Recovered runtime did not close cleanly" }
        check(Gdx.files === filesBeforeLoad) { "Recovered runtime did not restore GDX files" }
        println("S01-RECOVERED")
    }
}
