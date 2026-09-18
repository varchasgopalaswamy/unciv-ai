package uncivai

import com.github.ajalt.clikt.core.CliktCommand
import com.github.ajalt.clikt.core.UsageError
import com.github.ajalt.clikt.parameters.options.default
import com.github.ajalt.clikt.parameters.options.flag
import com.github.ajalt.clikt.parameters.options.option
import com.github.ajalt.clikt.parameters.options.required
import com.github.ajalt.clikt.parameters.types.choice
import com.github.ajalt.clikt.parameters.types.int
import com.github.ajalt.clikt.parameters.types.long
import com.github.ajalt.clikt.parameters.types.path
import com.github.ajalt.clikt.parameters.types.restrictTo
import java.nio.file.Path

internal data class CliOptions(
    val saveFile: Path?,
    val dataDirectory: Path,
    val mcpConfig: Path?,
    val port: Int,
    val checkpoint: Path?,
    val resumeCheckpoint: Path?,
    val newGame: Boolean,
    val scenario: String?,
    val seed: Long?,
    val operatorConfig: Path?,
    val tlsConfig: Path?,
    val tradeCounteroffers: Int?,
    val tradeTimeoutSeconds: Int?,
)

/** Parsing and help belong to Clikt; cross-option host requirements stay here. */
internal class HostCli(private val launch: (CliOptions) -> Unit) : CliktCommand(name = "unciv-ai") {
    private val save by option("--save", help = "Load an Unciv save.").path(mustExist = true, canBeDir = false)
    private val data by option("--data-dir", help = "Host runtime data directory.").path(canBeFile = false).required()
    private val players by option("--mcp-config", help = "Private player credential file.").path(mustExist = true, canBeDir = false)
    private val operator by option("--operator-config", help = "Private operator credential file.").path(mustExist = true, canBeDir = false)
    private val tls by option("--tls-config", help = "HTTPS certificate configuration.").path(mustExist = true, canBeDir = false)
    private val port by option("--port", help = "HTTPS port; 0 selects an available port (default: 8765).").int().restrictTo(0, 65535)
    private val checkpoint by option("--checkpoint", help = "Persist a new game or imported save here.").path(canBeDir = false)
    private val resume by option("--resume-checkpoint", help = "Restore a host checkpoint.").path(mustExist = true, canBeDir = false)
    private val newGame by option("--new-game", help = "Create a Quick Vanilla Rome/Greece/France game; --scenario selects a prepared exercise.").flag()
    private val seed by option("--seed", help = "New-game random seed.").long()
    private val scenario by option("--scenario", help = "Create a prepared exercise or verify its identity on resume.").choice("trade-cycle", "war-paid", "war-joint", "agreement-lifecycle")
    private val counteroffers by option("--trade-counteroffers", help = "Counteroffer limit per negotiation.").int().restrictTo(0, 100)
    private val timeout by option("--trade-timeout", help = "Negotiation deadline in seconds.").int().restrictTo(1, 86400)

    override fun run() {
        fun requireOption(condition: Boolean, message: String) {
            if (!condition) throw UsageError(message)
        }
        requireOption(listOf(save != null, resume != null, newGame).count { it } == 1, "Specify exactly one of --save, --resume-checkpoint or --new-game")
        requireOption(checkpoint == null || save != null || newGame, "--checkpoint requires --save or --new-game")
        requireOption(!newGame || checkpoint != null, "--new-game requires --checkpoint to persist the new game")
        requireOption(seed == null || newGame, "--seed requires --new-game")
        requireOption(port == null || players != null, "--port requires --mcp-config")
        requireOption(operator == null || players != null, "--operator-config requires --mcp-config")
        requireOption((tls != null) == (players != null), "Serving requires both --mcp-config and --tls-config (HTTPS only)")
        requireOption(scenario != "trade-cycle" || counteroffers == null || counteroffers == 3, "The trade-cycle exercise requires --trade-counteroffers 3")
        requireOption(scenario != "war-paid" || counteroffers?.let { it > 0 } != false, "The paid-war exercise requires at least one counteroffer")
        launch(CliOptions(save, data, players, port ?: 8765, checkpoint, resume, newGame, scenario, seed, operator, tls, counteroffers, timeout))
    }
}
