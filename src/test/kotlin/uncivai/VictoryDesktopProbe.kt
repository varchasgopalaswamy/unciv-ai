package uncivai

import com.badlogic.gdx.Gdx
import com.badlogic.gdx.files.FileHandle
import com.badlogic.gdx.graphics.Pixmap
import com.badlogic.gdx.graphics.PixmapIO
import com.badlogic.gdx.math.Vector2
import com.badlogic.gdx.scenes.scene2d.Actor
import com.badlogic.gdx.scenes.scene2d.Group
import com.badlogic.gdx.scenes.scene2d.ui.Button
import com.badlogic.gdx.scenes.scene2d.ui.Label
import com.unciv.UncivGame
import com.unciv.logic.civilization.CivFlags
import com.unciv.logic.files.UncivFiles
import com.unciv.ui.screens.basescreen.BaseScreen
import com.unciv.ui.screens.pickerscreens.DiplomaticVoteResultScreen
import com.unciv.ui.screens.victoryscreen.VictoryScreen
import com.unciv.ui.screens.worldscreen.PlayerReadyScreen
import com.unciv.ui.screens.worldscreen.WorldScreen
import kotlinx.coroutines.runBlocking
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit
import kotlin.system.exitProcess

/** Rendered ordinary vote, abstain, result and spaceship controls on isolated Vanilla saves. */
object VictoryDesktopProbe {
    private lateinit var output: Path
    private val game get() = UncivGame.Current
    private val state get() = checkNotNull(game.gameInfo)
    private val world get() = checkNotNull(game.worldScreen)

    @JvmStatic
    fun main(args: Array<String>) {
        output = Path.of(args.single())
        require(output.isAbsolute && !Files.exists(output))
        Files.createDirectories(output)
        for (name in listOf("election", "science")) {
            VictoryDecisionsProbe.create(output.resolve("$name-data")).use { runtime ->
                if (name == "election") VictoryDecisionsProbe.arrangeVote(runtime) else VictoryDecisionsProbe.arrangeScience(runtime)
                Files.writeString(output.resolve("$name.json"), UncivFiles.gameInfoToString(runtime.game, forceZip = false))
            }
        }
        Thread({
            try {
                waitFor { UncivGame.isCurrentInitialized() && game.getScreen() is PlayerReadyScreen }
                ready()
                exercise()
                Files.writeString(output.resolve("COMPLETE"), "Rendered victory controls and native effects passed.\n")
                onGl { Gdx.app.exit() }
            } catch (failure: Throwable) {
                failure.printStackTrace()
                Files.writeString(output.resolve("FAILURE.txt"), failure.stackTraceToString())
                exitProcess(1)
            }
        }, "victory desktop acceptance").apply {
            isDaemon = true
            start()
        }
        DesktopSaveUiLauncher.launch(arrayOf(output.resolve("election.json").toString(), output.resolve("desktop-data").toString()), DesktopSaveUiLauncher.Mode.INSPECTION)
    }

    private fun exercise() {
        capture("01-vote-required")
        click("Vote for World Leader")
        capture("02-candidates")
        click("Greece")
        click("Vote for Greece")
        check(onGl { state.diplomaticVictoryVotesCast["Rome"] == "Greece" })
        capture("03-vote-recorded")

        load("election")
        click("Vote for World Leader")
        click("Abstain")
        capture("04-explicit-abstention")
        click("Abstain")
        check(onGl { "Rome" in state.diplomaticVictoryVotesCast && state.diplomaticVictoryVotesCast["Rome"] == null })
        onGl {
            state.diplomaticVictoryVotesCast["Greece"] = "Rome"
            state.diplomaticVictoryVotesCast["France"] = "Rome"
            state.getCivilization("Rome").addFlag(CivFlags.ShowDiplomaticVotingResults.name, 0)
            world.shouldUpdate = true
        }
        waitFor { game.getScreen() is DiplomaticVoteResultScreen }
        capture("05-results")
        click("Continue")
        check(onGl { !state.getCivilization("Rome").shouldShowDiplomaticVotingResults() && state.diplomaticVictoryVotesCast.size == 3 })
        capture("06-results-acknowledged")

        load("science")
        onGl {
            val part = state.getCivilization("Rome").units.getCivUnits().single { it.name == "SS Booster" }
            check(part.currentMovement == 0f)
            actors().filterIsInstance<com.unciv.ui.screens.worldscreen.unit.UnitTable>().single()
                .selectUnit(world.selectedGameView.getForeignMapUnitView(part).tryGetMapUnitView()!!)
            world.shouldUpdate = true
        }
        capture("07-assemble-with-zero-movement")
        click("Add to The Spaceship")
        waitFor { game.getScreen() is VictoryScreen }
        check(
            onGl {
                state.getCivilization("Rome").victoryManager.currentsSpaceshipParts["SS Booster"] == 3 &&
                    state.getCivilization("Rome").units.getCivUnits().none { it.name == "SS Booster" } &&
                    state.victoryData?.victoryType == "Scientific"
            },
        )
        capture("08-scientific-victory")
        onGl { Files.writeString(output.resolve("won.json"), UncivFiles.gameInfoToString(state, forceZip = false)) }
    }

