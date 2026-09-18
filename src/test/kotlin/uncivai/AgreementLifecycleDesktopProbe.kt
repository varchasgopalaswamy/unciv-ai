package uncivai

import com.badlogic.gdx.Gdx
import com.badlogic.gdx.files.FileHandle
import com.badlogic.gdx.graphics.Pixmap
import com.badlogic.gdx.graphics.PixmapIO
import com.badlogic.gdx.scenes.scene2d.Actor
import com.badlogic.gdx.scenes.scene2d.Group
import com.badlogic.gdx.scenes.scene2d.ui.Label
import com.badlogic.gdx.scenes.scene2d.ui.TextButton
import com.unciv.Constants
import com.unciv.UncivGame
import com.unciv.logic.civilization.diplomacy.DiplomacyTurnManager.nextTurn
import com.unciv.logic.files.UncivFiles
import com.unciv.logic.trade.TradeLogic
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import com.unciv.ui.screens.basescreen.BaseScreen
import com.unciv.ui.screens.diplomacyscreen.DiplomacyScreen
import com.unciv.ui.screens.overviewscreen.EmpireOverviewCategories
import com.unciv.ui.screens.overviewscreen.EmpireOverviewScreen
import com.unciv.ui.screens.worldscreen.PlayerReadyScreen
import com.unciv.ui.screens.worldscreen.WorldScreen
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerHost
import java.nio.file.Files
import java.nio.file.Path
import java.security.MessageDigest
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit
import kotlin.system.exitProcess

/** Isolated prepared saves, player API comparisons, and actual native desktop rendering. */
object AgreementLifecycleDesktopProbe {
    private lateinit var output: Path
    private val expectedRows = mutableMapOf<Pair<String, String>, List<String>>()
    private val screenshots = mutableListOf<JsonObject>()
    private val game get() = UncivGame.Current
    private val world get() = checkNotNull(game.worldScreen)
    private val state get() = checkNotNull(game.gameInfo)

    @JvmStatic
    fun main(args: Array<String>) {
        require(args.size == 1) { "Usage: AgreementLifecycleDesktopProbe <new absolute output directory>" }
        output = Path.of(args.single())
        require(output.isAbsolute && !Files.exists(output))
        Files.createDirectories(output)
        prepareExpiry()
        prepareResourceLoss()
        Thread({
            try {
                waitFor { UncivGame.isCurrentInitialized() && game.getScreen() is PlayerReadyScreen }
                ready()
                exercise()
                Files.writeString(
                    output.resolve("acceptance.json"),
                    obj(
                        "engineRevision" to System.getProperty("uncivai.engineRevision"),
                        "ruleset" to "Civ V - Vanilla", "screenshots" to screenshots,
                        "apiNativeRowsEqual" to true, "perOwnerCountdown" to true,
                        "nativeExpiry" to true, "resourceCutShortPreservesPeace" to true,
                        "directCancellationAbsent" to true,
                        "preparation" to "Native TradeLogic.acceptTrade with shortened fixture durations; native DiplomacyTurnManager.nextTurn for lifecycle changes. No negotiated publication or full scheduled round is claimed by this graphical fixture.",
                        "visualInspection" to "Pending human or agent inspection of every captured PNG.",
                    ).toString(),
                )
                Files.writeString(output.resolve("COMPLETE"), "Rendered native screens and API/native assertions passed. Inspect all PNGs.\n")
                println("AGREEMENT-DESKTOP-COMPLETE:$output")
                onGl { Gdx.app.exit() }
            } catch (failure: Throwable) {
                failure.printStackTrace()
                Files.writeString(output.resolve("FAILURE.txt"), failure.stackTraceToString())
                exitProcess(1)
            }
        }, "agreement desktop acceptance").apply {
            isDaemon = true
            start()
        }
        DesktopSaveUiLauncher.launch(arrayOf(savePath("expiry-before").toString(), output.resolve("desktop-data").toString()), DesktopSaveUiLauncher.Mode.INSPECTION)
    }

