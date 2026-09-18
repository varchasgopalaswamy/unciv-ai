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

/** Operator-only rendered desktop acceptance; isolated copies never connect to a live host. */
object WarTradeDesktopProbe {
    private lateinit var output: Path
    private lateinit var baseline: Path
    private val game get() = UncivGame.Current
    private val world get() = checkNotNull(game.worldScreen)
    private val state get() = checkNotNull(game.gameInfo)

    @JvmStatic
    fun main(args: Array<String>) {
        require(args.size == 1) { "Usage: WarTradeDesktopProbe <new absolute output directory>" }
        output = Path.of(args.single())
        require(output.isAbsolute && !Files.exists(output))
        Files.createDirectories(output)
        baseline = output.resolve("baseline.json")
        HeadlessRuntime.createWarExercise(output.resolve("fixture-data"), "war-joint").use {
            Files.writeString(baseline, UncivFiles.gameInfoToString(it.game, forceZip = false))
        }
        Thread({
            try {
                waitFor { UncivGame.isCurrentInitialized() && game.getScreen() is PlayerReadyScreen }
                ready()
                exercise()
                Files.writeString(output.resolve("COMPLETE"), "Rendered desktop actions and assertions passed. Inspect PNGs for visual acceptance.\n")
                println("WAR-DESKTOP-COMPLETE:$output")
                onGl { Gdx.app.exit() }
            } catch (failure: Throwable) {
                failure.printStackTrace()
                Files.writeString(output.resolve("FAILURE.txt"), failure.stackTraceToString())
                exitProcess(1)
            }
        }, "war desktop acceptance").apply {
            isDaemon = true
            start()
        }
        DesktopSaveUiLauncher.launch(arrayOf(baseline.toString(), output.resolve("desktop-data").toString()), DesktopSaveUiLauncher.Mode.INSPECTION)
    }

    private fun exercise() {
        propose("Rome", "Greece", 60, joint = false, "01-paid-offer")
        acceptAs("Greece", "02-paid-consent")
        assertSettlement(440, 560, romeAtWar = false)
        save("paid-settled")
        viewAs("France")
        capture("03-paid-notice")
        check(labels().any { "declared war" in it.lowercase() })
        check(labels().none { "Rome" in it || "60" in it })
        click("Very well.")
        relationship("Greece", "04-paid-enemy")
        relationship("Rome", "05-paid-neutral-sponsor")

        viewAs("Greece")
        propose("Greece", "Rome", 20, joint = false, "06-joining-offer")
        acceptAs("Rome", "07-joining-consent")
        assertSettlement(460, 540, romeAtWar = true)
        save("joining-settled")
        viewAs("France")
        capture("08-joining-notice")
        check(labels().none { "20 Gold" in it })
        click("Very well.")
        relationship("Rome", "09-joining-enemy")

        loadBaseline("Rome")
        propose("Rome", "Greece", 50, joint = true, "10-joint-offer")
        acceptAs("Greece", "11-joint-consent")
        assertSettlement(450, 550, romeAtWar = true)
        save("joint-settled")
        viewAs("France")
        check(state.getCivilization("France").popupAlerts.count { it.type == AlertType.WarDeclaration } == 2)
        capture("12-joint-first-notice")
        click("Very well.")
        Thread.sleep(500)
        capture("13-joint-second-notice")
        click("Very well.")
        relationship("Rome", "14-joint-rome-enemy")
        relationship("Greece", "15-joint-greece-enemy")

        loadBaseline("Greece")
        onGl {
            val france = state.getCivilization("France")
            france.getDiplomacyManager("Greece")!!.setFlag(DiplomacyFlags.AgreedToNotAttackUs, 20)
            france.getDiplomacyManager("Rome")!!.signDefensivePact(20)
        }
        diplomacy("France")
        click("Declare war")
        capture("16-native-promise-and-pact-warning")
        check(labels().any { "promise" in it })
        check(labels().any { "Rome" in it && "war" in it })
        click("Cancel")
        check(!state.getCivilization("Greece").isAtWarWith(state.getCivilization("France")))
        save("warning-cancelled")
    }

    private fun propose(author: String, partner: String, gold: Int, joint: Boolean, name: String) {
        check(state.currentPlayer == author)
        onGl {
            val trade = Trade().apply {
                ourOffers.add(TradeOffer("Gold", TradeOfferType.Gold, gold, -1))
                if (joint) ourOffers.add(TradeOffer("France", TradeOfferType.WarDeclaration, 1, -1))
                theirOffers.add(TradeOffer("France", TradeOfferType.WarDeclaration, 1, -1))
            }
            val view = world.selectedGameView
            game.pushScreen { DiplomacyScreen(view.civView, view.getForeignCivView(state.getCivilization(partner)), trade) }
            // Seed a prepared draft rather than testing every amount-picker gesture. Publishing
            // and acceptance below still use the rendered screen's ordinary button handlers.
            actors().filterIsInstance<TradeTable>().single().enableOfferButton(true)
        }
        capture(name)
        click("Offer trade", startsWith = true)
        check(state.getCivilization(partner).tradeRequests.size == 1)
    }

    private fun acceptAs(civ: String, name: String) {
        viewAs(civ)
        waitFor { labels().contains("Sounds good!") }
        capture(name)
        click("Sounds good!")
        waitFor { labels().contains("Farewell.") }
        click("Farewell.")
    }

    private fun assertSettlement(romeGold: Int, greeceGold: Int, romeAtWar: Boolean) = onGl {
        val rome = state.getCivilization("Rome")
        val greece = state.getCivilization("Greece")
        val france = state.getCivilization("France")
        check(rome.gold == romeGold && greece.gold == greeceGold && france.gold == 500)
        check(rome.isAtWarWith(france) == romeAtWar && greece.isAtWarWith(france))
        check(!rome.isAtWarWith(greece))
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
            val button = actors().filterIsInstance<TextButton>().single {
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
            println("WAR-DESKTOP-CAPTURE:$name")
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
