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
import com.unciv.logic.files.UncivFiles
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.ui.screens.basescreen.BaseScreen
import com.unciv.ui.screens.worldscreen.PlayerReadyScreen
import com.unciv.ui.screens.worldscreen.WorldScreen
import com.unciv.ui.screens.worldscreen.unit.UnitTable
import kotlinx.coroutines.runBlocking
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit
import kotlin.system.exitProcess

/** Actual rendered controls, compared with native state after each click. */
object AirDesktopProbe {
    private lateinit var output: Path
    private val game get() = UncivGame.Current
    private val state get() = checkNotNull(game.gameInfo)
    private val world get() = checkNotNull(game.worldScreen)
    private val rome get() = state.getCivilization("Rome")

    @JvmStatic fun main(args: Array<String>) {
        output = Path.of(args.single())
        require(output.isAbsolute && !Files.exists(output))
        Files.createDirectories(output)
        AirFixtures.create(output.resolve("fixture-data")).use { runtime ->
            Files.writeString(output.resolve("Rome.json"), UncivFiles.gameInfoToString(runtime.game, forceZip = false))
            runtime.game.getCivilization("Rome").getDiplomacyManager("Greece")!!.declareWar()
            Files.writeString(output.resolve("War.json"), UncivFiles.gameInfoToString(runtime.game, forceZip = false))
            runtime.game.currentPlayer = "France"
            runtime.game.currentPlayerCiv = runtime.game.getCivilization("France")
            Files.writeString(output.resolve("France.json"), UncivFiles.gameInfoToString(runtime.game, forceZip = false))
        }
        Thread({
            try {
                waitFor { UncivGame.isCurrentInitialized() && game.getScreen() is PlayerReadyScreen }
                ready()
                exercise()
                Files.writeString(output.resolve("COMPLETE"), "Rendered aircraft relocation, strikes, sweeps, paradrops and nuclear actions passed.\n")
                onGl { Gdx.app.exit() }
            } catch (failure: Throwable) {
                failure.printStackTrace()
                Files.writeString(output.resolve("FAILURE.txt"), failure.stackTraceToString())
                exitProcess(1)
            }
        }, "air desktop acceptance").apply {
            isDaemon = true
            start()
        }
        DesktopSaveUiLauncher.launch(arrayOf(output.resolve("Rome.json").toString(), output.resolve("desktop-data").toString()), DesktopSaveUiLauncher.Mode.INSPECTION)
    }

    private fun select(label: String): MapUnit = onGl {
        val unit = state.currentPlayerCiv.units.getCivUnits().single { it.instanceName == label }
        val view = world.selectedGameView.getForeignMapUnitView(unit).tryGetMapUnitView()!!
        world.mapHolder.setCenterPosition(unit.currentTile.position)
        actors().filterIsInstance<UnitTable>().single().selectUnit(view)
        world.shouldUpdate = true
        unit
    }

    private fun clickPrefix(prefix: String) {
        val text = onGl { actors().filterIsInstance<Label>().map { it.text.toString().filter { c -> c.code !in 0xe000..0xf8ff } }.last { it.startsWith(prefix) } }
        click(text)
    }

    private fun chooseTile(position: HexCoord) = onGl {
        world.mapHolder.setCenterPosition(position, selectUnit = false)
        world.mapHolder.onTileClicked(world.selectedGameView.tileMapView.getTile(state.tileMap[position]))
    }

    private fun clickMoveBadge() = onGl {
        // Paradrops use a parachute glyph; ordinary moves use a numbered badge.
        val field = world.mapHolder.javaClass.getDeclaredField("unitActionOverlays").apply { isAccessible = true }
        val overlay = (field.get(world.mapHolder) as List<*>).last() as Group
        val button = overlay.children.first()
        val screen = game.getScreen() as BaseScreen
        val point = button.localToStageCoordinates(Vector2(button.width / 2, button.height / 2))
        screen.stage.stageToScreenCoordinates(point)
        screen.stage.touchDown(point.x.toInt(), point.y.toInt(), 0, 0)
        screen.stage.touchUp(point.x.toInt(), point.y.toInt(), 0, 0)
    }

    private fun clickBattle(text: String) {
        onGl {
            val label = actors().filterIsInstance<Label>().single { label ->
                label.text.toString() == text && generateSequence(label as Actor?) { it.parent }
                    .any { it is com.unciv.ui.screens.worldscreen.bottombar.BattleTable }
            }
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

    private fun exercise() {
        val wing = select("Carrier wing")
        chooseTile(AirFixtures.carrierStart)
        capture("01-rebase-preview")
        clickMoveBadge()
        waitFor { onGl { wing.currentTile.position == AirFixtures.carrierStart && wing.isTransported } }
        select("Carrier wing")
        capture("02-aircraft-aboard")
        val carrier = select("Carrier")
        chooseTile(AirFixtures.carrierEnd)
        capture("03-carrier-movement-preview")
        clickMoveBadge()
        waitFor { onGl { carrier.currentTile.position == AirFixtures.carrierEnd && wing.currentTile === carrier.currentTile } }
        select("Carrier wing")
        capture("04-aircraft-transported")

        val old = select("Reserve wing")
        capture("05-aircraft-upgrade")
        val gold = onGl { rome.gold }
        clickPrefix("Upgrade to Jet Fighter")
        if (!onGl { old.isDestroyed }) clickPrefix("Upgrade")
        waitFor { onGl { old.isDestroyed && rome.gold < gold && rome.units.getCivUnits().any { it.id == old.id && it.name == "Jet Fighter" } } }
        select("Reserve wing")
        capture("06-aircraft-upgraded")

        load("War")
        val bomber = select("Strike wing")
        chooseTile(AirFixtures.strikeTarget)
        capture("07-air-strike-preview")
        clickBattle("Attack")
        waitFor { onGl { bomber.attacksThisTurn == 1 && bomber.health < 100 } }
        capture("08-air-strike-interception")
        val fighter = select("Sweep wing")
        clickPrefix("Air Sweep")
        chooseTile(AirFixtures.strikeTarget)
        capture("09-air-sweep-preview")
        clickBattle("Air Sweep")
        waitFor { onGl { fighter.attacksThisTurn == 1 } }
        capture("10-air-sweep-result")
        val missile = select("Guided strike")
        chooseTile(AirFixtures.missileTarget)
        capture("11-guided-missile-preview")
        clickBattle("Attack")
        waitFor { onGl { missile.isDestroyed } }
        capture("12-guided-missile-result")

        load("France")
        val dropper = select("Drop team")
        clickPrefix("Paradrop")
        chooseTile(AirFixtures.dropTarget)
        capture("13-paradrop-preview")
        clickMoveBadge()
        waitFor { onGl { dropper.currentTile.position == AirFixtures.dropTarget } }
        capture("14-paradrop-result")
        val nuke = select("Nuclear trial")
        chooseTile(AirFixtures.nuclearTarget)
        capture("15-nuclear-preview")
        clickBattle("NUKE")
        waitFor { onGl { nuke.isDestroyed && state.getCivilization("France").isAtWarWith(state.getCivilization("Greece")) } }
        capture("16-nuclear-result")
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
