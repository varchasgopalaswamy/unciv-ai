package uncivai

import com.badlogic.gdx.Gdx
import com.badlogic.gdx.backends.lwjgl3.Lwjgl3ApplicationConfiguration
import com.badlogic.gdx.backends.lwjgl3.Lwjgl3WindowAdapter
import com.unciv.app.desktop.DesktopGame
import com.unciv.app.desktop.DesktopLauncher
import com.unciv.json.json
import com.unciv.logic.files.SETTINGS_FILE_NAME
import com.unciv.logic.files.UncivFiles
import com.unciv.logic.map.MapSize
import com.unciv.models.metadata.GameSettings
import com.unciv.utils.Concurrency
import uncivai.mcp.OperatorMcpBinding
import uncivai.mcp.PlayerMcpBinding
import uncivai.mcp.PlayerMcpServer
import uncivai.player.PlayerHost
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ExecutionException

/** The production desktop and HTTPS server own the same engine game. */
internal object DesktopHost {
    fun launch(options: CliOptions, onReady: (DesktopSession, Int) -> Unit = { _, _ -> }) {
        System.setProperty("java.awt.headless", "false")
        val grants = loadMcpPlayerConfig(checkNotNull(options.mcpConfig))
        require(grants.filter { it.controller == "HUMAN" }.map { it.civilizationId } == listOf(options.human)) {
            "The desktop requires exactly one HUMAN credential matching --human."
        }
        val directory = options.dataDirectory.toAbsolutePath()
        Files.createDirectories(directory)
        val settingsPath = directory.resolve(SETTINGS_FILE_NAME)
        if (!Files.exists(settingsPath)) {
            val settings = HeadlessRuntime.controlledSettings().apply {
                isFreshlyCreated = false
                windowState = GameSettings.WindowState(width = 1280, height = 800)
                screenSize = GameSettings.ScreenSize.Medium
                enableEasterEggs = false
            }
            Files.writeString(settingsPath, json().toJson(settings))
        }
        DesktopLauncher.launch(arrayOf("--data-dir=$directory")) { config, data ->
            HostedDesktopGame(config, data, options, grants, onReady)
        }
    }
}

private class HostedDesktopGame(
    config: Lwjgl3ApplicationConfiguration,
    directory: String?,
    private val options: CliOptions,
    private val grants: List<McpPlayerGrant>,
    private val onReady: (DesktopSession, Int) -> Unit,
) : DesktopGame(config, directory) {
    private var started = false
    private var controller: DesktopSession? = null
    private var server: PlayerMcpServer? = null
    private var shutdown: Thread? = null

    init {
        config.setWindowListener(object : Lwjgl3WindowAdapter() {
            override fun closeRequested(): Boolean {
                controller?.exit() ?: Gdx.app.exit()
                return false
            }
        })
    }

    override fun render() {
        if (isInitialized && !started) {
            started = true
            startGame()
        }
        controller?.tick()
        val session = controller
        if (session == null) super.render() else session.render { super.render() }
    }

    private fun startGame() {
        val store = options.resumeCheckpoint?.let(::CheckpointStore)
        try {
            val checkpoint = store?.read()
            val human = checkNotNull(options.human)
            val game = if (checkpoint != null) {
                UncivFiles.gameInfoFromString(checkpoint.gameJson)
            } else {
                require(options.newGame) { "The desktop starts new games or resumes managed checkpoints." }
                VanillaGameSetup.create(
                    options.seed ?: System.currentTimeMillis(),
                    options.cityStates ?: 0,
                    options.builtInAI,
                    listOf(human) + grants.filter { it.controller == "AGENT" }.map { it.civilizationId },
                    options.mapRadius?.let(::MapSize) ?: MapSize(options.mapSize ?: "Small"),
                )
            }
            require(!game.gameParameters.isOnlineMultiplayer && game.gameParameters.mods.isEmpty())
            require(game.civilizations.filter { it.isHuman() && it.isMajorCiv() }.map { it.civID }.toSet() == grants.map { it.civilizationId }.toSet())
            gameInfo = game
            val session = DesktopSession(this, game, human, options.dataDirectory.parent.resolve("runner"))
            controller = session
            val executor = session.executor
            if (checkpoint != null) {
                require(checkpoint.communication.humanPlayers == setOf(human)) { "Controller roles differ from the checkpoint." }
                executor.restore(checkpoint, checkNotNull(store))
                session.rewindNotice = "Resuming: the interrupted turn will be restored to its starting position."
            } else {
                executor.communicationState.humanPlayers.add(human)
                options.tradeCounteroffers?.let { executor.communicationState.trades.counterofferLimit = it }
                options.tradeTimeoutSeconds?.let { executor.communicationState.trades.timeoutSeconds = it }
                executor.enableCheckpoints(checkNotNull(options.checkpoint))
            }
            val operator = OperatorControl(executor)
            session.operator = operator
            managedSession = session
            // Load only the executor-owned game, using the fixed human perspective.
            Concurrency.runOnGLThread("Managed desktop startup") {
                loadGame(game)
                val host = PlayerHost(executor)
                val tls = loadHostTls(checkNotNull(options.tlsConfig))
                val hosted = PlayerMcpServer(
                    grants.map { PlayerMcpBinding(it.token, host.bindPlayer(it.civilizationId)) },
                    options.port,
                    OperatorMcpBinding(loadOperatorConfig(checkNotNull(options.operatorConfig)), operator),
                    tls,
                )
                server = hosted
                val actualPort = hosted.start()
                shutdown = Thread({ closeHost() }, "desktop-host-shutdown").also { Runtime.getRuntime().addShutdownHook(it) }
                println("MCP-READY:https://${tls.publicHost}:$actualPort/mcp")
                println("OPERATOR-READY:https://${tls.publicHost}:$actualPort/operator")
                println("DESKTOP-READY:$human")
                onReady(session, actualPort)
            }
        } catch (failure: Throwable) {
            store?.close()
            System.err.println("Desktop startup failed: ${failure.message}")
            throw failure
        }
    }

    private fun closeHost() {
        controller?.close()
        server?.close()
    }

    override fun dispose() {
        closeHost()
        shutdown?.let { runCatching { Runtime.getRuntime().removeShutdownHook(it) } }
        managedSession = null
        super.dispose()
    }
}

/** Never wait for the GL thread while holding the executor's monitor. */
internal class DesktopDispatch :
    GameDispatch,
    AutoCloseable {
    private val owner = Thread.currentThread()
    private val pending = ConcurrentHashMap.newKeySet<CompletableFuture<*>>()

    @Volatile private var closed = false

    override fun <T> call(block: () -> T): T {
        if (closed) throw RuntimeClosedException()
        if (Thread.currentThread() === owner) return block()
        val result = CompletableFuture<T>()
        pending.add(result)
        if (closed) {
            result.completeExceptionally(RuntimeClosedException())
        } else {
            Gdx.app.postRunnable {
                if (!result.isDone) {
                    try {
                        if (closed) throw RuntimeClosedException()
                        result.complete(block())
                    } catch (failure: Throwable) {
                        result.completeExceptionally(failure)
                    } finally {
                        pending.remove(result)
                    }
                }
            }
        }
        return try {
            result.get()
        } catch (failure: ExecutionException) {
            throw checkNotNull(failure.cause)
        }
    }

    override fun close() {
        closed = true
        pending.forEach { it.completeExceptionally(RuntimeClosedException()) }
        pending.clear()
    }
}