    private fun prepareExpiry() {
        HeadlessRuntime.createTradeExercise(output.resolve("expiry-data")).use { runtime ->
            val rome = runtime.game.getCivilization("Rome")
            val greece = runtime.game.getCivilization("Greece")
            nativeTrade(
                runtime,
                listOf(
                    TradeOffer(Constants.flatGold, TradeOfferType.Gold, 7, -1),
                    TradeOffer(Constants.goldPerTurn, TradeOfferType.Gold_Per_Turn, 2, 3),
                ),
                listOf(
                    TradeOffer("Iron", TradeOfferType.Strategic_Resource, 1, 3),
                    TradeOffer(Constants.openBorders, TradeOfferType.Agreement, 1, 3),
                ),
            )
            runtime.executor.enableCheckpoints(output.resolve("expiry-checkpoint.json"))
            snapshot(runtime, "expiry-before")
            val romanId = activeTrade(runtime, "Rome").text("agreement")
            check(rome.gold == 993 && greece.gold == 1007)
            check(rome.getDiplomacyManager(greece)!!.goldPerTurn() == -2)
            check(rome.getDiplomacyManager(greece)!!.hasOpenBorders)
            rome.getDiplomacyManager(greece)!!.nextTurn()
            snapshot(runtime, "expiry-owner-ticked")
            check(activeTrade(runtime, "Rome").getValue("remainingTurns").jsonPrimitive.int == 2)
            check(activeTrade(runtime, "Greece").getValue("remainingTurns").jsonPrimitive.int == 3)
            repeat(2) { rome.getDiplomacyManager(greece)!!.nextTurn() }
            repeat(3) { greece.getDiplomacyManager(rome)!!.nextTurn() }
            snapshot(runtime, "expiry-ended")
            check(read(runtime, "Rome", romanId).text("status") == "ENDED")
            check(read(runtime, "Rome", romanId).getValue("history").jsonArray.last().jsonObject.text("reason") == "EXPIRED")
            check(rome.getDiplomacyManager(greece)!!.goldPerTurn() == 0)
            check(!rome.getDiplomacyManager(greece)!!.hasOpenBorders)
            check(rome.getDiplomacyManager(greece)!!.trades.isEmpty())
            check(greece.getDiplomacyManager(rome)!!.trades.isEmpty())
            check(rome.gold == 993 && greece.gold == 1007) { "Fixture diplomacy ticks must not repeat immediate payment" }
        }
    }

    private fun prepareResourceLoss() {
        HeadlessRuntime.createTradeExercise(output.resolve("resource-data")).use { runtime ->
            val rome = runtime.game.getCivilization("Rome")
            val greece = runtime.game.getCivilization("Greece")
            val peace = TradeOffer(Constants.peaceTreaty, TradeOfferType.Treaty, 1, 6)
            nativeTrade(runtime, listOf(peace, TradeOffer("Iron", TradeOfferType.Strategic_Resource, 1, 6)), listOf(peace.copy(), TradeOffer(Constants.goldPerTurn, TradeOfferType.Gold_Per_Turn, 3, 6)))
            for (civ in listOf(rome, greece)) {
                civ.notifications.clear()
                civ.popupAlerts.clear()
            }
            runtime.executor.enableCheckpoints(output.resolve("resource-checkpoint.json"))
            snapshot(runtime, "resource-before")
            val previousId = activeTrade(runtime, "Rome").text("agreement")
            val iron = rome.cities.single().getTiles().single { it.resource == "Iron" }
            iron.improvementIsPillaged = true
            rome.cache.updateCivResources()
            rome.getDiplomacyManager(greece)!!.nextTurn()
            snapshot(runtime, "resource-ended")
            val former = read(runtime, "Rome", previousId)
            check(former.text("status") == "ENDED")
            check(former.getValue("history").jsonArray.last().jsonObject.text("reason") == "RESOURCE_UNAVAILABLE")
            for ((civ, partner) in listOf(rome to greece, greece to rome)) {
                val diplomacy = civ.getDiplomacyManager(partner)!!
                // The pinned engine's union() decrements one of the equal distinct treaty
                // clauses; its remnant uses the other clause's native duration of six.
                check(diplomacy.turnsToPeaceTreaty() == 6 && !diplomacy.canDeclareWar())
                check(diplomacy.goldPerTurn() == 0)
                check(diplomacy.trades.single().let { (it.ourOffers + it.theirOffers).all { offer -> offer.name == Constants.peaceTreaty && offer.duration == 6 } })
                check(civ.notifications.any { "cut short" in it.text })
            }
            check(activeTrade(runtime, "Rome").text("agreement") != previousId)
        }
    }

