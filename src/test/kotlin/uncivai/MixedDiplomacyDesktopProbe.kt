package uncivai

import com.badlogic.gdx.Gdx
import com.badlogic.gdx.files.FileHandle
import com.badlogic.gdx.graphics.Pixmap
import com.badlogic.gdx.graphics.PixmapIO
import com.badlogic.gdx.math.Vector2
import com.badlogic.gdx.scenes.scene2d.Actor
import com.badlogic.gdx.scenes.scene2d.Group
import com.badlogic.gdx.scenes.scene2d.ui.Label
import com.badlogic.gdx.scenes.scene2d.ui.TextButton
import com.unciv.UncivGame
import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.files.UncivFiles
import com.unciv.logic.trade.Trade
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import com.unciv.ui.screens.basescreen.BaseScreen
import com.unciv.ui.screens.diplomacyscreen.DiplomacyScreen
import com.unciv.ui.screens.diplomacyscreen.TradeTable
import com.unciv.ui.screens.worldscreen.PlayerReadyScreen
import com.unciv.ui.screens.worldscreen.WorldScreen
import kotlinx.coroutines.runBlocking
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit
import kotlin.system.exitProcess

/** Rendered native diplomacy controls and delayed native AI trade effects. */
object MixedDiplomacyDesktopProbe {
    private lateinit var output: Path
    private lateinit var baseline: Path
    private val game get() = UncivGame.Current
    private val world get() = checkNotNull(game.worldScreen)
    private val state get() = checkNotNull(game.gameInfo)

    @JvmStatic
    fun main(args: Array<String>) {
        require(args.size == 1) { "Usage: MixedDiplomacyDesktopProbe <new absolute output directory>" }
        output = Path.of(args.single())
        require(output.isAbsolute && !Files.exists(output))
        Files.createDirectories(output)
        baseline = output.resolve("baseline.json")
        HeadlessRuntime.createMixedDiplomacyExercise(output.resolve("fixture-data")).use {
            Files.writeString(baseline, UncivFiles.gameInfoToString(it.game, forceZip = false))
        }
        Thread({
            try {
                waitFor { UncivGame.isCurrentInitialized() && game.getScreen() is PlayerReadyScreen }
                ready()
                exercise()
                Files.writeString(output.resolve("COMPLETE"), "Rendered desktop actions and assertions passed. Inspect PNGs for visual acceptance.\n")
                println("MIXED-DESKTOP-COMPLETE:$output")
                onGl { Gdx.app.exit() }
            } catch (failure: Throwable) {
                failure.printStackTrace()
                Files.writeString(output.resolve("FAILURE.txt"), failure.stackTraceToString())
                exitProcess(1)
            }
        }, "mixed diplomacy desktop acceptance").apply {
            isDaemon = true
            start()
        }
        DesktopSaveUiLauncher.launch(arrayOf(baseline.toString(), output.resolve("desktop-data").toString()), DesktopSaveUiLauncher.Mode.INSPECTION)
    }

