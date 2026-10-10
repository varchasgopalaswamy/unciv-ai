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
import com.badlogic.gdx.scenes.scene2d.ui.ScrollPane
import com.badlogic.gdx.scenes.scene2d.ui.SelectBox
import com.badlogic.gdx.scenes.scene2d.ui.TextArea
import com.badlogic.gdx.scenes.scene2d.ui.TextField
import com.unciv.UncivGame
import com.unciv.logic.files.UncivFiles
import com.unciv.ui.screens.basescreen.BaseScreen
import com.unciv.ui.screens.diplomacyscreen.DiplomacyScreen
import com.unciv.ui.screens.pickerscreens.TechPickerScreen
import com.unciv.ui.screens.worldscreen.WorldScreen
import com.unciv.ui.screens.worldscreen.unit.UnitTable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.net.ServerSocket
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermissions
import java.time.Duration
import java.util.Base64
import java.util.UUID
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit
import kotlin.system.exitProcess

/** Drives the production desktop and HTTPS host together, with ordinary native controls. */
object ManagedDesktopProbe {
    private lateinit var output: Path
    private lateinit var session: DesktopSession
    private lateinit var client: HttpClient
    private lateinit var base: String
    private val lease = UUID.randomUUID().toString()
    private var tokens = mapOf("France" to "F".repeat(43), "Rome" to "R".repeat(43), "Greece" to "G".repeat(43))
    private val app get() = UncivGame.Current
    private val game get() = checkNotNull(app.gameInfo)
    private val world get() = checkNotNull(app.worldScreen)

    @JvmStatic fun main(args: Array<String>) {
        output = Path.of(args.single())
        require(output.isAbsolute && !Files.exists(output))
        Files.createDirectories(output)
        Files.setPosixFilePermissions(output, PosixFilePermissions.fromString("rwx------"))
        val checkpoint = output.resolve("checkpoint.json")
        HeadlessRuntime.createVanillaGame(output.resolve("fixture"), 953L, 1, listOf("Egypt"), tokens.keys.toList()).use { runtime ->
            runtime.executor.communicationState.humanPlayers.add("France")
            val players = tokens.keys.map(runtime.game::getCivilization)
            for (player in players) {
                player.addGold(1000)
                for (other in players) if (player !== other) player.diplomacyFunctions.makeCivilizationsMeet(other)
                if (player.civID != "France") player.tech.techsToResearch.add("Pottery")
            }
            players.forEach { it.popupAlerts.clear() }
            runtime.executor.enableCheckpoints(checkpoint)
        }
        fun privateFile(name: String, content: String): Path = Files.writeString(output.resolve(name), content).also {
            Files.setPosixFilePermissions(it, PosixFilePermissions.fromString("rw-------"))
        }
        val players = privateFile("players.json", obj("players" to tokens.map { (civ, token) -> obj("civilizationId" to civ, "token" to token, "controller" to if (civ == "France") "HUMAN" else "AGENT") }).toString())
        val operator = privateFile("operator.json", obj("token" to "O".repeat(43)).toString())
        val tlsDirectory = Files.createDirectories(output.resolve("tls"))
        Files.setPosixFilePermissions(tlsDirectory, PosixFilePermissions.fromString("rwx------"))
        val tls = TlsTestFixture(tlsDirectory)
        val ca = output.resolve("ca.pem")
        Files.writeString(ca, "-----BEGIN CERTIFICATE-----\n" + Base64.getMimeEncoder(64, byteArrayOf(10)).encodeToString(tls.tls.keyStore.getCertificate("host").encoded) + "\n-----END CERTIFICATE-----\n")
        client = HttpClient.newBuilder().sslContext(tls.context).connectTimeout(Duration.ofSeconds(5)).build()
        DesktopHost.launch(
            CliOptions(
                saveFile = null, dataDirectory = output.resolve("desktop"), mcpConfig = players, port = 0,
                checkpoint = null, resumeCheckpoint = checkpoint, newGame = false, scenario = null, seed = null,
                operatorConfig = operator, tlsConfig = tls.config, tradeCounteroffers = null, tradeTimeoutSeconds = null, human = "France",
            ),
        ) { ready, port ->
            session = ready
            base = "https://localhost:$port"
            privateFile("endpoints.json", obj("hostUrl" to base, "caFile" to ca.toString()).toString())
            Thread({
                try {
                    if (System.getProperty("uncivai.liveDesktop") == "true") {
                        liveExercise()
                        return@Thread
                    }
                    exercise()
                    Files.writeString(output.resolve("COMPLETE"), "Native input, HTTPS turns, private groups, exact chat history and unread audiences, focus and drafts, queued review messages, human consent and rewind passed.\n")
                    println("MANAGED-DESKTOP-COMPLETE:$output")
                    operator(OperatorRequest("release", lease))
                    onGl { session.exit() }
                } catch (failure: Throwable) {
                    failure.printStackTrace()
                    runCatching { capture("failure") }
                    Files.writeString(output.resolve("FAILURE.txt"), failure.stackTraceToString())
                    exitProcess(1)
                }
            }, "managed desktop acceptance").apply {
                isDaemon = true
                start()
            }
        }
    }

