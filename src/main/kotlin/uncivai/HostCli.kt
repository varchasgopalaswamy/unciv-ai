package uncivai

import com.github.ajalt.clikt.core.CliktCommand
import com.github.ajalt.clikt.core.UsageError
import com.github.ajalt.clikt.parameters.options.default
import com.github.ajalt.clikt.parameters.options.flag
import com.github.ajalt.clikt.parameters.options.multiple
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
    val cityStates: Int? = null,
    val builtInAI: List<String> = emptyList(),
    val agentPlayers: List<String> = emptyList(),
    val mapSize: String? = null,
    val mapRadius: Int? = null,
    val human: String? = null,
    val vanilla: VanillaOptions = VanillaOptions(),
    val validateSetup: Boolean = false,
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
    private val seed by option("--seed", help = "New-game map terrain seed.").long()
    private val startingPositionSeed by option("--starting-position-seed", help = "New-game starting-position and resource-placement seed, independent of terrain.").long()
    private val cityStates by option("--city-states", help = "Number of city-states in a new ordinary Vanilla game (default: 0).").int().restrictTo(0, 41)
    private val builtInAI by option("--built-in-ai", help = "Add a built-in AI major civilization to a new ordinary game; repeat for multiple opponents.").multiple()
    private val agentPlayers by option("--agent-player", help = "Externally controlled civilization; repeat to replace the default Rome/Greece/France roster.").multiple()
    private val mapSize by option("--map-size", help = "New ordinary game map size (default: Small).").choice("Tiny", "Small", "Medium", "Large", "Huge")
    private val mapRadius by option("--map-radius", help = "Custom hexagonal map radius, instead of --map-size.").int().restrictTo(2, 500)
    private val scenario by option("--scenario", help = "Create a prepared exercise or verify its identity on resume.").choice("economy-manager", "military-manager", "trade-cycle", "war-paid", "war-joint", "agreement-lifecycle", "mixed-diplomacy")
    private val counteroffers by option("--trade-counteroffers", help = "Counteroffer limit per negotiation.").int().restrictTo(0, 100)
    private val timeout by option("--trade-timeout", help = "Negotiation deadline in seconds.").int().restrictTo(1, 86400)
    private val human by option("--human", help = "Open the local desktop for this human-controlled civilization.")
    private val mapType by option("--map-type", help = "Native generated map type.").default("Pangaea")
    private val difficulty by option("--difficulty", help = "Native Vanilla difficulty.").default("Prince")
    private val speed by option("--speed", help = "Native game speed.").default("Quick")
    private val startingEra by option("--starting-era", help = "Native starting era.").default("Ancient era")
    private val victories by option("--victories", help = "Comma-separated enabled victory names; omitted enables all.")
    private val validateSetup by option("--validate-setup", help = "Validate native setup and print its turn limit without starting services.").flag()

    override fun run() {
        fun requireOption(condition: Boolean, message: String) {
            if (!condition) throw UsageError(message)
        }
        requireOption(listOf(save != null, resume != null, newGame, validateSetup).count { it } == 1, "Specify exactly one of --save, --resume-checkpoint, --new-game or --validate-setup")
        requireOption(checkpoint == null || save != null || newGame, "--checkpoint requires --save or --new-game")
        requireOption(!newGame || checkpoint != null, "--new-game requires --checkpoint to persist the new game")
        requireOption(cityStates == null || ((newGame || validateSetup) && scenario == null), "--city-states requires an ordinary --new-game")
        requireOption(builtInAI.isEmpty() || ((newGame || validateSetup) && scenario == null), "--built-in-ai requires an ordinary --new-game")
        requireOption(agentPlayers.isEmpty() || ((newGame || validateSetup) && scenario == null), "--agent-player requires an ordinary --new-game")
        requireOption((mapSize == null && mapRadius == null) || ((newGame || validateSetup) && scenario == null), "Map options require an ordinary --new-game")
        requireOption(mapSize == null || mapRadius == null, "Choose --map-size or --map-radius")
        requireOption(seed == null || newGame || validateSetup, "--seed requires --new-game")
        requireOption(port == null || players != null, "--port requires --mcp-config")
        requireOption(operator == null || players != null, "--operator-config requires --mcp-config")
        requireOption((tls != null) == (players != null), "Serving requires both --mcp-config and --tls-config (HTTPS only)")
        requireOption(scenario != "trade-cycle" || counteroffers == null || counteroffers == 3, "The trade-cycle exercise requires --trade-counteroffers 3")
        requireOption(scenario != "war-paid" || counteroffers?.let { it > 0 } != false, "The paid-war exercise requires at least one counteroffer")
        requireOption(human == null || (players != null && operator != null && scenario == null), "--human requires player/operator credentials and an ordinary game")
        val vanilla = VanillaOptions(mapType, difficulty, speed, startingEra, victories?.split(',')?.map(String::trim), startingPositionSeed)
        requireOption(vanilla == VanillaOptions() || ((newGame || validateSetup) && scenario == null), "Native setup options require a new ordinary game")
        requireOption(!validateSetup || (players == null && human == null && (scenario == null || scenario in ManagerScenarioSetup.variants)), "Setup validation cannot start services or this scenario")
        launch(CliOptions(save, data, players, port ?: 8765, checkpoint, resume, newGame, scenario, seed, operator, tls, counteroffers, timeout, cityStates, builtInAI, agentPlayers, mapSize, mapRadius, human, vanilla, validateSetup))
    }
}