    private fun load(name: String) {
        val fresh = game.files.loadGameFromFile(FileHandle(output.resolve("$name.json").toFile()))
        runBlocking { game.loadGame(fresh, callFromLoadScreen = true) }
        ready()
    }

    private fun ready() {
        waitFor { game.getScreen() is PlayerReadyScreen }
        onGl {
            val screen = game.getScreen() as BaseScreen
            val x = Gdx.graphics.width / 2
            val y = Gdx.graphics.height / 2
            screen.stage.touchDown(x, y, 0, 0)
            screen.stage.touchUp(x, y, 0, 0)
        }
        waitFor { game.getScreen() is WorldScreen }
        Thread.sleep(750)
    }

    private fun click(text: String) {
        onGl {
            val label = actors().filterIsInstance<Label>().lastOrNull { label -> label.text.toString().filter { it.code !in 0xe000..0xf8ff } == text }
                ?: error("Missing '$text'; labels: ${actors().filterIsInstance<Label>().map { it.text }}")
            val button = generateSequence(label as Actor?) { it.parent }.filterIsInstance<Button>().first()
            check(!button.isDisabled)
            val screen = game.getScreen() as BaseScreen
            val point = button.localToStageCoordinates(Vector2(button.width / 2, button.height / 2))
            screen.stage.stageToScreenCoordinates(point)
            screen.stage.touchDown(point.x.toInt(), point.y.toInt(), 0, 0)
            screen.stage.touchUp(point.x.toInt(), point.y.toInt(), 0, 0)
        }
        Thread.sleep(350)
    }

    private fun capture(name: String) {
        Thread.sleep(500)
        onGl {
            checkNotNull(game.getScreen()).render(0f)
            val pixels = Pixmap.createFromFrameBuffer(0, 0, Gdx.graphics.backBufferWidth, Gdx.graphics.backBufferHeight)
            try {
                PixmapIO.writePNG(FileHandle(output.resolve("$name.png").toFile()), pixels, -1, true)
            } finally {
                pixels.dispose()
            }
            Files.writeString(output.resolve("$name.txt"), actors().filterIsInstance<Label>().joinToString("\n") { it.text })
        }
    }

    private fun actors(): List<Actor> {
        fun descendants(actor: Actor): List<Actor> = listOf(actor) + if (actor is Group) actor.children.flatMap(::descendants) else emptyList()
        return descendants((game.getScreen() as BaseScreen).stage.root)
    }

    private fun waitFor(condition: () -> Boolean) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(45)
        while (!condition()) {
            check(System.nanoTime() < deadline) { "Desktop condition timed out" }
            Thread.sleep(50)
        }
    }

    private fun <T> onGl(action: () -> T): T {
        val result = CompletableFuture<T>()
        Gdx.app.postRunnable {
            try {
                result.complete(action())
            } catch (failure: Throwable) {
                result.completeExceptionally(failure)
            }
        }
        return result.get(30, TimeUnit.SECONDS)
    }
}
