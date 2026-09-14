package uncivai

import java.nio.file.Path

/** Internal game states for feature regressions, independent of launchable scenarios. */
internal fun HeadlessRuntime.Companion.createDuel(dataDirectory: Path, seed: Long = 412L, closeStarts: Boolean = false, combatScenario: Boolean = false): HeadlessRuntime = create(dataDirectory) { DuelSetup(seed, closeStarts, combatScenario).create() }

internal fun HeadlessRuntime.Companion.createGreatPeopleExercise(dataDirectory: Path, seed: Long = 517L): HeadlessRuntime = create(dataDirectory) { DuelSetup(seed, greatPeopleScenario = true).create() }

internal fun HeadlessRuntime.Companion.createChatExercise(dataDirectory: Path, seed: Long = 412L): HeadlessRuntime = createDuel(dataDirectory, seed, closeStarts = true)

internal fun HeadlessRuntime.Companion.createVerbalBetrayalExercise(dataDirectory: Path, seed: Long = 412L): HeadlessRuntime = createDuel(dataDirectory, seed, closeStarts = true)

internal fun HeadlessRuntime.Companion.createPeaceExercise(dataDirectory: Path, seed: Long = 412L): HeadlessRuntime = create(dataDirectory) { DuelSetup(seed, peaceScenario = true).create() }

internal fun HeadlessRuntime.Companion.createTradeExercise(dataDirectory: Path, seed: Long = 412L): HeadlessRuntime = create(dataDirectory) { DuelSetup(seed, tradeScenario = true).create() }

internal fun HeadlessRuntime.Companion.createBilateralExercise(dataDirectory: Path, seed: Long = 412L): HeadlessRuntime = create(dataDirectory) { DuelSetup(seed, bilateralScenario = true).create() }

internal fun HeadlessRuntime.Companion.createCityManagementExercise(dataDirectory: Path, seed: Long = 412L): HeadlessRuntime {
    val runtime = createDuel(dataDirectory, seed, combatScenario = true)
    try {
        CityManagementScenarioSetup.arrange(runtime.game)
        return runtime
    } catch (failure: Throwable) {
        runtime.close()
        throw failure
    }
}

/** A basic unexplored world needed by the exploration process-boundary tests. */
object ExplorationFixture {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        HeadlessRuntime.createDuel(root.resolve("data"), 10001L).use { it.executor.enableCheckpoints(root.resolve("game.json")) }
    }
}