    private fun exercise() {
        diplomacy("Greece")
        click("Demands")
        capture("01-native-demands")
        click(com.unciv.logic.civilization.diplomacy.Demand.DoNotSettleNearUs.demandText)
        check(state.getCivilization("Greece").popupAlerts.any { it.type == AlertType.DemandToStopSettlingCitiesNear })
        viewAs("Greece")
        capture("02-demand-response")
        click(com.unciv.logic.civilization.diplomacy.Demand.DoNotSettleNearUs.acceptDemandText)
        check(state.getCivilization("Rome").getDiplomacyManager("Greece")!!.hasFlag(DiplomacyFlags.AgreedToNotSettleNearUs))
        viewAs("Rome")
        capture("03-native-acceptance-notice")
        click("Very well.")
        diplomacy("Greece")
        click("Denounce", startsWith = true)
        capture("04-denouncement-confirmation")
        click("Denounce", startsWith = true)
        check(state.getCivilization("Rome").getDiplomacyManager("Greece")!!.hasFlag(DiplomacyFlags.Denunciation))
        viewAs("Greece")
        capture("05-denounced-decision")
        click("Very well.")
        check(!state.getCivilization("Rome").isAtWarWith(state.getCivilization("Greece")))
        save("native-demand-and-denouncement")

        loadBaseline("Rome")
        onGl {
            val trade = Trade().apply { ourOffers.add(TradeOffer("Gold", TradeOfferType.Gold, 25, -1)) }
            val view = world.selectedGameView
            game.pushScreen { DiplomacyScreen(view.civView, view.getForeignCivView(state.getCivilization("China")), trade) }
            actors().filterIsInstance<TradeTable>().single().enableOfferButton(true)
        }
        capture("06-native-ai-offer")
        click("Offer trade", startsWith = true)
        check(state.getCivilization("China").tradeRequests.single().trade.theirOffers.single().amount == 25)
        check(state.getCivilization("Rome").gold == 500)
        capture("07-awaiting-native-ai-turn")
        onGl { com.unciv.logic.automation.civilization.TradeAutomation.respondToTradeRequests(state.getCivilization("China"), true) }
        check(state.getCivilization("Rome").gold == 475)
        save("native-ai-settlement")
        viewAs("Rome")
        capture("08-native-ai-acceptance")
    }

    private fun relationship(partner: String, name: String) {
        diplomacy(partner)
        capture(name)
        onGl { game.popScreen() }
    }

    private fun diplomacy(partner: String) = onGl {
        val view = world.selectedGameView
        game.pushScreen { DiplomacyScreen(view.civView, view.getForeignCivView(state.getCivilization(partner))) }
    }

    private fun loadBaseline(civ: String) {
        val fresh = game.files.loadGameFromFile(FileHandle(baseline.toFile()))
        loadAs(fresh, civ)
    }

    private fun viewAs(civ: String) {
        val copy = onGl { state.clone().apply { setTransients() } }
        loadAs(copy, civ)
    }

    private fun loadAs(value: GameInfo, civ: String) {
        value.currentPlayer = civ
        value.currentPlayerCiv = value.getCivilization(civ)
        runBlocking { game.loadGame(value, callFromLoadScreen = true) }
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

    private fun click(text: String, startsWith: Boolean = false) {
        onGl {
            val button = actors().filterIsInstance<TextButton>().last {
                if (startsWith) it.text.toString().startsWith(text) else it.text.toString() == text
            }
            check(!button.isDisabled) { "Disabled button: $text" }
            val screen = game.getScreen() as BaseScreen
            val position = button.localToStageCoordinates(Vector2(button.width / 2, button.height / 2))
            screen.stage.stageToScreenCoordinates(position)
            screen.stage.touchDown(position.x.toInt(), position.y.toInt(), 0, 0)
            screen.stage.touchUp(position.x.toInt(), position.y.toInt(), 0, 0)
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
            Files.writeString(output.resolve("$name.txt"), labelsOnGl().joinToString("\n"))
            println("MIXED-DESKTOP-CAPTURE:$name")
        }
    }

    private fun save(name: String) = onGl {
        Files.writeString(output.resolve("$name.json"), UncivFiles.gameInfoToString(state, forceZip = false))
    }

    private fun actors(): List<Actor> {
        fun descendants(actor: Actor): List<Actor> = listOf(actor) + if (actor is Group) actor.children.flatMap(::descendants) else emptyList()
        return descendants((game.getScreen() as BaseScreen).stage.root)
    }

    private fun labelsOnGl() = actors().filterIsInstance<Label>().map { it.text.toString() }
    private fun labels() = onGl { labelsOnGl() }

    private fun waitFor(condition: () -> Boolean) {
        val deadline = System.nanoTime() + TimeUnit.MINUTES.toNanos(5)
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