    private fun nativeTrade(runtime: HeadlessRuntime, give: List<TradeOffer>, receive: List<TradeOffer>) {
        TradeLogic(runtime.game.getCivilization("Rome"), runtime.game.getCivilization("Greece")).apply {
            currentTrade.ourOffers.addAll(give.map { it.copy() })
            currentTrade.theirOffers.addAll(receive.map { it.copy() })
            acceptTrade()
        }
    }

    private fun snapshot(runtime: HeadlessRuntime, name: String) {
        runtime.executor.commitCheckpoint()
        val evidence = listOf("Rome", "Greece").associateWith { civ ->
            val listed = call(runtime, civ, "agreement_list")
            val records = listed.getValue("agreements").jsonArray.map { read(runtime, civ, it.jsonObject.text("agreement")) }
            val player = runtime.game.getCivilization(civ)
            val partner = if (civ == "Rome") "Greece" else "Rome"
            val rows = player.getDiplomacyManager(partner)!!.trades
            val active = records.filter { it.text("kind") == "TRADE" && it.text("status") == "ACTIVE" }
            check(rows.size == active.size)
            val texts = mutableListOf<String>()
            for ((trade, record) in rows.zip(active)) {
                check(!record.getValue("cancellation").jsonObject.getValue("available").jsonPrimitive.boolean)
                for ((direction, offers) in listOf("give" to trade.ourOffers, "receive" to trade.theirOffers)) {
                    val items = record.getValue(direction).jsonArray.map { it.jsonObject }
                    check(items.size == offers.size)
                    for ((offer, item) in offers.zip(items)) {
                        check(item.text("type") == offer.type.name && item.text("name") == offer.name)
                        check(item.getValue("amount").jsonPrimitive.int == offer.amount)
                        check(item.getValue("duration").jsonPrimitive.int == offer.duration)
                        check(item.text("displayText") == offer.getOfferText())
                        texts.add(item.text("displayText"))
                    }
                }
            }
            expectedRows[name to civ] = texts
            obj("agreementList" to listed, "agreementReads" to records, "nativeDisplayTexts" to texts, "notifications" to player.notifications.map { it.text })
        }
        Files.writeString(output.resolve("api-$name.json"), obj("players" to JsonObject(evidence)).toString())
        Files.writeString(savePath(name), UncivFiles.gameInfoToString(runtime.game, forceZip = false))
    }

    private fun exercise() {
        trades("expiry-before", "Rome", "01-current-rome-three-turns")
        onGl {
            val view = world.selectedGameView
            game.pushScreen { DiplomacyScreen(view.civView, view.getForeignCivView(state.getCivilization("Greece"))) }
        }
        noCancellationButtons()
        capture("02-diplomacy-no-cancel")
        trades("expiry-owner-ticked", "Rome", "03-current-rome-two-turns")
        trades("expiry-owner-ticked", "Greece", "04-current-greece-three-turns")
        notices("expiry-ended", "Rome", "05-expiration-notices-rome", "has ended")
        notices("expiry-ended", "Greece", "06-expiration-notices-greece", "has ended")
        trades("resource-before", "Rome", "07-resource-peace-original-six-turns")
        trades("resource-ended", "Rome", "08-resource-peace-remnant-six-turns")
        notices("resource-ended", "Rome", "09-resource-cut-short-rome", "cut short")
        notices("resource-ended", "Greece", "10-resource-cut-short-greece", "cut short")
    }

