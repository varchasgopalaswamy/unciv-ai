package uncivai

import com.github.ajalt.clikt.core.main
import uncivai.mcp.OperatorMcpBinding
import uncivai.mcp.PlayerMcpBinding
import uncivai.mcp.PlayerMcpServer
import uncivai.player.PlayerHost
import java.util.concurrent.CountDownLatch

fun main(args: Array<String>) = HostCli(::launchHost).main(args)

private fun launchHost(options: CliOptions) {
    try {
        val players = options.mcpConfig?.let(::loadMcpPlayerConfig)
        val operatorToken = options.operatorConfig?.let(::loadOperatorConfig)
        val tls = options.tlsConfig?.let(::loadHostTls)
        val expectedMap = options.scenario?.let { if (it == "trade-cycle") TradeCycleScenarioSetup.MAP_NAME else WarScenarioSetup.mapName(it) }
        val loaded = if (options.newGame) {
            if (options.scenario == "trade-cycle") {
                HeadlessRuntime.createTradeCycleExercise(options.dataDirectory, options.seed ?: 412L)
            } else if (options.scenario in setOf("war-paid", "war-joint")) {
                HeadlessRuntime.createWarExercise(options.dataDirectory, checkNotNull(options.scenario), options.seed ?: 413L)
            } else {
                HeadlessRuntime.createVanillaGame(options.dataDirectory, options.seed ?: System.currentTimeMillis())
            }
        } else if (options.resumeCheckpoint != null) {
            HeadlessRuntime.loadCheckpoint(options.resumeCheckpoint, options.dataDirectory, expectedMap)
        } else {
            HeadlessRuntime.load(checkNotNull(options.saveFile), options.dataDirectory)
        }
        loaded.use { runtime ->
            require(expectedMap == null || runtime.game.tileMap.mapParameters.name == expectedMap) {
                "Checkpoint is not the ${options.scenario} scenario. Choose a fresh --state-dir."
            }
            runtime.executor.accessGame {
                options.tradeCounteroffers?.let { runtime.executor.communicationState.trades.counterofferLimit = it }
                options.tradeTimeoutSeconds?.let { runtime.executor.communicationState.trades.timeoutSeconds = it }
                runtime.executor.commitCheckpoint()
            }
            options.checkpoint?.let { runtime.executor.enableCheckpoints(it) }
            println("Scenario: ${runtime.game.tileMap.mapParameters.name.ifEmpty { "Ordinary game" }}")
            println("Rules: ${runtime.game.gameParameters}; map seed: ${runtime.game.tileMap.mapParameters.seed}")
            if (players == null) {
                val game = runtime.game
                println(
                    "Loaded game ${game.gameId}: turn ${game.turns}, " +
                        "current player ${game.currentPlayer}, ${game.civilizations.size} civilizations",
                )
            } else {
                servePlayers(runtime, players, options.port, operatorToken, checkNotNull(tls))
            }
        }
    } catch (failure: Exception) {
        throw com.github.ajalt.clikt.core.CliktError(failure.conciseMessage())
    }
}

private fun servePlayers(runtime: HeadlessRuntime, players: List<McpPlayerGrant>, port: Int, operatorToken: String?, tls: HostTls) {
    val host = PlayerHost(runtime)
    val bindings = players.map { grant ->
        PlayerMcpBinding(grant.token, host.bindPlayer(grant.civilizationId))
    }
    val operator = operatorToken?.let { OperatorMcpBinding(it, OperatorControl(runtime.executor)) }
    PlayerMcpServer(bindings, port, operator, tls).use { server ->
        val stopped = CountDownLatch(1)
        val shutdown = Thread({
            try {
                server.close()
            } finally {
                try {
                    runtime.close()
                } finally {
                    stopped.countDown()
                }
            }
        }, "mcp-shutdown")
        val jvm = Runtime.getRuntime()
        jvm.addShutdownHook(shutdown)
        try {
            val actualPort = server.start()
            println("MCP-READY:https://${tls.publicHost}:$actualPort/mcp")
            if (operator != null) println("OPERATOR-READY:https://${tls.publicHost}:$actualPort/operator")
            stopped.await()
        } finally {
            try {
                jvm.removeShutdownHook(shutdown)
            } catch (_: IllegalStateException) {
                // Shutdown is already running; the hook owns runtime cleanup.
            }
        }
    }
}

private fun Throwable.conciseMessage(): String {
    val messages = generateSequence(this) { it.cause }
        .mapNotNull { it.message?.trim()?.takeIf(String::isNotEmpty) }
        .distinct()
        .toList()
    return messages.joinToString(": ").ifEmpty { this::class.java.simpleName }
}
