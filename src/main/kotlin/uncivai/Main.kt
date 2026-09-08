package uncivai

import uncivai.mcp.OperatorMcpBinding
import uncivai.mcp.PlayerMcpBinding
import uncivai.mcp.PlayerMcpServer
import uncivai.player.PlayerHost
import java.nio.file.Path
import java.util.concurrent.CountDownLatch
import kotlin.system.exitProcess

fun main(args: Array<String>) {
    val exitCode = runCli(args)
    if (exitCode != 0) exitProcess(exitCode)
}

private fun runCli(args: Array<String>): Int {
    if (args.contentEquals(arrayOf("--help"))) {
        println(usage())
        return 0
    }

    return try {
        val options = parseOptions(args)
        val players = options.mcpConfig?.let(::loadMcpPlayerConfig)
        val operatorToken = options.operatorConfig?.let(::loadOperatorConfig)
        val tls = options.tlsConfig?.let(::loadHostTls)
        val loaded = if (options.newDuel) {
            HeadlessRuntime.createDuel(options.dataDirectory, options.seed ?: System.currentTimeMillis(), options.closeStarts)
        } else if (options.resumeCheckpoint != null) {
            HeadlessRuntime.loadCheckpoint(options.resumeCheckpoint, options.dataDirectory)
        } else {
            HeadlessRuntime.load(checkNotNull(options.saveFile), options.dataDirectory)
        }
        loaded.use { runtime ->
            options.checkpoint?.let { runtime.executor.enableCheckpoints(it) }
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
        0
    } catch (failure: Exception) {
        System.err.println("Error: ${failure.conciseMessage()}")
        System.err.println(usage())
        2
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

private data class CliOptions(
    val saveFile: Path?,
    val dataDirectory: Path,
    val mcpConfig: Path?,
    val port: Int,
    val checkpoint: Path?,
    val resumeCheckpoint: Path?,
    val newDuel: Boolean,
    val closeStarts: Boolean,
    val seed: Long?,
    val operatorConfig: Path?,
    val tlsConfig: Path?,
)

private fun parseOptions(args: Array<String>): CliOptions {
    var saveFile: Path? = null
    var dataDirectory: Path? = null
    var mcpConfig: Path? = null
    var port: Int? = null
    var checkpoint: Path? = null
    var resumeCheckpoint: Path? = null
    var newDuel = false
    var closeStarts = false
    var seed: Long? = null
    var operatorConfig: Path? = null
    var tlsConfig: Path? = null
    var index = 0
    while (index < args.size) {
        val option = args[index]
        if (option == "--close-starts") {
            require(!closeStarts) { "--close-starts may only be specified once" }
            closeStarts = true
            index++
            continue
        }
        if (option == "--new-duel") {
            require(!newDuel) { "--new-duel may only be specified once" }
            newDuel = true
            index++
            continue
        }
        require(option in setOf("--save", "--data-dir", "--mcp-config", "--operator-config", "--tls-config", "--port", "--checkpoint", "--resume-checkpoint", "--seed")) {
            "Unknown argument: $option"
        }
        require(index + 1 < args.size) { "Missing value for $option" }
        val value = args[index + 1]
        when (option) {
            "--tls-config" -> {
                require(tlsConfig == null) { "--tls-config may only be specified once" }
                tlsConfig = Path.of(value)
            }

            "--operator-config" -> {
                require(operatorConfig == null) { "--operator-config may only be specified once" }
                operatorConfig = Path.of(value)
            }

            "--seed" -> {
                require(seed == null) { "--seed may only be specified once" }
                seed = value.toLongOrNull() ?: throw IllegalArgumentException("--seed must be an integer")
            }

            "--save" -> {
                require(saveFile == null) { "--save may only be specified once" }
                saveFile = Path.of(value)
            }

            "--data-dir" -> {
                require(dataDirectory == null) { "--data-dir may only be specified once" }
                dataDirectory = Path.of(value)
            }

            "--mcp-config" -> {
                require(mcpConfig == null) { "--mcp-config may only be specified once" }
                mcpConfig = Path.of(value)
            }

            "--port" -> {
                require(port == null) { "--port may only be specified once" }
                port = value.toIntOrNull()
                require(port != null && port in 0..65535) { "--port must be between 0 and 65535" }
            }

            "--checkpoint" -> {
                require(checkpoint == null) { "--checkpoint may only be specified once" }
                checkpoint = Path.of(value)
            }

            "--resume-checkpoint" -> {
                require(resumeCheckpoint == null) { "--resume-checkpoint may only be specified once" }
                resumeCheckpoint = Path.of(value)
            }
        }
        index += 2
    }

    require(port == null || mcpConfig != null) { "--port requires --mcp-config" }
    require(operatorConfig == null || mcpConfig != null) { "--operator-config requires --mcp-config" }
    require((tlsConfig != null) == (mcpConfig != null)) { "Serving requires both --mcp-config and --tls-config (HTTPS only)" }
    require(listOf(saveFile != null, resumeCheckpoint != null, newDuel).count { it } == 1) {
        "Specify exactly one of --save, --resume-checkpoint or --new-duel"
    }
    require(checkpoint == null || saveFile != null || newDuel) { "--checkpoint requires --save or --new-duel" }
    require(!newDuel || checkpoint != null) { "--new-duel requires --checkpoint to persist the new game" }
    require(seed == null || newDuel) { "--seed requires --new-duel" }
    require(!closeStarts || newDuel) { "--close-starts requires --new-duel" }
    return CliOptions(
        saveFile = saveFile,
        dataDirectory = requireNotNull(dataDirectory) {
            "Missing required --data-dir <absolute-path>"
        },
        mcpConfig = mcpConfig,
        port = port ?: 8765,
        checkpoint = checkpoint,
        resumeCheckpoint = resumeCheckpoint,
        newDuel = newDuel,
        closeStarts = closeStarts,
        seed = seed,
        operatorConfig = operatorConfig,
        tlsConfig = tlsConfig,
    )
}

private fun Throwable.conciseMessage(): String {
    val messages = generateSequence(this) { it.cause }
        .mapNotNull { it.message?.trim()?.takeIf(String::isNotEmpty) }
        .distinct()
        .toList()
    return messages.joinToString(": ").ifEmpty { this::class.java.simpleName }
}

private fun usage() = "Usage: unciv-ai (--new-duel --checkpoint <absolute-path> [--seed <integer>] [--close-starts] | " +
    "--save <absolute-path> [--checkpoint <absolute-path>] | " +
    "--resume-checkpoint <absolute-path>) --data-dir <absolute-path> " +
    "[--mcp-config <absolute-path> --tls-config <absolute-path> [--port <0-65535>] [--operator-config <absolute-path>]]"