    /** Opt-in paid acceptance using the existing production services and scheduler. */
    private fun liveExercise() {
        val cli = checkNotNull(System.getProperty("uncivai.agentCli"))
        val processes = mutableListOf<Process>()
        fun start(name: String, vararg arguments: String): Process = ProcessBuilder(listOf(cli) + arguments + listOf("--state-dir", output.toString()))
            .redirectErrorStream(true).redirectOutput(output.resolve("$name.log").toFile()).start().also { processes.add(it) }
        try {
            for (civ in listOf("Rome", "Greece")) {
                val port = ServerSocket(0).use { it.localPort }
                val service = start(civ, "agent-server", "start", civ, "--port", port.toString(), "--model", "gpt-6-luna", "--reasoning-effort", "medium", "--fast")
                val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60)
                while (!Files.readString(output.resolve("$civ.log")).contains("AGENT-READY:")) {
                    check(service.isAlive && System.nanoTime() < deadline) { "Agent service did not start: $civ" }
                    Thread.sleep(100)
                }
            }
            val scheduler = start("scheduler", "match", "start", "--max-turns", "1", "--max-invocations", "30", "--max-seconds", "900", "--turn-timeout", "600")
            val deadline = System.nanoTime() + TimeUnit.MINUTES.toNanos(15)
            while (!onGl { world.isPlayersTurn }) {
                check(scheduler.isAlive && System.nanoTime() < deadline) { "Opening reviews did not finish" }
                Thread.sleep(500)
            }
            onGl {
                session.rewindNotice = null
                val settler = game.getCivilization("France").units.getCivUnits().first { it.name == "Settler" }
                world.mapHolder.setCenterPosition(settler.currentTile.position)
                actors().filterIsInstance<UnitTable>().single().selectUnit(world.selectedGameView.getForeignMapUnitView(settler).tryGetMapUnitView()!!)
                world.shouldUpdate = true
            }
            click("Found city", prefix = true)
            waitFor { onGl { game.getCivilization("France").cities.isNotEmpty() } }
            onGl { app.pushScreen { TechPickerScreen(game.getCivilization("France"), game.ruleset.technologies.getValue("Pottery")) } }
            click("Research Pottery", prefix = true)
            onGl { session.nativeAction { world.nextTurn() } }
            while (scheduler.isAlive) {
                check(System.nanoTime() < deadline) { "Live desktop round timed out" }
                val incoming = onGl {
                    val state = session.social(obj("op" to "state", "epoch" to session.epoch))
                    state.array("trades").filterIsInstance<JsonObject>().firstOrNull { it.text("status") == "PENDING" && it.text("author") != "France" }
                }
                if (incoming != null) {
                    onGl { session.openDiplomacy(incoming.text("author")) }
                    click("Trades")
                    click("Decline") // The scripted human makes no uninspected economic concessions.
                    click("Back to map")
                }
                Thread.sleep(500)
            }
            check(scheduler.exitValue() == 0 && status().turn == 1 && status().handoff == 3L) { "Live round failed; inspect scheduler.log" }
            check(onGl { world.selectedGameView.civView.getCiv().civID == "France" })
            Files.writeString(output.resolve("COMPLETE"), "One scripted human + two gpt-6-luna/medium agents + native Egypt completed a round, including opening reviews.\n")
            println("MANAGED-DESKTOP-LIVE-COMPLETE:$output")
        } finally {
            for (process in processes.asReversed()) {
                if (process.isAlive) process.destroy()
                if (!process.waitFor(30, TimeUnit.SECONDS)) process.destroyForcibly()
            }
            onGl { session.exit() }
        }
    }

    private fun exercise() {
        operator(OperatorRequest("acquire", lease))
        operator(OperatorRequest("barrier", lease))
        val saved = checkNotNull(operator(OperatorRequest("snapshot", lease)).checkpoint)
        operator(OperatorRequest("unbarrier", lease))
        activate()
        waitFor { onGl { app.screen is WorldScreen && world.isPlayersTurn && Gdx.input.inputProcessor != null } }
        onGl {
            session.rewindNotice = null
            val settler = game.getCivilization("France").units.getCivUnits().first { it.name == "Settler" }
            world.mapHolder.setCenterPosition(settler.currentTile.position)
            actors().filterIsInstance<UnitTable>().single().selectUnit(world.selectedGameView.getForeignMapUnitView(settler).tryGetMapUnitView()!!)
            world.shouldUpdate = true
        }
        capture("01-human-start")
        click("Found city", prefix = true)
        waitFor { onGl { game.getCivilization("France").cities.isNotEmpty() } }
        check(UncivFiles.gameInfoFromString(Json.decodeFromString<GameCheckpoint>(Files.readString(output.resolve("checkpoint.json"))).gameJson).getCivilization("France").cities.isNotEmpty())
        onGl { app.pushScreen { TechPickerScreen(game.getCivilization("France"), game.ruleset.technologies.getValue("Pottery")) } }
        click("Research Pottery", prefix = true)
        waitFor { onGl { game.getCivilization("France").tech.techsToResearch.firstOrNull() == "Pottery" } }
        capture("02-native-city-and-research")

        onGl { session.openDiplomacy("Rome") }
        Thread.sleep(400)
        onGl { actors().filterIsInstance<TextArea>().first { it.isVisible }.text = "Private human message" }
        click("Send message")
        check(communication("Rome", "read").toString().contains("Private human message"))
        check(!communication("Greece", "read").toString().contains("Private human message"))
        capture("03-private-chat")
        communication("Greece", "send", "to" to "France", "text" to "A message in another conversation")
        waitFor { onGl { session.unreadCount > 0 } }
        onGl { actors().filterIsInstance<SelectBox<*>>().single { it.name == "contacts" }.setSelectedIndex(2) }
        waitFor { onGl { session.unreadCount == 0 } }
        onGl { actors().filterIsInstance<SelectBox<*>>().single { it.name == "contacts" }.setSelectedIndex(1) }
        val composer = onGl {
            chatMessage().apply {
                text = "Unsent draft"
                setSelection(0, 6)
                stage.keyboardFocus = this
            }
        }
        val previousPanel = onGl { app.screen }
        operator(OperatorRequest("barrier", lease))
        communication("France", "state")
        onGl {
            check(app.screen === previousPanel && chatMessage() === composer)
            check(composer.stage.keyboardFocus === composer && composer.selection == "Unsent" && composer.cursorPosition == 6)
            checkNotNull(Gdx.input.inputProcessor).keyTyped('!')
            check(composer.text == "! draft")
        }
        // Separate conversation drafts survive leaving the pane, including the selected channel.
        onGl {
            selectConversation("Greece")
            chatMessage().text = "Greek draft"
        }
        onGl {
            selectConversation("Public")
            chatMessage().text = "Public draft"
        }
        onGl {
            selectConversation("Rome")
            check(chatMessage().text == "! draft")
        }
        click("Back to map")
        waitFor { onGl { app.screen is WorldScreen && Gdx.input.inputProcessor != null } }
        onGl { session.openDiplomacy(null) }
        onGl {
            check(session.panel?.selectedCivilization == "Rome" && chatMessage().text == "! draft")
            selectConversation("Greece")
            check(chatMessage().text == "Greek draft")
            selectConversation("Public")
            check(chatMessage().text == "Public draft")
            selectConversation("Rome")
        }
        operator(OperatorRequest("unbarrier", lease))
        activate()
        waitFor { onGl { app.screen is DesktopDiplomacyScreen && session.operator.humanCanAct("France", game) } }
        exerciseGroups()
        click("Trades")
        click("Open trade")
        clickNamed("OurAvail:Gold:Gold")
        onGl { actors().filterIsInstance<TextField>().single { it.isVisible }.text = "15" }
        click("OK")
        capture("03a-native-trade-draft")
        click("Offer trade")
        capture("03b-native-final-terms")
        click("Publish")
        waitFor { onGl { session.executor.communicationState.trades.activeProposal != null && app.screen is DiplomacyScreen } }
        val proposed = onGl { session.executor.communicationState.trades.proposals.getValue(checkNotNull(session.executor.communicationState.trades.activeProposal)).last() }
        check(proposed.author == "France" && proposed.clauses.single().item.amount == 15)
        val counter = communication(
            "Rome",
            "trade_offer",
            "partner" to "France",
            "proposal" to proposed.proposal,
            "version" to proposed.version,
            "give" to emptyList<String>(),
            "receive" to listOf(obj("type" to "Gold", "name" to "Gold", "amount" to 20, "duration" to -1)),
        )
        check(counter["trade"] != null) { counter }
        click("Counteroffer")
        clickNamed("OurTrade:Gold:Gold")
        onGl { actors().filterIsInstance<TextField>().single { it.isVisible }.text = "18" }
        click("OK")
        click("Publish counteroffer")
        click("Publish")
        waitFor { onGl { session.executor.communicationState.trades.proposals.getValue(proposed.proposal).last().version == 3 } }
        val humanCounter = onGl { session.executor.communicationState.trades.proposals.getValue(proposed.proposal).last() }
        check(humanCounter.clauses.single().from == "France" && humanCounter.clauses.single().item.amount == 18)
        communication("Rome", "trade_decide", "proposal" to proposed.proposal, "version" to 3, "decision" to "accept")
        waitFor { onGl { session.executor.communicationState.trades.activeProposal == null } }
        check(onGl { game.getCivilization("France").gold == 982 })
        capture("03c-native-counteroffer-accepted")

        fun publishSmallGift(amount: Int): EngineTradeProposal {
            if (onGl { actors().filterIsInstance<Label>().any { it.text.toString() == "New proposal" } }) click("New proposal")
            clickNamed("OurAvail:Gold:Gold")
            onGl { actors().filterIsInstance<TextField>().single { it.isVisible }.text = amount.toString() }
            click("OK")
            click("Offer trade")
            click("Publish")
            waitFor { onGl { session.executor.communicationState.trades.activeProposal != null } }
            return onGl { session.executor.communicationState.trades.proposals.getValue(checkNotNull(session.executor.communicationState.trades.activeProposal)).last() }
        }
        val withdrawn = publishSmallGift(1)
        click("Withdraw")
        waitFor { onGl { session.executor.communicationState.trades.proposals.getValue(withdrawn.proposal).last().status == "WITHDRAWN" } }
        val declined = publishSmallGift(2)
        communication(
            "Rome",
            "trade_offer",
            "partner" to "France",
            "proposal" to declined.proposal,
            "version" to 1,
            "give" to emptyList<String>(),
            "receive" to listOf(obj("type" to "Gold", "name" to "Gold", "amount" to 3, "duration" to -1)),
        )
        // An open confirmation is consent to its displayed version, never to a later counter.
        click("Accept")
        communication(
            "Rome",
            "trade_offer",
            "partner" to "France",
            "proposal" to declined.proposal,
            "version" to 2,
            "give" to emptyList<String>(),
            "receive" to listOf(obj("type" to "Gold", "name" to "Gold", "amount" to 4, "duration" to -1)),
        )
        click("Accept")
        check(onGl { session.executor.communicationState.trades.proposals.getValue(declined.proposal).last().status == "PENDING" })
        check(onGl { game.getCivilization("France").gold == 982 })
        capture("03d-stale-native-consent")
        click("Decline")
        waitFor { onGl { session.executor.communicationState.trades.proposals.getValue(declined.proposal).last().status == "DECLINED" } }
        closeNativeDiplomacy()

        // A stopped attempt restores both native actions and communication, in the same window.
        onGl {
            session.openDiplomacy("Rome")
        }
        click("Chat")
        onGl { chatMessage().text = "Discard this abandoned draft" }
        operator(OperatorRequest("barrier", lease))
        val epoch = "desktop-rewind"
        operator(OperatorRequest("restore", leaseId = lease, epoch = epoch, checkpoint = saved))
        val rotated = operator(OperatorRequest("rotate_players", leaseId = lease, epoch = epoch)).players
        check(rotated.single { it.civilizationId == "France" }.controller == "HUMAN")
        tokens = rotated.associate { it.civilizationId to it.token }
        operator(OperatorRequest("restore_complete", leaseId = lease, epoch = epoch))
        operator(OperatorRequest("unbarrier", lease))
        activate()
        waitFor { onGl { app.screen is DesktopDiplomacyScreen && world.isPlayersTurn } }
        check(onGl { actors().filterIsInstance<TextArea>().none { it.text == "Discard this abandoned draft" } })
        check(onGl { session.diplomacyDrafts.chats.isEmpty() && GROUP_LABEL !in conversationNames() })
        communication("Rome", "send", "recipients" to listOf("Greece", "France"), "text" to "Discovered incoming group after rewind")
        waitFor { onGl { GROUP_LABEL in conversationNames() && session.unreadCount == 1 } }
        onGl { selectConversation(GROUP_LABEL) }
        waitFor { onGl { session.unreadCount == 0 && hasChatText("Discovered incoming group after rewind") } }
        onGl {
            check(chatMessage().text.isEmpty())
            chatMessage().text = "Reply to all discovered participants"
        }
        click("Send message")
        val reply = communication("Greece", "read").array("events").last().jsonObject.getValue("body").jsonObject
        check(!reply.flag("public") && reply.array("participants").size == 3 && reply.text("text") == "Reply to all discovered participants")
        capture("04a-incoming-group")
        onGl { selectConversation("Rome") }
        click("Back to map")
        waitFor { onGl { app.screen is WorldScreen && world.isPlayersTurn } }
        check(onGl { game.getCivilization("France").cities.isEmpty() && session.rewindNotice != null })
        check(!communication("Rome", "read").toString().contains("Private human message"))
        capture("04-rewound-native-and-social-state")

        // Native End Turn delegates to the authoritative handoff, even without a city.
        onGl { session.nativeAction { world.nextTurn() } }
        waitFor { status().activeCivilization == "Rome" }
        check(status().handoff == 1L)
        val roman = checkNotNull(activate().playerToken)
        waitFor { onGl { app.screen is WorldScreen && !world.isPlayersTurn } }
        check(onGl { world.selectedGameView.civView.getCiv().civID == "France" })
        check(!onGl { session.nativeAction { com.unciv.logic.civilization.PlayerOperations(game.getCivilization("France")).trySetResearchQueue(listOf("Pottery")) } })
        val offered = communication("Rome", "trade_offer", "partner" to "France", "give" to listOf(obj("type" to "Gold", "name" to "Gold", "amount" to 25, "duration" to -1)), "receive" to emptyList<String>())
        check(offered["trade"] != null) { offered }
        val trade = offered.getValue("trade").jsonObject
        check(trade["deadlineMillis"] == kotlinx.serialization.json.JsonNull)
        waitFor { onGl { !world.isPlayersTurn && Gdx.input.inputProcessor != null } }
        onGl { session.openDiplomacy("Rome") }
        click("Trades")
        click("Open trade")
        capture("05-incoming-trade-off-turn")
        click("Accept")
        capture("06-exact-consent")
        click("Accept")
        waitFor { onGl { session.executor.communicationState.trades.activeProposal == null } }
        check(onGl { game.getCivilization("France").gold == 1025 })
        closeNativeDiplomacy()
        onGl { session.openDiplomacy("Rome") }
        click("Chat")
        val activeComposer = onGl {
            chatMessage().apply {
                text = "Keep typing"
                cursorPosition = 4
                stage.keyboardFocus = this
            }
        }
        finish(roman)
        val greek = checkNotNull(activate().playerToken)
        finish(greek)
        check(status().handoff == 3L && status().activeCivilization == "France" && status().turn == 1)
        onGl {
            check(chatMessage() === activeComposer && activeComposer.stage.keyboardFocus === activeComposer)
            check(activeComposer.cursorPosition == 4)
            checkNotNull(Gdx.input.inputProcessor).keyTyped('!')
            check(activeComposer.text == "Keep! typing")
        }

        operator(OperatorRequest("barrier", lease, pauseReason = "STRATEGIC_REVIEW"))
        waitFor { onGl { session.message().contains("Strategic reviews") } }
        val denied = communication("Rome", "send", "to" to "France", "text" to "Agents still wait during review")
        check(denied["error"] != null)
        onGl { chatMessage().text = "A question queued during review" }
        click("Send message")
        check(!communication("Rome", "read").toString().contains("A question queued during review"))
        check(onGl { chatMessage().text.isEmpty() && actors().filterIsInstance<Label>().any { it.text.contains("Queued ·") } })
        onGl {
            selectConversation(GROUP_LABEL)
            check(!hasChatText("A question queued during review"))
            chatMessage().text = "A group question queued during review"
        }
        click("Send message")
        check(!communication("Greece", "read").toString().contains("A group question queued during review"))
        onGl {
            check(hasChatText("A group question queued during review"))
            selectConversation("Rome")
            check(hasChatText("A question queued during review") && !hasChatText("A group question queued during review"))
        }
        capture("07-review-barrier")
        click("Back to map")
        waitFor { onGl { app.screen is WorldScreen && !world.isPlayersTurn && Gdx.input.inputProcessor != null } }
        onGl { session.openDiplomacy("Rome") }
        check(onGl { actors().filterIsInstance<Label>().any { it.text.contains("A question queued during review") } })
        operator(OperatorRequest("unbarrier", lease))
        check(communication("Rome", "read").toString().contains("A question queued during review"))
        check(!communication("Greece", "read").toString().contains("A question queued during review"))
        check(communication("Greece", "read").toString().contains("A group question queued during review"))
        check(communication("Rome", "read").toString().contains("A group question queued during review"))
        waitFor { onGl { actors().filterIsInstance<Label>().none { it.text.contains("Queued ·") } } }
        click("Back to map")
        activate()
        waitFor { onGl { world.isPlayersTurn } }
        check(onGl { session.executor.communicationState.telemetry.turns.values.any { it.civilization == "France" && it.nativeActions.isNotEmpty() } })
        capture("08-next-human-turn")
    }

    private const val GROUP_LABEL = "Group · France, Greece, Rome"
    private fun conversationNames() = actors().filterIsInstance<SelectBox<*>>().single { it.name == "contacts" }.items.toList()
    private fun hasChatText(text: String) = actors().filterIsInstance<Label>().any { it.text.contains(text) }

    private fun exerciseGroups() {
        click("New group")
        onGl {
            check(actors().filterIsInstance<Button>().single { button -> button.children.filterIsInstance<Label>().any { it.text.toString() == "Open conversation" } }.isDisabled)
            check(actors().none { it.name in listOf("group-recipient-Egypt", "group-recipient-France") })
        }
        clickNamed("group-recipient-Greece")
        onGl { check(actors().filterIsInstance<Label>().single { it.name == "group-participants" }.text.toString() == "Private · Participants: France, Greece") }
        clickNamed("group-recipient-Rome")
        capture("03e-new-group-preview")
        click("Open conversation")
        onGl {
            check(session.panel?.selectedCivilization == "Rome")
            check(hasChatText("Private · Participants: France, Greece, Rome"))
            check(!hasChatText("Private human message"))
            chatMessage().apply {
                text = "Private group draft"
                cursorPosition = 7
                stage.keyboardFocus = this
            }
            selectConversation("Rome")
            check(chatMessage().text == "! draft" && hasChatText("Private human message"))
        }
        // Reversing checkbox selection reopens the same unsent conversation and its cursor.
        click("New group")
        clickNamed("group-recipient-Rome")
        clickNamed("group-recipient-Greece")
        click("Open conversation")
        onGl {
            check(conversationNames().count { it == GROUP_LABEL } == 1)
            check(chatMessage().text == "Private group draft" && chatMessage().cursorPosition == 7)
            chatMessage().text = "Private group message"
        }
        click("Send message")
        for (recipient in listOf("Rome", "Greece")) {
            val message = communication(recipient, "read").array("events").last().jsonObject.getValue("body").jsonObject
            check(!message.flag("public") && message.array("participants").size == 3 && message.text("text") == "Private group message")
        }
        onGl {
            chatMessage().apply {
                text = "Keep group draft"
                cursorPosition = 4
                stage.keyboardFocus = this
            }
            selectConversation("Rome")
        }
        communication("Greece", "send", "recipients" to listOf("Rome", "France"), "text" to "Incoming group only")
        communication("Greece", "send", "to" to "France", "text" to "Incoming direct only")
        communication("Greece", "send", "text" to "Incoming public only")
        waitFor { onGl { session.unreadCount == 3 } }
        onGl { selectConversation(GROUP_LABEL) }
        waitFor { onGl { session.unreadCount == 2 } }
        val composer = onGl {
            check(hasChatText("Incoming group only") && !hasChatText("Incoming direct only") && !hasChatText("Incoming public only"))
            check(chatMessage().text == "Keep group draft" && chatMessage().cursorPosition == 4)
            chatMessage().also { it.stage.keyboardFocus = it }
        }
        communication("Rome", "send", "recipients" to listOf("France", "Greece"), "text" to "Group refresh while typing")
        waitFor { onGl { hasChatText("Group refresh while typing") } }
        onGl {
            check(chatMessage() === composer && composer.stage.keyboardFocus === composer && composer.cursorPosition == 4)
            selectConversation("Greece")
        }
        waitFor { onGl { session.unreadCount == 1 } }
        onGl {
            check(hasChatText("Incoming direct only") && !hasChatText("Incoming group only"))
            selectConversation("Public")
        }
        waitFor { onGl { session.unreadCount == 0 } }
        onGl {
            check(hasChatText("Incoming public only") && !hasChatText("Private group message"))
            selectConversation("Rome")
            selectConversation(GROUP_LABEL)
        }
        click("Trades")
        onGl { check(session.panel?.selectedCivilization == "Rome" && GROUP_LABEL !in conversationNames()) }
        click("Chat")
        onGl { check(session.panel?.selectedChat?.label("France") == GROUP_LABEL && chatMessage().text == "Keep group draft") }
        capture("03f-private-group-chat")
        click("Back to map")
        waitFor { onGl { app.screen is WorldScreen } }
        onGl { session.openDiplomacy(null) }
        onGl {
            check(session.panel?.selectedChat?.label("France") == GROUP_LABEL)
            check(chatMessage().text == "Keep group draft" && chatMessage().cursorPosition == 4)
            check(chatMessage().stage.keyboardFocus === chatMessage())
        }
    }

    private fun status() = operator(OperatorRequest("status")).status
    private fun activate(): OperatorResponse {
        val state = status()
        return operator(OperatorRequest("activate", lease, state.epoch, state.serverInstance, state.handoff, state.activeCivilization, UUID.randomUUID().toString()))
    }
    private fun operator(request: OperatorRequest): OperatorResponse = OperatorControl.json.decodeFromString(raw("/operator", OperatorControl.json.encodeToString(request), "O".repeat(43)))
    private fun raw(path: String, body: String, token: String): String {
        val response = client.send(
            HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofSeconds(30))
                .header("Authorization", "Bearer $token").header("Content-Type", "application/json").header("Accept", "application/json, text/event-stream")
                .POST(HttpRequest.BodyPublishers.ofString(body)).build(),
            HttpResponse.BodyHandlers.ofString(),
        )
        check(response.statusCode() == 200) { "HTTP ${response.statusCode()}: ${response.body()}" }
        return response.body()
    }
    private fun tool(token: String, name: String, args: JsonObject = obj()): JsonObject {
        val result = Json.parseToJsonElement(raw("/mcp", obj("jsonrpc" to "2.0", "id" to 1, "method" to "tools/call", "params" to obj("name" to name, "arguments" to args)).toString(), token)).jsonObject.getValue("result").jsonObject
        return result.getValue("structuredContent").jsonObject
    }
    private fun communication(civ: String, op: String, vararg fields: Pair<String, Any?>): JsonObject {
        val request = obj("op" to op, "epoch" to status().epoch, *fields)
        val body = if (op == "read") JsonObject(request + obj("cursor" to 0, "limit" to 100)) else JsonObject(request + obj("requestId" to UUID.randomUUID().toString()))
        return tool(tokens.getValue(civ), "player_communication", obj("request" to body))
    }
    private fun finish(token: String) {
        operator(OperatorRequest("heartbeat", lease))
        val turn = PlayerTurnJson.json.decodeFromString<PlayerTurnSnapshot>(tool(token, "player_turn").toString())
        val request = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, TurnCommandKind.END_TURN, deferChoices = true, deferReason = "Desktop integration probe").reviewedFor(turn)
        val result = tool(token, "player_command", PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject)
        check(result["error"] == null) { result }
    }
    private fun actors(): List<Actor> {
        fun descendants(actor: Actor): List<Actor> = listOf(actor) + if (actor is Group) actor.children.flatMap(::descendants) else emptyList()
        return descendants((app.screen as BaseScreen).stage.root)
    }
    private fun chatMessage(): TextArea = actors().filterIsInstance<TextArea>().single { it.name == "chat-message" }
    private fun selectConversation(name: String) {
        val contacts = actors().filterIsInstance<SelectBox<*>>().single { it.name == "contacts" }
        contacts.setSelectedIndex(contacts.items.toList().indexOf(name).also { check(it >= 0) })
    }
    private fun click(text: String, prefix: Boolean = false) {
        Thread.sleep(350)
        onGl {
            val label = actors().filterIsInstance<Label>().lastOrNull {
                val value = it.text.toString().filter { c -> c.code !in 0xe000..0xf8ff }.trim()
                if (prefix) value.startsWith(text) else value == text
            } ?: error("No '$text' control: ${actors().filterIsInstance<Label>().map { it.text.toString() }}")
            val button = generateSequence(label as Actor?) { it.parent }.filterIsInstance<Button>().first()
            check(!button.isDisabled) { "Disabled: $text" }
            generateSequence(button.parent as Actor?) { it.parent }.filterIsInstance<ScrollPane>().firstOrNull()?.let { pane ->
                val point = button.localToAscendantCoordinates(pane.actor, Vector2())
                pane.scrollTo(point.x, point.y, button.width, button.height)
                pane.updateVisualScroll()
                session.render { checkNotNull(app.screen).render(0f) }
            }
            val point = button.localToStageCoordinates(Vector2(button.width / 2, button.height / 2))
            (app.screen as BaseScreen).stage.stageToScreenCoordinates(point)
            checkNotNull(Gdx.input.inputProcessor).touchDown(point.x.toInt(), point.y.toInt(), 0, 0)
            checkNotNull(Gdx.input.inputProcessor).touchUp(point.x.toInt(), point.y.toInt(), 0, 0)
        }
        Thread.sleep(350)
    }
    private fun closeNativeDiplomacy() {
        Thread.sleep(350)
        onGl {
            checkNotNull(Gdx.input.inputProcessor).keyDown(com.badlogic.gdx.Input.Keys.ESCAPE)
            checkNotNull(Gdx.input.inputProcessor).keyUp(com.badlogic.gdx.Input.Keys.ESCAPE)
        }
        Thread.sleep(350)
        if (onGl { app.screen is DesktopDiplomacyScreen }) click("Back to map")
    }
    private fun clickNamed(name: String) {
        Thread.sleep(350)
        onGl {
            val button = actors().filterIsInstance<Button>().single { it.name == name }
            check(!button.isDisabled) { "Disabled: $name" }
            val pane = generateSequence(button.parent as Actor?) { it.parent }.filterIsInstance<ScrollPane>().firstOrNull()
            if (pane != null) {
                val point = button.localToAscendantCoordinates(pane.actor, Vector2())
                pane.scrollTo(point.x, point.y, button.width, button.height)
                pane.updateVisualScroll()
                session.render { checkNotNull(app.screen).render(0f) }
            }
            val point = button.localToStageCoordinates(Vector2(button.width / 2, button.height / 2))
            (app.screen as BaseScreen).stage.stageToScreenCoordinates(point)
            checkNotNull(Gdx.input.inputProcessor).touchDown(point.x.toInt(), point.y.toInt(), 0, 0)
            checkNotNull(Gdx.input.inputProcessor).touchUp(point.x.toInt(), point.y.toInt(), 0, 0)
        }
        Thread.sleep(350)
    }
    private fun capture(name: String) {
        Thread.sleep(350)
        operator(OperatorRequest("heartbeat", lease))
        onGl {
            session.render { checkNotNull(app.screen).render(0f) }
            val pixels = Pixmap.createFromFrameBuffer(0, 0, Gdx.graphics.backBufferWidth, Gdx.graphics.backBufferHeight)
            try {
                PixmapIO.writePNG(FileHandle(output.resolve("$name.png").toFile()), pixels, -1, true)
            } finally {
                pixels.dispose()
            }
            Files.writeString(output.resolve("$name.txt"), actors().filterIsInstance<Label>().joinToString("\n") { it.text.toString() })
        }
    }
    private fun waitFor(condition: () -> Boolean) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(25)
        while (!condition()) {
            check(System.nanoTime() < deadline) { "Desktop condition timed out" }
            Thread.sleep(50)
        }
    }
    private fun <T> onGl(block: () -> T): T {
        val result = CompletableFuture<T>()
        Gdx.app.postRunnable {
            try {
                result.complete(block())
            } catch (failure: Throwable) {
                result.completeExceptionally(failure)
            }
        }
        return result.get(30, TimeUnit.SECONDS)
    }
}
