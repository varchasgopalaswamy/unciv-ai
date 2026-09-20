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
object NavalDesktopProbe {
    private lateinit var output: Path
    private val game get() = UncivGame.Current
    private val state get() = checkNotNull(game.gameInfo)
    private val world get() = checkNotNull(game.worldScreen)
    private val rome get() = state.getCivilization("Rome")

    @JvmStatic fun main(args: Array<String>) {
        output = Path.of(args.single())
        require(output.isAbsolute && !Files.exists(output))
        Files.createDirectories(output)
        NavalFixtures.create(output.resolve("fixture-data")).use { runtime ->
            Files.writeString(output.resolve("Rome.json"), UncivFiles.gameInfoToString(runtime.game, forceZip = false))
            runtime.game.getCivilization("Rome").getDiplomacyManager("Greece")!!.declareWar()
            Files.writeString(output.resolve("War.json"), UncivFiles.gameInfoToString(runtime.game, forceZip = false))
        }
        Thread({
            try {
                waitFor { UncivGame.isCurrentInitialized() && game.getScreen() is PlayerReadyScreen }
                ready()
                exercise()
                Files.writeString(output.resolve("COMPLETE"), "Rendered naval actions and movement previews passed.\n")
                onGl { Gdx.app.exit() }
            } catch (failure: Throwable) {
                failure.printStackTrace()
                Files.writeString(output.resolve("FAILURE.txt"), failure.stackTraceToString())
                exitProcess(1)
            }
        }, "naval desktop acceptance").apply {
            isDaemon = true
            start()
        }
        DesktopSaveUiLauncher.launch(arrayOf(output.resolve("Rome.json").toString(), output.resolve("desktop-data").toString()), DesktopSaveUiLauncher.Mode.INSPECTION)
    }

    private fun select(label: String): MapUnit = onGl {
        val unit = rome.units.getCivUnits().single { it.instanceName == label }
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

    private fun exercise() {
        val boat = select("Resource crew")
        capture("01-work-boat-offer")
        clickPrefix("Create Fishing Boats")
        check(onGl { boat.isDestroyed && state.tileMap[-7, 0].improvement == "Fishing Boats" })
        onGl {
            world.mapHolder.setCenterPosition(HexCoord(-7, 0), selectUnit = false)
            world.mapHolder.onTileClicked(world.selectedGameView.tileMapView.getTile(state.tileMap[-7, 0]))
        }
        capture("02-resource-improved")

        val old = select("Upgrade ship")
        capture("03-ship-upgrade-offer")
        val gold = onGl { rome.gold }
        clickPrefix("Upgrade to Caravel")
        // Upgrade controls may show the multi-unit upgrade popup.
        if (!onGl { old.isDestroyed }) {
            capture("04-upgrade-confirmation")
            clickPrefix("Upgrade")
        }
        check(onGl { old.isDestroyed && rome.gold < gold && rome.units.getCivUnits().any { it.id == old.id && it.name == "Caravel" } })
        select("Upgrade ship")
        capture("05-ship-upgraded")

        val voyager = select("Voyager")
        onGl {
            world.mapHolder.setCenterPosition(NavalFixtures.routeDestination)
            world.mapHolder.onTileClicked(world.selectedGameView.tileMapView.getTile(state.tileMap[NavalFixtures.routeDestination]))
        }
        capture("06-distant-route-preview")
        onGl {
            val screen = game.getScreen() as BaseScreen
            val label = actors().filterIsInstance<Label>().single { it.text.toString() == "2" }
            val point = label.localToStageCoordinates(Vector2(label.width / 2, label.height / 2))
            screen.stage.stageToScreenCoordinates(point)
            screen.stage.touchDown(point.x.toInt(), point.y.toInt(), 0, 0)
            screen.stage.touchUp(point.x.toInt(), point.y.toInt(), 0, 0)
        }
        waitFor { onGl { voyager.isMoving() && voyager.currentTile.position != HexCoord(-7, -2) } }
        select("Voyager")
        capture("07-persistent-destination")
        clickPrefix("Stop movement")
        check(onGl { !voyager.isMoving() })
        capture("08-destination-cancelled")

        load("War")
        val frigate = select("Coastal battery")
        onGl { world.mapHolder.onTileClicked(world.selectedGameView.tileMapView.getTile(state.tileMap[6, -1])) }
        capture("09-naval-combat-preview")
        click("Attack")
        waitFor { onGl { frigate.attacksThisTurn == 1 } }
        capture("10-naval-combat-result")
        load("War")
        val bombard = select("Coastal battery")
        val city = onGl { state.getCivilization("Greece").getCapital()!! }
        val health = onGl { city.health }
        onGl { world.mapHolder.onTileClicked(world.selectedGameView.tileMapView.getTile(city.getCenterTile())) }
        capture("11-coastal-bombardment-preview")
        click("Attack")
        waitFor { onGl { bombard.attacksThisTurn == 1 && city.health < health } }
        capture("12-coastal-bombardment-result")
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