    private fun trades(snapshot: String, civ: String, name: String) {
        load(snapshot, civ)
        overview(EmpireOverviewCategories.Trades)
        val expected = expectedRows.getValue(snapshot to civ)
        check(expected.isNotEmpty())
        check(onGl { labelsOnGl().map(String::trim).containsAll(expected.map(String::trim)) }) { "Native screen does not contain the agreement API row text: $name" }
        noCancellationButtons()
        capture(name)
        onGl { game.popScreen() }
    }

    private fun notices(snapshot: String, civ: String, name: String, expected: String) {
        load(snapshot, civ)
        overview(EmpireOverviewCategories.Notifications)
        check(onGl { labelsOnGl().any { expected in it } }) { "Expected native notice absent: $name" }
        capture(name)
        onGl { game.popScreen() }
    }

    private fun overview(category: EmpireOverviewCategories) = onGl { game.pushScreen { EmpireOverviewScreen(world.selectedGameView.civView, category) } }

    private fun noCancellationButtons() = onGl {
        check(actors().filterIsInstance<TextButton>().none { Regex("cancel|retract|terminate", RegexOption.IGNORE_CASE).containsMatchIn(it.text) })
    }

    private fun load(snapshot: String, civ: String) {
        val value = game.files.loadGameFromFile(FileHandle(savePath(snapshot).toFile()))
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
        Thread.sleep(500)
    }

    private fun capture(name: String) {
        Thread.sleep(500)
        onGl {
            checkNotNull(game.getScreen()).render(0f)
            val pixels = Pixmap.createFromFrameBuffer(0, 0, Gdx.graphics.backBufferWidth, Gdx.graphics.backBufferHeight)
            val path = output.resolve("$name.png")
            try {
                PixmapIO.writePNG(FileHandle(path.toFile()), pixels, -1, true)
            } finally {
                pixels.dispose()
            }
            Files.writeString(output.resolve("$name.txt"), labelsOnGl().joinToString("\n"))
            screenshots.add(obj("file" to path.fileName.toString(), "sha256" to MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(path)).joinToString("") { "%02x".format(it) }))
            println("AGREEMENT-DESKTOP-CAPTURE:$name")
        }
    }

    private fun actors(): List<Actor> {
        fun descendants(actor: Actor): List<Actor> = listOf(actor) + if (actor is Group) actor.children.flatMap(::descendants) else emptyList()
        return descendants((game.getScreen() as BaseScreen).stage.root)
    }

    private fun labelsOnGl() = actors().filterIsInstance<Label>().map { it.text.toString() }
    private fun savePath(name: String) = output.resolve("save-$name.json")
    private fun call(runtime: HeadlessRuntime, civ: String, op: String, vararg fields: Pair<String, Any?>) = PlayerHost(runtime).bindPlayer(civ).communication(obj("op" to op, "epoch" to runtime.epoch, *fields))
    private fun read(runtime: HeadlessRuntime, civ: String, id: String) = call(runtime, civ, "agreement_read", "agreement" to id).getValue("agreement").jsonObject
    private fun activeTrade(runtime: HeadlessRuntime, civ: String) = call(runtime, civ, "agreement_list").getValue("agreements").jsonArray.map { it.jsonObject }.single { it.text("kind") == "TRADE" && it.text("status") == "ACTIVE" }
    private fun JsonObject.text(key: String) = getValue(key).jsonPrimitive.content

    private fun waitFor(condition: () -> Boolean) {
        val deadline = System.nanoTime() + TimeUnit.MINUTES.toNanos(3)
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
