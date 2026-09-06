package uncivai

import java.nio.file.Path
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
        HeadlessRuntime.load(options.saveFile, options.dataDirectory).use { runtime ->
            val game = runtime.game
            println(
                "Loaded game ${game.gameId}: turn ${game.turns}, " +
                    "current player ${game.currentPlayer}, ${game.civilizations.size} civilizations",
            )
        }
        0
    } catch (failure: Exception) {
        System.err.println("Error: ${failure.conciseMessage()}")
        System.err.println(usage())
        2
    }
}

private data class CliOptions(val saveFile: Path, val dataDirectory: Path)

private fun parseOptions(args: Array<String>): CliOptions {
    var saveFile: Path? = null
    var dataDirectory: Path? = null
    var index = 0
    while (index < args.size) {
        val option = args[index]
        require(option == "--save" || option == "--data-dir") { "Unknown argument: $option" }
        require(index + 1 < args.size) { "Missing value for $option" }
        val value = Path.of(args[index + 1])
        when (option) {
            "--save" -> {
                require(saveFile == null) { "--save may only be specified once" }
                saveFile = value
            }
            "--data-dir" -> {
                require(dataDirectory == null) { "--data-dir may only be specified once" }
                dataDirectory = value
            }
        }
        index += 2
    }

    return CliOptions(
        saveFile = requireNotNull(saveFile) { "Missing required --save <absolute-path>" },
        dataDirectory = requireNotNull(dataDirectory) {
            "Missing required --data-dir <absolute-path>"
        },
    )
}

private fun Throwable.conciseMessage(): String {
    val messages = generateSequence(this) { it.cause }
        .mapNotNull { it.message?.trim()?.takeIf(String::isNotEmpty) }
        .distinct()
        .toList()
    return messages.joinToString(": ").ifEmpty { this::class.java.simpleName }
}

private fun usage() = "Usage: unciv-ai --save <absolute-path> --data-dir <absolute-path>"
