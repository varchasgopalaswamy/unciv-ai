package uncivai

import com.badlogic.gdx.Gdx
import com.badlogic.gdx.InputProcessor
import com.badlogic.gdx.scenes.scene2d.ui.Label
import com.badlogic.gdx.scenes.scene2d.ui.Table
import com.unciv.UncivGame
import com.unciv.json.json
import com.unciv.logic.GameInfo
import com.unciv.ui.ManagedGameSession
import com.unciv.ui.components.extensions.toLabel
import com.unciv.ui.components.extensions.toTextButton
import com.unciv.ui.components.input.onClick
import com.unciv.ui.screens.basescreen.BaseScreen
import com.unciv.ui.screens.diplomacyscreen.DiplomacyScreen
import com.unciv.ui.screens.diplomacyscreen.TradeTable
import com.unciv.ui.screens.worldscreen.WorldScreen
import com.unciv.utils.Concurrency
import com.unciv.view.CivView
import com.unciv.view.ForeignCivView
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.serialization.json.JsonObject
import uncivai.player.PlayerHost
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.util.UUID
import java.util.concurrent.CancellationException
import kotlin.coroutines.AbstractCoroutineContextElement
import kotlin.coroutines.CoroutineContext

/** Native UI authority and presentation for one local player. Operator data never enters the panel. */
internal class DesktopSession(
    private val app: UncivGame,
    private var game: GameInfo,
    override val viewingCivilization: String,
    private val runnerDirectory: Path,
) : ManagedGameSession,
    AutoCloseable {
    private val dispatch = DesktopDispatch()
    internal val executor = GameExecutor(
        game,
        onGameReplaced = { replacement ->
            game = replacement
            app.gameInfo = replacement
            readEvents.clear()
            unreadEvents.clear()
            latestCursor = 0
            rewindNotice = "The interrupted turn was rewound. Messages and actions from that attempt were removed."
            reload = true
        },
        dispatch = dispatch,
        onCommitted = { committed() },
    )
    lateinit var operator: OperatorControl
    var rewindNotice: String? = null
    private var generation = 0L
    private var positionGeneration = 0L
    private var uiDepth = 0
    private var reload = false
    private var loading = false
    private var closing = false
    private var closed = false
    private var lastWorld = ""
    private var lastAuthority = ""
    private var statusLabel: Label? = null
    private var error: String? = null
    private var stoppedScreen = false
    private val readEvents = mutableSetOf<Int>()
    private val unreadEvents = linkedMapOf<Int, JsonObject>()
    private var latestCursor = 0
    private var communicationDirty = true
    internal var panel: DesktopDiplomacyScreen? = null
    internal var tradePanel: DesktopTradeController? = null
    private var tradeDraftToRestore: DesktopTradeDraft? = null
    private val host = PlayerHost(executor)

    private class UiWork(val generation: Long) : AbstractCoroutineContextElement(Key) {
        companion object Key : CoroutineContext.Key<UiWork>
    }

    private val uiDispatcher = object : CoroutineDispatcher() {
        override fun dispatch(context: CoroutineContext, block: Runnable) {
            Gdx.app.postRunnable {
                val work = context[UiWork]
                if (closed || (work != null && work.generation != positionGeneration)) {
                    context.cancel(CancellationException("The displayed position has changed"))
                    block.run() // Resume the cancelled continuation so its parent can finish.
                } else if (work == null) {
                    block.run()
                } else {
                    nativeAction { block.run() }
                }
            }
        }
    }

    override fun uiContext(): CoroutineContext = if (uiDepth > 0) uiDispatcher + UiWork(positionGeneration) else uiDispatcher

    override fun canChangeState(game: GameInfo): Boolean = !closed && !closing && !loading &&
        game === this.game && ::operator.isInitialized && operator.humanCanAct(viewingCivilization, game)

    override fun ownsGame(game: GameInfo) = game === this.game

    override fun controllerLabel(civilization: String): String? = when {
        civilization == viewingCivilization -> "You"
        usesSharedDiplomacy(civilization) -> "Agent"
        else -> null
    }

    override fun failed(failure: Throwable) {
        (executor.mutationFailure ?: failure).printStackTrace(System.err)
        runCatching { executor.haltAfterMutation(failure) }
        error = "The game stopped. Inspect the operator logs before resuming."
        Gdx.graphics.requestRendering()
    }

    override fun canAct(civilization: String): Boolean = uiDepth == 0 ||
        (civilization == viewingCivilization && canChangeState(game))

    private fun authority() = listOf(
        executor.epoch,
        executor.handoff,
        executor.isPaused(),
        executor.communicationPaused,
        executor.communicationState.restorePending,
        executor.communicationState.trades.activeProposal,
    ).joinToString(":")

    private fun committed() {
        generation++
        communicationDirty = true
        val nextAuthority = authority()
        val world = json().toJson(game)
        if (lastAuthority.isNotEmpty() && (lastAuthority != nextAuthority || (uiDepth == 0 && world != lastWorld))) {
            positionGeneration++
            reload = true
            // No cached picker or confirmation may accept input before it is rebuilt.
            Gdx.input.inputProcessor = null
        }
        lastWorld = world
        lastAuthority = nextAuthority
        app.worldScreen?.shouldUpdate = true
        panel?.changed = true
        tradePanel?.changed = true
        Gdx.graphics.requestRendering()
    }

    /** Only actual persistent native changes count; scrolling and text entry do not. */
    internal fun <T> nativeAction(block: () -> T): T {
        if (uiDepth > 0) return block()
        return executor.accessGame {
            val before = json().toJson(game)
            val handoff = executor.handoff
            val beforeCommit = generation
            val permitted = canChangeState(game)
            val player = game.getCivilization(viewingCivilization)
            val wars = game.civilizations.filter { it.isMajorCiv() && it !== player }
                .associate { it.civID to player.isAtWarWith(it) }
            val outcomes = executor.communicationState.outcomes.size
            uiDepth++
            try {
                val result = block()
                if (executor.handoff == handoff && generation == beforeCommit && json().toJson(game) != before) {
                    check(permitted) { "A native UI callback changed the game without turn authority" }
                    game.checkForVictory()
                    executor.turnSessions.values.forEach { it.invalidate() }
                    executor.captureCombatReports()
                    for ((other, wasAtWar) in wars) {
                        if (!wasAtWar && player.isAtWarWith(game.getCivilization(other)) &&
                            executor.communicationState.outcomes.drop(outcomes).none { it.action == "DECLARE_WAR" && other in it.participants }
                        ) {
                            executor.communication.diplomacyEnacted(viewingCivilization, other, "DECLARE_WAR", game.turns)
                        }
                    }
                    executor.telemetry.nativeAction(app.screen?.javaClass?.simpleName ?: "Desktop")
                    executor.commitCheckpoint()
                }
                result
            } catch (failure: Throwable) {
                if (executor.handoff == handoff && json().toJson(game) != before) executor.haltAfterMutation(failure)
                throw failure
            } finally {
                uiDepth--
            }
        }
    }

    override fun input(processor: InputProcessor): InputProcessor {
        val boundGame = game
        val epoch = executor.epoch
        val position = positionGeneration
        fun invoke(action: () -> Boolean): Boolean {
            if (closed || loading || reload || game !== boundGame || epoch != executor.epoch || position != positionGeneration) return false
            if (stoppedScreen) return action()
            // This panel changes the game only through checkpointed player operations.
            return if (app.screen is DesktopDiplomacyScreen) action() else nativeAction(action)
        }
        return object : InputProcessor by processor {
            override fun keyDown(keycode: Int) = invoke { processor.keyDown(keycode) }
            override fun keyUp(keycode: Int) = invoke { processor.keyUp(keycode) }
            override fun keyTyped(character: Char) = invoke { processor.keyTyped(character) }
            override fun touchDown(x: Int, y: Int, pointer: Int, button: Int) = invoke { processor.touchDown(x, y, pointer, button) }
            override fun touchUp(x: Int, y: Int, pointer: Int, button: Int) = invoke { processor.touchUp(x, y, pointer, button) }
            override fun touchDragged(x: Int, y: Int, pointer: Int) = invoke { processor.touchDragged(x, y, pointer) }
            override fun scrolled(x: Float, y: Float) = invoke { processor.scrolled(x, y) }
        }
    }

    internal fun render(block: () -> Unit) {
        if (executor.mutationFailure == null && app.screen !is DesktopDiplomacyScreen && app.worldScreen?.shouldUpdate == true && !loading && !closed) {
            nativeAction(block)
        } else {
            uiDepth++
            try {
                block()
            } finally {
                uiDepth--
            }
        }
    }

    override fun endTurn(worldScreen: WorldScreen) {
        if (!canChangeState(worldScreen.gameInfo)) return
        try {
            val session = host.bindPlayer(viewingCivilization)
            val turn = session.turn()
            if (!turn.canEndTurn && !turn.turnControls.canDeferChoices) {
                error = "Resolve the remaining decisions before ending your turn."
                return
            }
            executor.telemetry.nativeAction("END_TURN")
            session.execute(
                TurnCommandRequest(
                    requestId = "human-" + UUID.randomUUID(),
                    epoch = turn.epoch,
                    revision = turn.revision,
                    kind = TurnCommandKind.END_TURN,
                    reviewId = turn.turnReview?.id,
                    deferChoices = !turn.canEndTurn,
                    deferReason = "The human ended the turn from the desktop.",
                ),
            )
            error = null
        } catch (failure: uncivai.player.PlayerSessionException) {
            error = "The position changed. Review your remaining decisions and try again."
            app.worldScreen?.shouldUpdate = true
        }
    }

    internal fun social(request: JsonObject): JsonObject {
        // Native trade widgets call the same bound, checkpointed player operations
        // as Chat. Their off-turn consent must not acquire native gameplay authority.
        val depth = uiDepth
        uiDepth = 0
        try {
            return host.bindPlayer(viewingCivilization).communication(request)
        } finally {
            uiDepth = depth
        }
    }
    internal val epoch get() = executor.epoch

    override fun usesSharedDiplomacy(civilization: String): Boolean = game.civilizations.any {
        it.civID == civilization && it.isHuman() && it.isMajorCiv() && it.civID != viewingCivilization
    }

    override fun openDiplomacy(civilization: String?): Boolean {
        if (closed) return false
        app.pushScreen { DesktopDiplomacyScreen(this, civilization).also { panel = it } }
        return true
    }

    internal fun openTrade(civilization: String, draft: DesktopTradeDraft? = null) {
        val view = checkNotNull(app.worldScreen).selectedGameView
        tradeDraftToRestore = draft
        try {
            app.pushScreen { DiplomacyScreen(view.civView, view.getForeignCivView(game.getCivilization(civilization)), showTrade = true) }
        } finally {
            tradeDraftToRestore = null
        }
    }

    override fun createTradeTable(viewer: CivView, partner: ForeignCivView, screen: DiplomacyScreen): TradeTable? {
        if (viewer.getCiv().civID != viewingCivilization || !usesSharedDiplomacy(partner.getCiv().civID)) return null
        val controller = DesktopTradeController(this, viewer, partner, screen, tradeDraftToRestore)
        return TradeTable(viewer, partner, screen, controller).also {
            controller.table = it
            tradePanel = controller
        }
    }

    override fun worldScreenCreated(worldScreen: WorldScreen) {
        statusLabel = "".toLabel().apply {
            setWrap(true)
            setSize((worldScreen.stage.width - 600f).coerceAtLeast(200f), 55f)
            setPosition(300f, 8f)
            worldScreen.stage.addActor(this)
        }
        lastAuthority = authority()
        lastWorld = json().toJson(game)
    }

    internal fun message(): String = listOfNotNull(rewindNotice, status()).joinToString("\n")

    private fun status(): String = when {
        closing -> "Stopping the match…"
        executor.mutationFailure != null -> "The game stopped. Inspect the operator logs before resuming."
        error != null -> checkNotNull(error)
        executor.communicationState.restorePending -> "Restoring the interrupted turn…"
        executor.pauseReason == "STRATEGIC_REVIEW" -> "Strategic reviews in progress. Gameplay and communication are paused."
        executor.communicationPaused -> "Gameplay and communication are paused."
        executor.communicationState.trades.activeProposal != null -> negotiationMessage()
        canChangeState(game) -> "Your turn · $viewingCivilization"
        else -> "Waiting for other players… You can inspect your map and use Chat."
    }

    private fun negotiationMessage(): String {
        val pending = executor.communicationState.trades.activeProposal?.let { executor.communicationState.trades.proposals[it]?.lastOrNull() }
        return if (pending != null && viewingCivilization in listOf(pending.author, pending.partner)) {
            "Gameplay paused for your negotiation. Open Chat → Trades to respond."
        } else {
            "Waiting for other players to finish their negotiation."
        }
    }

    internal fun tick() {
        if (closed) return
        if (closing && ::operator.isInitialized && operator.handle(OperatorRequest("status")).status.leaseId == null) {
            Gdx.app.exit()
            return
        }
        if (executor.mutationFailure != null) {
            reload = false
            if (!stoppedScreen) {
                stoppedScreen = true
                app.pushScreen {
                    object : BaseScreen() {}.apply {
                        val table = Table().apply {
                            setFillParent(true)
                            add("The match stopped after an error.\nClose the match, inspect its logs, then resume.".toLabel()).pad(20f).row()
                            add("Stop game".toTextButton().apply { onClick { exit() } }).pad(20f)
                        }
                        stage.addActor(table)
                    }
                }
            }
            return
        }
        if (reload && !loading) {
            reload = false
            loading = true
            val reopen = panel?.takeIf { app.screen === it }
            val trade = tradePanel?.takeIf { app.screen === it.screen && it.table.stage != null }
            val draft = trade?.draft()
            panel = null
            tradePanel = null
            Concurrency.runOnGLThread("Refresh managed game") {
                try {
                    app.loadGame(game)
                    loading = false
                    if (reopen != null) {
                        app.pushScreen { reopen.refreshed().also { panel = it } }
                    } else if (trade != null) {
                        openTrade(trade.partner, draft)
                    }
                } finally {
                    loading = false
                }
            }
        }
        statusLabel?.setText(message())
        if (communicationDirty && !executor.communicationState.restorePending) {
            communicationDirty = false
            val page = social(obj("op" to "read", "epoch" to epoch, "cursor" to latestCursor, "limit" to 100))
            for (event in page.array("events").filterIsInstance<JsonObject>()) {
                val id = event.number("id") ?: continue
                val body = event["body"] as JsonObject
                if (id !in readEvents && body.text("author") != viewingCivilization &&
                    event.text("kind") in setOf("message", "trade", "offer", "consent", "agreement")
                ) {
                    unreadEvents[id] = event
                }
            }
            latestCursor = page.number("nextCursor") ?: latestCursor
            communicationDirty = page.flag("hasMore")
        }
        // Multi-turn autoplay cannot silently take another externally controlled turn.
        app.settings.autoPlay.autoPlayUntilEnd = false
        app.worldScreen?.autoPlay?.let { if (it.shouldContinueAutoPlaying()) it.stopAutoPlay() }
    }

    internal fun markRead(events: List<JsonObject>) {
        for (event in events) {
            val id = event.number("id") ?: continue
            readEvents.add(id)
            unreadEvents.remove(id)
        }
    }
    override val unreadCount get() = unreadEvents.size

    override fun exit() {
        if (closing) return
        closing = true
        Files.createDirectories(runnerDirectory)
        val temporary = Files.createTempFile(runnerDirectory, ".desktop-stop-", ".json")
        Files.writeString(temporary, "{\"action\":\"stop\"}")
        Files.move(temporary, runnerDirectory.resolve("control.json"), StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
        Gdx.graphics.requestRendering()
    }

    override fun close() {
        if (closed) return
        closed = true
        dispatch.close()
        executor.close()
    }
}
