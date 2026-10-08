package uncivai

import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.files.UncivFiles
import com.unciv.models.ruleset.unique.GameContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import java.nio.file.Files
import java.nio.file.Path
import java.security.MessageDigest

/** Trusted fixture preparation. Neither raw saves nor evaluator criteria are player inputs. */
object StrategyPositionFixtures {
    private val json = Json { prettyPrint = true }

    @JvmStatic fun main(args: Array<String>) {
        val (mode, source, destination) = args
        val root = Path.of(destination)
        require(root.isAbsolute)
        if (mode == "observe") {
            require(!Files.exists(root)) { "Use a fresh output directory: $root" }
            Files.createDirectories(root)
            val checkpoint = root.resolve("exploration.json")
            Files.copy(Path.of(source), checkpoint)
            HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("runtime")).use { runtime ->
                val game = runtime.game
                val before = com.unciv.json.json().toJson(game)
                val owners = game.tileMap.values.associateWith { it.getCity() }
                check(owners.values.filterNotNull().all { owner -> owner.civ.cities.any { it === owner } }) {
                    "Loaded checkpoint contains detached tile owners"
                }
                val civilization = args.getOrNull(3)?.takeIf { it.isNotEmpty() } ?: game.currentPlayer
                val turn = PlayerHost(runtime).bindPlayer(civilization).turn()
                check(owners.all { (tile, owner) -> tile.getCity() === owner }) { "Observation changed tile ownership references" }
                check(com.unciv.json.json().toJson(game) == before) { "Observation changed native state" }
                Files.writeString(root.resolve("initial-turn.json"), PlayerTurnJson.json.encodeToString(turn))
            }
            println("STRATEGY_OBSERVATION_READY $root")
            return
        }
        val manifestPath = Path.of(source).resolve("manifest.json")
        val manifest = json.parseToJsonElement(Files.readString(manifestPath)).jsonObject
        for (entry in manifest.getValue("positions").jsonArray) {
            val spec = entry.jsonObject
            val id = spec.getValue("id").jsonPrimitive.content
            val civilization = spec.getValue("civilization").jsonPrimitive.content
            val save = Path.of(source).resolve("$id.json")
            if (mode == "prepare") {
                require(sha256(Files.readString(save)) == spec.getValue("saveSha256").jsonPrimitive.content) {
                    "Saved position checksum mismatch: $id"
                }
            }
            val output = root.resolve(id)
            require(!Files.exists(output)) { "Use a fresh output directory: $output" }
            Files.createDirectories(output)
            HeadlessRuntime.load(save, output.resolve("runtime")).use { runtime ->
                val game = runtime.game
                if (mode == "capture") {
                    val originalPlayer = game.currentPlayer
                    // Leave the currently started human turn intact while advancing.
                    // Other non-subject majors become native AI. The current player
                    // takes no fixture-authored actions; normal nextTurn ends it.
                    for (civ in game.civilizations.filter { it.isMajorCiv() }) {
                        val type = if (civ.civID in setOf(civilization, originalPlayer)) PlayerType.Human else PlayerType.AI
                        civ.playerType = type
                        game.gameParameters.players.single { it.chosenCiv == civ.civName }.playerType = type
                    }
                    if (originalPlayer != civilization) game.nextTurn()
                    check(game.currentPlayer == civilization)
                    for (civ in game.civilizations.filter { it.isMajorCiv() && it.civID != civilization }) {
                        civ.playerType = PlayerType.AI
                        game.gameParameters.players.single { it.chosenCiv == civ.civName }.playerType = PlayerType.AI
                    }
                } else {
                    require(mode == "prepare")
                }
                check(game.currentPlayer == civilization)
                check(game.civilizations.filter { it.isHuman() }.map { it.civID } == listOf(civilization))
                val player = game.getCivilization(civilization)
                val turn = PlayerHost(runtime).bindPlayer(civilization).turn()
                check(turn.map.units.isNotEmpty() && turn.cities.isNotEmpty())
                val alternatives = if (id == "idle-workers") {
                    val workers = player.units.getCivUnits().filter { it.name == "Worker" }.toList()
                    check(workers.isNotEmpty())
                    game.tileMap.values.count { tile ->
                        tile.isVisible(player) && tile.getOwner() == player && tile.improvement == null &&
                            workers.any { worker ->
                                worker.currentTile != tile && worker.movement.canReach(tile) &&
                                    listOf("Farm", "Mine", "Camp", "Pasture").any { name ->
                                        tile.improvementFunctions.canBuildImprovement(
                                            game.ruleset.tileImprovements.getValue(name),
                                            GameContext(civInfo = player, unit = worker, tile = tile),
                                        )
                                    }
                            }
                    }.also { check(it > 0) { "Worker fixture has no reachable, visible, native-legal new improvements" } }
                } else {
                    null
                }
                when (id) {
                    "exploration-loop" -> check(turn.map.frontier.isNotEmpty() && turn.map.units.any { it.name == "Scout" && it.moves.isNotEmpty() })

                    "slow-economy" -> check(turn.cities.any { it.name == "Orleans" && "Library" in it.constructionQueue })

                    "civilian-route" -> check(turn.map.units.count { it.name == "Settler" } == 2)

                    "injured-military" -> check(turn.map.units.any { it.military && it.health < 50 && it.nearbyHostiles.isNotEmpty() && it.moves.isNotEmpty() })

                    "scout-survival" -> check(
                        turn.map.units.any {
                            it.name == "Scout" && it.health == 24 && it.moves.isNotEmpty() &&
                                it.combat!!.attacks.any { attack -> attack.consequences!!.attacker.destructionPossible }
                        },
                    ) { "Scout must have both a move and a potentially lethal attack" }

                    "settler-escort" -> check(turn.map.units.any { it.name == "Settler" && it.moves.isNotEmpty() } && turn.map.units.any { it.military })

                    "civilian-exposure" -> check(turn.map.units.any { it.name == "Settler" } && turn.map.units.count { it.name == "Worker" } == 2 && turn.map.units.any { it.nearbyHostiles.isNotEmpty() })

                    "stalled-city" -> check(turn.cities.any { it.name == "Antium" && it.development!!.population == 1 && it.development.foodPerTurn == 0 })
                }
                Files.writeString(output.resolve("initial-turn.json"), PlayerTurnJson.json.encodeToString(turn))
                val savedGame = UncivFiles.gameInfoToString(game, forceZip = false)
                Files.writeString(output.resolve("game.json"), savedGame)
                Files.writeString(
                    output.resolve("preflight.json"),
                    json.encodeToString(
                        buildJsonObject {
                            put("id", id)
                            put("civilization", civilization)
                            put("turn", game.turns)
                            put("saveSha256", sha256(savedGame))
                            put("exploredTiles", turn.map.tiles.size)
                            put("frontierTiles", turn.map.frontier.size)
                            if (alternatives != null) put("reachableVisibleUnimprovedTilesWithLegalJobs", alternatives)
                        },
                    ),
                )
                // Advancing the raw fixture can leave observation callbacks for former
                // human opponents. They are now native AI and have no player session
                // to restore; their stale alert handles must not enter the checkpoint.
                game.civilizations.filter { it.civID != civilization }.forEach {
                    it.cache.onTileRevealed = null
                    it.cache.onViewableTilesUpdated = null
                }
                runtime.executor.turnSessions.keys.retainAll(setOf(civilization))
                runtime.executor.enableCheckpoints(output.resolve("exploration.json"))
            }
            // Reload the exact artifact used by live services; recompute visibility.
            HeadlessRuntime.loadCheckpoint(output.resolve("exploration.json"), output.resolve("reload")).use { runtime ->
                val turn = PlayerHost(runtime).bindPlayer(civilization).turn()
                check(turn.phase == "ACTIVE" && turn.map.units.isNotEmpty())
            }
            println("STRATEGY_POSITION_READY $id")
        }
    }

    // Native serialization has no final newline; checked-in JSON may have one.
    private fun sha256(text: String) = MessageDigest.getInstance("SHA-256").digest(text.trimEnd().toByteArray()).joinToString("") { "%02x".format(it) }
}
