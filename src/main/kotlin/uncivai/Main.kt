package uncivai

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
        HeadlessRuntime.load(options.saveFile, options.dataDirectory).use { runtime ->
            if (players == null) {
                val game = runtime.game
                println(
                    "Loaded game ${game.gameId}: turn ${game.turns}, " +
                        "current player ${game.currentPlayer}, ${game.civilizations.size} civilizations",
                )
            } else {
                servePlayers(runtime, players, options.port)
            }
        }
        0
    } catch (failure: Exception) {
        System.err.println("Error: ${failure.conciseMessage()}")
        System.err.println(usage())
        2
    }
}

private fun servePlayers(runtime: HeadlessRuntime, players: List<McpPlayerGrant>, port: Int) {
    val host = PlayerHost(runtime)
    val bindings = players.map { grant ->
        PlayerMcpBinding(grant.token, host.bindPlayer(grant.civilizationId))
    }
    PlayerMcpServer(bindings, port).use { server ->
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
            println("MCP-READY:http://127.0.0.1:$actualPort/mcp")
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
    val saveFile: Path,
    val dataDirectory: Path,
    val mcpConfig: Path?,
    val port: Int,
)

private fun parseOptions(args: Array<String>): CliOptions {
    var saveFile: Path? = null
    var dataDirectory: Path? = null
    var mcpConfig: Path? = null
    var port: Int? = null
    var index = 0
    while (index < args.size) {
        val option = args[index]
        require(option in setOf("--save", "--data-dir", "--mcp-config", "--port")) {
            "Unknown argument: $option"
        }
        require(index + 1 < args.size) { "Missing value for $option" }
        val value = args[index + 1]
        when (option) {
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
        }
        index += 2
    }

    require(port == null || mcpConfig != null) { "--port requires --mcp-config" }
    return CliOptions(
        saveFile = requireNotNull(saveFile) { "Missing required --save <absolute-path>" },
        dataDirectory = requireNotNull(dataDirectory) {
            "Missing required --data-dir <absolute-path>"
        },
        mcpConfig = mcpConfig,
        port = port ?: 8765,
    )
}

private fun Throwable.conciseMessage(): String {
    val messages = generateSequence(this) { it.cause }
        .mapNotNull { it.message?.trim()?.takeIf(String::isNotEmpty) }
        .distinct()
        .toList()
    return messages.joinToString(": ").ifEmpty { this::class.java.simpleName }
}

private fun usage() = "Usage: unciv-ai --save <absolute-path> --data-dir <absolute-path> " +
    "[--mcp-config <absolute-path> [--port <0-65535>]]"
