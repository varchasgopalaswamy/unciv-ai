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
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.civilization.diplomacy.DiplomacyTurnManager.nextTurn
import com.unciv.logic.files.UncivFiles
import com.unciv.ui.screens.basescreen.BaseScreen
import com.unciv.ui.screens.diplomacyscreen.DiplomacyScreen
import com.unciv.ui.screens.worldscreen.PlayerReadyScreen
import com.unciv.ui.screens.worldscreen.WorldScreen
import kotlinx.coroutines.runBlocking
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit
import kotlin.system.exitProcess

/** Rendered native city-state gifts, protection, tribute, war, peace and trade missions. */
object CityStateDesktopProbe {
    private lateinit var output: Path
    private val game get() = UncivGame.Current
    private val state get() = checkNotNull(game.gameInfo)
    private val world get() = checkNotNull(game.worldScreen)

    @JvmStatic
    fun main(args: Array<String>) {
        output = Path.of(args.single())
        require(output.isAbsolute && !Files.exists(output))
        Files.createDirectories(output)
        for (name in listOf("Rome", "France")) {
            CityStateFixtures.create(output.resolve("$name-data")).use { runtime ->
                runtime.game.currentPlayer = name
                runtime.game.currentPlayerCiv = runtime.game.getCivilization(name)
                Files.writeString(output.resolve("$name.json"), UncivFiles.gameInfoToString(runtime.game, forceZip = false))
            }
        }
        Thread({
            try {
                waitFor { UncivGame.isCurrentInitialized() && game.getScreen() is PlayerReadyScreen }
                ready()
                exercise()
                Files.writeString(output.resolve("COMPLETE"), "Rendered city-state controls and native effects passed.\n")
                onGl { Gdx.app.exit() }
            } catch (failure: Throwable) {
                failure.printStackTrace()
                Files.writeString(output.resolve("FAILURE.txt"), failure.stackTraceToString())
                exitProcess(1)
            }
        }, "city-state desktop acceptance").apply {
            isDaemon = true
            start()
        }
        DesktopSaveUiLauncher.launch(arrayOf(output.resolve("Rome.json").toString(), output.resolve("desktop-data").toString()), DesktopSaveUiLauncher.Mode.INSPECTION)
    }

    private fun diplomacy(partner: String) = onGl {
        val view = world.selectedGameView
        game.pushScreen { DiplomacyScreen(view.civView, view.getForeignCivView(state.getCivilization(partner))) }
    }

    private fun clickPrefix(prefix: String) {
        val text = onGl {
            actors().filterIsInstance<Label>().map { it.text.toString().filter { c -> c.code !in 0xe000..0xf8ff } }
                .last { it.startsWith(prefix) }
        }
        click(text)
    }

    private fun exercise() {
        diplomacy("Geneva")
        capture("01-relationship-and-quest")
        click("Give a Gift")
        capture("02-gold-gift-options")
        val gold = onGl { state.getCivilization("Rome").gold }
        clickPrefix("Gift 1,000")
        check(onGl { state.getCivilization("Rome").gold == gold - 1000 && state.getCivilization("Geneva").allyCiv?.civID == "Rome" })
        click("Gift Improvement")
        capture("03-improvement-options")
        clickPrefix("Build Mine on Gems")
        check(onGl { state.getCivilization("Rome").gold == gold - 1200 })
        click("Pledge to protect")
        capture("04-protection-confirmation")
        click("Pledge to protect")
        capture("05-protection-cooldown")
        check(onGl { state.getCivilization("Geneva").cityStateFunctions.getProtectorCivs().any { it.civID == "Rome" } })
        onGl {
            game.popScreen()
            val merchant = state.getCivilization("Rome").units.getCivUnits().single { it.name == "Great Merchant" }
            actors().filterIsInstance<com.unciv.ui.screens.worldscreen.unit.UnitTable>().single()
                .selectUnit(world.selectedGameView.getForeignMapUnitView(merchant).tryGetMapUnitView()!!)
            world.shouldUpdate = true
        }
        capture("06-merchant-mission")
        val beforeMission = onGl { state.getCivilization("Rome").gold }
        clickPrefix("Conduct Trade Mission")
        check(onGl { state.getCivilization("Rome").gold > beforeMission && state.getCivilization("Rome").units.getCivUnits().none { it.name == "Great Merchant" } })
        capture("07-mission-completed")

        load("France")
        diplomacy("Sidon")
        click("Demand Tribute")
        capture("08-tribute-options")
        val beforeTribute = onGl { state.getCivilization("France").gold }
        clickPrefix("Take 60 gold")
        check(onGl { state.getCivilization("France").gold == beforeTribute + 60 })
        capture("09-tribute-paid")

        load("Rome")
        diplomacy("Geneva")
        click("Declare war")
        capture("10-war-confirmation")
        click("Declare war")
        check(onGl { state.getCivilization("Rome").isAtWarWith(state.getCivilization("Geneva")) })
        onGl { game.popScreen() }
        diplomacy("Geneva")
        capture("11-peace-cooldown")
        onGl {
            val manager = state.getCivilization("Rome").getDiplomacyManager("Geneva")!!
            repeat(10) { manager.nextTurn() }
            check(!manager.hasFlag(DiplomacyFlags.DeclaredWar))
            game.popScreen()
        }
        diplomacy("Geneva")
        click("Negotiate Peace")
        capture("12-peace-confirmation")
        click("Negotiate Peace")
        check(onGl { !state.getCivilization("Rome").isAtWarWith(state.getCivilization("Geneva")) })
        capture("13-peace-treaty")
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
