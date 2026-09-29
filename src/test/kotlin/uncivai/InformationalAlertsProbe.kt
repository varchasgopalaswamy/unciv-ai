package uncivai

import com.unciv.json.json
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PopupAlert
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.mcp.PlayerMcpBinding
import uncivai.mcp.PlayerMcpServer
import uncivai.player.PlayerAlert
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Files
import java.nio.file.Path
import java.time.Duration
import java.util.UUID

@Serializable
private data class InformationalAlertRecovery(
    val snapshot: PlayerTurnSnapshot,
    val acknowledgement: TurnCommandRequest? = null,
    val receipt: TurnCommandReceipt? = null,
)

/** Trusted fixture writes create popup queues; player reads and acknowledgements use verified HTTPS. */
object InformationalAlertsProbe {
    private const val TECHNOLOGY_NAME = "Iron Working"
    private const val WONDER_NAME = "Stonehenge"
    private val tokens = mapOf("Rome" to "R".repeat(43), "Greece" to "G".repeat(43))

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val stage = args[1]
        val checkpoint = root.resolve("checkpoint.json")
        val fresh = stage == "content" || stage == "prepare"
        val runtime = if (fresh) {
            HeadlessRuntime.createDuel(root.resolve(stage), 412L)
        } else {
            HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve(stage))
        }
        runtime.use {
            if (fresh) arrangeAlerts(runtime)
            if (stage == "prepare") runtime.executor.enableCheckpoints(checkpoint)
            val host = PlayerHost(runtime)
            val tls = TlsTestFixture(Files.createDirectories(root.resolve("$stage-tls")))
            val bindings = tokens.map { (civilization, token) -> PlayerMcpBinding(token, host.bindPlayer(civilization)) }
            PlayerMcpServer(bindings, 0, tls = tls.tls).use { server ->
                AlertClient("https://localhost:${server.start()}/mcp", tls).use { api ->
                    when (stage) {
                        "content" -> verifyContentAndGuards(runtime, host, api)

                        "prepare" -> {
                            val snapshot = api.turn("Rome")
                            verifyContents(runtime.game.getCivilization("Rome"), snapshot, active = true)
                            saveRecovery(root, InformationalAlertRecovery(snapshot))
                        }

                        "acknowledge" -> acknowledgeAfterRestart(root, runtime, api)

                        "replay" -> replayAndContinue(root, runtime, api)

                        else -> error("Unknown stage: $stage")
                    }
                }
            }
        }
    }

    private fun arrangeAlerts(runtime: HeadlessRuntime) {
        for (civilization in tokens.keys.map(runtime.game::getCivilization)) {
            civilization.popupAlerts.clear()
            civilization.popupAlerts.addAll(
                listOf(
                    PopupAlert(AlertType.StartIntro, ""),
                    PopupAlert(AlertType.TechResearched, TECHNOLOGY_NAME),
                    PopupAlert(AlertType.WonderBuilt, WONDER_NAME),
                    PopupAlert(AlertType.GoldenAge, ""),
                ),
            )
        }
    }

    private fun verifyContentAndGuards(runtime: HeadlessRuntime, host: PlayerHost, api: AlertClient) {
        val player = runtime.game.getCivilization("Rome")
        val other = runtime.game.getCivilization("Greece")
        val raw = json().toJson(runtime.game)
        val future = host.bindPlayer("Rome").researchDetails(listOf("Future Tech")).technologies.single()
        check("Who knows what the future holds?".tr() in future.description!!)
        check("Who knows what the future holds?".tr() !in future.gameplayDescription!!)
        check(future.gameplayDescription!!.isNotBlank()) { "Future Tech's actual effects were removed" }
        val effects = host.bindPlayer("Rome").researchDetails(listOf(TECHNOLOGY_NAME)).technologies.single()
        check(effects.description == effects.gameplayDescription) { "Mechanical research description changed" }
        val snapshot = api.turn("Rome")
        val waiting = api.turn("Greece")
        check(json().toJson(runtime.game) == raw) { "Reading popup content changed the game" }
        verifyContents(player, snapshot, active = true)
        verifyContents(other, waiting, active = false)
        check(snapshot == api.turn("Rome"))
        check(snapshot == PlayerTurnJson.json.decodeFromString<PlayerTurnSnapshot>(PlayerTurnJson.encode(snapshot)))
        val romanTech = snapshot.alerts.single { it.kind == "TechResearched" }.content!!.paragraphs.single()
        val greekTech = waiting.alerts.single { it.kind == "TechResearched" }.content!!.paragraphs.single()
        check("Legion".tr() in romanTech && "Swordsman".tr() !in romanTech)
        check("Swordsman".tr() in greekTech && "Legion".tr() !in greekTech)
        val detached = host.bindPlayer("Rome").turn()
        val paragraphs = detached.alerts.single { it.kind == "StartIntro" }.content!!.paragraphs
        check(runCatching { (paragraphs as MutableList).clear() }.exceptionOrNull() is UnsupportedOperationException)

        // Opponent state must not influence the player's text or opaque revision.
        check(!player.knows(other))
        other.addGold(421)
        other.tech.techsToResearch.add("Pottery")
        other.units.getCivUnits().first().promotions.XP += 9
        other.popupAlerts.add(PopupAlert(AlertType.TechResearched, "SECRET-FOREIGN-POPUP"))
        check(api.turn("Rome") == snapshot) { "Foreign state changed permitted popup observations" }

        val first = snapshot.alerts.first()
        api.reject("Greece", command(waiting, waiting.alerts.first().handle), "NOT_YOUR_TURN")
        check(waiting.alerts.first().handle == first.handle) { "Alert counters must be player-local" }
        api.reject("Rome", command(snapshot, "alert999"), "OPTION_UNAVAILABLE")
        api.reject("Rome", command(snapshot, "unknown-alert"), "OPTION_UNAVAILABLE")
        val staleAfterPause = command(snapshot, first.handle)
        runtime.executor.pause()
        val paused = api.turn("Rome")
        check(paused.alerts.all { !it.canAcknowledge })
        check(paused.alerts.map { it.content } == snapshot.alerts.map { it.content })
        api.reject("Rome", staleAfterPause, "GAME_PAUSED")
        runtime.executor.resume()
        api.reject("Rome", staleAfterPause, "STALE_REVISION")

        // Unsupported consequential alerts and invalid subject references remain opaque and blocking.
        for ((kind, value) in listOf(
            AlertType.DeclarationOfFriendship to "SECRET-DIPLOMACY-TARGET",
            AlertType.Event to "SECRET-EVENT",
            AlertType.TechResearched to "SECRET-MISSING-TECH",
            AlertType.WonderBuilt to "SECRET-MISSING-WONDER",
            AlertType.WonderBuilt to "Monument",
        )) {
            val invalid = PopupAlert(kind, value)
            player.popupAlerts.add(invalid)
            val mixed = api.turn("Rome")
            val unavailable = mixed.alerts.last()
            check(!unavailable.canAcknowledge && unavailable.content == null && unavailable.subject == null)
            check(!mixed.pending.single { it.kind == "PopupAlerts" }.supported && !mixed.canEndTurn)
            check(!value.startsWith("SECRET-") || value !in PlayerTurnJson.encode(mixed))
            api.reject("Rome", command(mixed, unavailable.handle), "OPTION_UNAVAILABLE")
            check(player.popupAlerts.last() === invalid)
            player.popupAlerts.remove(invalid)
        }

        // A command receipt is not evidence that the current popup content has been delivered.
        val beforeResearch = api.turn("Rome")
        val research = api.execute(
            "Rome",
            command(beforeResearch, null, TurnCommandKind.SET_RESEARCH_QUEUE)
                .copy(queue = listOf(beforeResearch.researchOptions.first())),
        )
        val unread = command(beforeResearch, first.handle).copy(revision = research.revision)
        api.reject("Rome", unread, "OBSERVATION_REQUIRED")
        api.tool("Rome", "player_summary")
        api.reject("Rome", unread, "OBSERVATION_REQUIRED")
        val observed = api.turn("Rome")
        val introReceipt = acknowledgeOnly(runtime, api, observed, observed.alerts.first())
        val unreadSecond = command(observed, observed.alerts[1].handle).copy(revision = introReceipt.revision)
        api.reject("Rome", unreadSecond, "OBSERVATION_REQUIRED")

        // A visible text change invalidates the observed revision before any dismissal can happen.
        val beforeTextChange = api.turn("Rome")
        val techAlert = beforeTextChange.alerts.single { it.kind == "TechResearched" }
        val staleText = command(beforeTextChange, techAlert.handle)
        val technology = runtime.game.ruleset.technologies.getValue(TECHNOLOGY_NAME)
        val originalQuote = technology.quote
        technology.quote = "A changed quotation visible in the popup"
        api.reject("Rome", staleText, "STALE_REVISION")
        val changed = api.turn("Rome")
        check(changed.revision != beforeTextChange.revision)
        check(changed.alerts.single { it.kind == "TechResearched" }.content!!.quote == technology.quote.tr())
        check(techAlert.content!!.quote == originalQuote.tr()) { "Detached content changed in place" }
        acknowledgeOnly(runtime, api, changed, changed.alerts.single { it.kind == "TechResearched" })
        technology.quote = originalQuote

        for (kind in listOf("WonderBuilt", "GoldenAge")) {
            val before = api.turn("Rome")
            acknowledgeOnly(runtime, api, before, before.alerts.single { it.kind == kind })
        }
        val complete = api.turn("Rome")
        check(complete.alerts.isEmpty() && complete.pending.none { it.kind == "PopupAlerts" })
        check(other.popupAlerts.size == 5) { "Acknowledgements consumed the other player's alerts" }
        api.reject("Rome", command(complete, first.handle), "OPTION_UNAVAILABLE")
    }

    private fun verifyContents(player: Civilization, snapshot: PlayerTurnSnapshot, active: Boolean) {
        check(snapshot.alerts.size == 4)
        check(snapshot.alerts.all { it.canAcknowledge == active && it.content != null })
        check(snapshot.pending.single { it.kind == "PopupAlerts" }.supported && !snapshot.canEndTurn)
        val intro = snapshot.alerts.single { it.kind == "StartIntro" }.content!!
        check(intro.title == player.getLeaderDisplayName())
        check(intro.paragraphs == listOf(player.nation.startIntroPart1.tr(), player.nation.startIntroPart2.tr()))
        check(intro.quote == null && intro.acknowledgement == "Let's begin!".tr())
        check(intro.kind == "StartIntro" && intro.gameplayParagraphs == emptyList<String>())
        val technology = player.gameInfo.ruleset.technologies.getValue(TECHNOLOGY_NAME)
        val tech = snapshot.alerts.single { it.kind == "TechResearched" }
        check(tech.subject == TECHNOLOGY_NAME && tech.content!!.title == technology.name.tr())
        check(tech.content!!.paragraphs == listOf(technology.getDescription(player).tr()))
        check(tech.content!!.quote == technology.quote.tr() && tech.content!!.acknowledgement == "Close".tr())
        check(tech.content!!.gameplayParagraphs == tech.content!!.paragraphs)
        val wonder = player.gameInfo.ruleset.buildings.getValue(WONDER_NAME)
        val built = snapshot.alerts.single { it.kind == "WonderBuilt" }
        check(built.subject == WONDER_NAME && built.content!!.title == wonder.name.tr())
        check(built.content!!.paragraphs == listOf(wonder.getShortDescription().tr()))
        check(built.content!!.quote == wonder.quote.takeIf { it.isNotEmpty() }?.tr())
        check(built.content!!.acknowledgement == "Close".tr())
        check(built.content!!.gameplayParagraphs == built.content!!.paragraphs)
        val golden = snapshot.alerts.single { it.kind == "GoldenAge" }.content!!
        check(golden.title == "GOLDEN AGE".tr())
        check(golden.paragraphs == listOf("Your citizens have been happy with your rule for so long that the empire enters a Golden Age!".tr()))
        check(golden.quote == null && golden.acknowledgement == "Close".tr())
        check(golden.kind == "GoldenAge" && golden.gameplayParagraphs == emptyList<String>())
    }

    private fun acknowledgeAfterRestart(root: Path, runtime: HeadlessRuntime, api: AlertClient) {
        val saved = loadRecovery(root)
        val stale = command(saved.snapshot, saved.snapshot.alerts.first().handle)
        // Rebuild the observation; durable handles survive, old revisions do not.
        api.reject("Rome", stale, "STALE_REVISION")
        val observed = api.turn("Rome")
        assertRebuiltObservation(saved.snapshot, observed)
        val request = stale.copy(revision = observed.revision)
        val receipt = acknowledgeOnly(runtime, api, observed, observed.alerts.first(), request)
        val after = api.turn("Rome")
        check(after.alerts == observed.alerts.drop(1))
        saveRecovery(root, InformationalAlertRecovery(after, request, receipt))
    }

    private fun replayAndContinue(root: Path, runtime: HeadlessRuntime, api: AlertClient) {
        val saved = loadRecovery(root)
        val raw = json().toJson(runtime.game)
        check(api.execute("Rome", checkNotNull(saved.acknowledgement)) == saved.receipt)
        check(json().toJson(runtime.game) == raw) { "Committed acknowledgement replay changed game state" }
        api.reject("Rome", command(saved.snapshot, saved.snapshot.alerts.first().handle), "STALE_REVISION")
        assertRebuiltObservation(saved.snapshot, api.turn("Rome"))
        val initialTurn = saved.snapshot.turn
        for (civilization in listOf("Rome", "Greece", "Rome", "Greece")) {
            finishTurn(runtime, api, civilization)
        }
        val final = api.turn("Rome")
        check(final.turn == initialTurn + 2 && final.phase == "ACTIVE")
        check(final.alerts.isEmpty() && api.turn("Greece").alerts.isEmpty())
        check(runtime.executor.handoff == 4L)
    }

    private fun finishTurn(runtime: HeadlessRuntime, api: AlertClient, civilization: String) {
        repeat(20) {
            val snapshot = api.turn(civilization)
            check(snapshot.phase == "ACTIVE" && snapshot.pending.none { !it.supported })
            val alert = snapshot.alerts.firstOrNull { it.canAcknowledge }
            if (alert != null) {
                checkNotNull(alert.content) { "Fixture must receive each popup's prose before dismissing it" }
                acknowledgeOnly(runtime, api, snapshot, alert, civilization = civilization)
            } else if (snapshot.pending.any { it.kind == "Research" }) {
                api.execute(
                    civilization,
                    command(snapshot, null, TurnCommandKind.SET_RESEARCH_QUEUE)
                        .copy(queue = listOf(snapshot.researchOptions.first())),
                )
            } else if (snapshot.pending.any { it.kind == "Policy" }) {
                api.execute(
                    civilization,
                    command(snapshot, null, TurnCommandKind.ADOPT_POLICY)
                        .copy(selection = snapshot.policyOptions.first()),
                )
            } else {
                check(snapshot.canEndTurn) { "Unexpected remaining choice: ${snapshot.pending}" }
                api.execute(civilization, command(snapshot, null, TurnCommandKind.END_TURN))
                return
            }
        }
        error("Informational fixture exceeded command budget")
    }

    private fun acknowledgeOnly(
        runtime: HeadlessRuntime,
        api: AlertClient,
        snapshot: PlayerTurnSnapshot,
        alert: PlayerAlert,
        request: TurnCommandRequest = command(snapshot, alert.handle),
        civilization: String = "Rome",
    ): TurnCommandReceipt {
        checkNotNull(alert.content)
        val player = runtime.game.getCivilization(civilization)
        val index = snapshot.alerts.indexOf(alert)
        val rawAlert = player.popupAlerts.removeAt(index)
        val expected = json().toJson(runtime.game)
        player.popupAlerts.add(index, rawAlert)
        val receipt = api.execute(civilization, request)
        check(json().toJson(runtime.game) == expected) { "Acknowledgement changed more than the selected popup" }
        check(api.execute(civilization, request) == receipt)
        check(json().toJson(runtime.game) == expected) { "Receipt replay repeated acknowledgement effects" }
        return receipt
    }

    private fun command(snapshot: PlayerTurnSnapshot, target: String?, kind: TurnCommandKind = TurnCommandKind.ACKNOWLEDGE_ALERT) = TurnCommandRequest(UUID.randomUUID().toString(), snapshot.epoch, snapshot.revision, kind, target).reviewedFor(snapshot)

    private fun saveRecovery(root: Path, recovery: InformationalAlertRecovery) {
        Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(recovery))
    }

    private fun loadRecovery(root: Path): InformationalAlertRecovery = PlayerTurnJson.json.decodeFromString(Files.readString(root.resolve("expected.json")))

    private class AlertClient(private val url: String, tls: TlsTestFixture) : AutoCloseable {
        private val client = HttpClient.newBuilder().sslContext(tls.context).connectTimeout(Duration.ofSeconds(5)).build()

        fun turn(civilization: String): PlayerTurnSnapshot = PlayerTurnJson.json.decodeFromString(tool(civilization, "player_turn").toString())

        fun execute(civilization: String, request: TurnCommandRequest): TurnCommandReceipt = PlayerTurnJson.json.decodeFromString(tool(civilization, "player_command", arguments(request)).toString())

        fun reject(civilization: String, request: TurnCommandRequest, expectedCode: String) {
            val result = call(civilization, "player_command", arguments(request))
            check(result.getValue("isError").jsonPrimitive.boolean) { "Expected $expectedCode, got $result" }
            val error = result.getValue("structuredContent").jsonObject.getValue("error").jsonObject
            check(error.getValue("code").jsonPrimitive.content == expectedCode) { "Expected $expectedCode, got $result" }
        }

        fun tool(civilization: String, name: String, arguments: JsonObject = JsonObject(emptyMap())): JsonObject {
            val result = call(civilization, name, arguments)
            check(!result.getValue("isError").jsonPrimitive.boolean) { result.toString() }
            return result.getValue("structuredContent").jsonObject
        }

        private fun arguments(request: TurnCommandRequest) = PlayerTurnJson.json.encodeToJsonElement(TurnCommandRequest.serializer(), request).jsonObject

        private fun call(civilization: String, name: String, arguments: JsonObject): JsonObject {
            val body = """{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"$name","arguments":$arguments}}"""
            val response = client.send(
                HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofSeconds(15))
                    .header("Authorization", "Bearer ${tokens.getValue(civilization)}")
                    .header("Content-Type", "application/json").header("Accept", "application/json, text/event-stream")
                    .POST(HttpRequest.BodyPublishers.ofString(body)).build(),
                HttpResponse.BodyHandlers.ofString(),
            )
            check(response.statusCode() == 200) { "HTTPS ${response.statusCode()}: ${response.body()}" }
            return Json.parseToJsonElement(response.body()).jsonObject.getValue("result").jsonObject
        }

        override fun close() = client.close()
    }
}
