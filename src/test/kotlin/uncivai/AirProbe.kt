package uncivai

import com.unciv.json.json
import com.unciv.logic.map.HexCoord
import uncivai.player.MissionPreview
import uncivai.player.MissionPreviewRequest
import uncivai.player.OwnedUnit
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import uncivai.player.UnitMission
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

object AirProbe {
    @JvmStatic fun main(args: Array<String>) {
        try {
            val root = Path.of(args[0])
            when (args[1]) {
                "missions" -> missions(root)
                "nuclear" -> nuclear(root)
                "privacy" -> privacy(root)
                "recovery" -> recovery(root)
                else -> error("Unknown scenario")
            }
            println("AIR_${args[1]}_PASS")
        } catch (failure: Throwable) {
            failure.printStackTrace()
            kotlin.system.exitProcess(1)
        }
    }

    private fun HexCoord.position() = TilePosition(x, y)
    private fun request(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection).reviewedFor(turn)
    private fun unitByLabel(runtime: HeadlessRuntime, turn: PlayerTurnSnapshot, label: String): OwnedUnit {
        val native = runtime.game.getCivilization(runtime.game.currentPlayer).units.getCivUnits().single { it.instanceName == label }
        return turn.map.units.single { it.displayName == native.displayName() }
    }
    private fun preview(api: GameplayTestApi, token: String, target: String, mission: UnitMission, destination: HexCoord): MissionPreview = PlayerTurnJson.json.decodeFromString(api.tool(token, "mission_preview", obj("target" to target, "mission" to mission.name, "destination" to obj("x" to destination.x, "y" to destination.y))).toString())

    private fun missions(root: Path) {
        val checkpoint = root.resolve("checkpoint.json")
        lateinit var rebase: TurnCommandRequest
        lateinit var rebased: TurnCommandReceipt
        lateinit var carrierWing: String
        AirFixtures.create(root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls"), AirFixtures.tokens).use { api ->
                val token = api.playerToken("Rome")
                var turn = api.turn(token)
                carrierWing = unitByLabel(runtime, turn, "Carrier wing").handle
                rebase = request(turn, TurnCommandKind.REBASE_UNIT, carrierWing).copy(destination = AirFixtures.carrierStart.position())
                api.reject(token, rebase, "OBSERVATION_REQUIRED")
                val native = json().toJson(runtime.game)
                check(preview(api, token, carrierWing, UnitMission.REBASE, AirFixtures.carrierStart).available)
                check(native == json().toJson(runtime.game))
                api.reject(token, rebase.copy(destination = AirFixtures.carrierEnd.position()), "OBSERVATION_REQUIRED")
                rebased = api.execute(token, rebase)
                check(rebased.mission!!.transported == true && rebased.mission!!.end == AirFixtures.carrierStart.position())
                check(rebased.mission!!.movementAfter!!.startsWith("0/"))
                check(api.execute(token, rebase) == rebased)
                turn = api.turn(token)
                val carrier = turn.map.units.single { it.name == "Carrier" }
                check(carrier.air!!.carriedUnits.contains(carrierWing))
                check(carrier.moves.any { it.destination == AirFixtures.carrierEnd.position() })
                api.execute(token, request(turn, TurnCommandKind.MOVE_UNIT, carrier.handle).copy(destination = AirFixtures.carrierEnd.position()))
                turn = api.turn(token)
                check(turn.map.units.single { it.handle == carrierWing }.position == AirFixtures.carrierEnd.position())
                val reserve = unitByLabel(runtime, turn, "Reserve wing")
                val price = reserve.economy!!.upgrades.single { it.targetName == "Jet Fighter" }.goldCost!!
                val upgraded = api.execute(token, request(turn, TurnCommandKind.UPGRADE_UNIT, reserve.handle, "Jet Fighter").copy(confirmAction = true))
                check(upgraded.economy!!.goldChange == -price)
                turn = api.turn(token)
                val retired = unitByLabel(runtime, turn, "Retired bomber")
                val disbanded = api.execute(token, request(turn, TurnCommandKind.DISBAND_UNIT, retired.handle).copy(confirmAction = true))
                check(disbanded.economy!!.goldChange > 0)
                turn = api.turn(token)
                api.execute(token, request(turn, TurnCommandKind.DECLARE_WAR, "Greece").copy(confirmWar = true))
                turn = api.readAlerts(token)
                val bomber = unitByLabel(runtime, turn, "Strike wing")
                val strike = bomber.combat!!.attacks.single { it.destination == AirFixtures.strikeTarget.position() }
                check(strike.interceptionWarning != null)
                val struck = api.execute(token, request(turn, TurnCommandKind.ATTACK, bomber.handle).copy(destination = strike.destination, attackFrom = strike.attackFrom))
                check(struck.combat!!.damageToAttacker > 0 && struck.combat!!.damageToDefender > 0)
                turn = api.turn(token)
                val fighter = unitByLabel(runtime, turn, "Sweep wing")
                check(preview(api, token, fighter.handle, UnitMission.AIR_SWEEP, AirFixtures.strikeTarget).available)
                val swept = api.execute(token, request(turn, TurnCommandKind.AIR_SWEEP, fighter.handle).copy(destination = AirFixtures.strikeTarget.position()))
                check(swept.mission!!.reports.any { it.kind == "ObservedInterception" })
                turn = api.turn(token)
                val missile = unitByLabel(runtime, turn, "Guided strike")
                val offer = missile.combat!!.attacks.single { it.destination == AirFixtures.missileTarget.position() }
                check(offer.consequences!!.attackerConsumedOnAttack)
                val fired = api.execute(token, request(turn, TurnCommandKind.ATTACK, missile.handle).copy(destination = offer.destination, attackFrom = offer.attackFrom))
                check(fired.combat!!.attackerDestroyed && fired.combat!!.damageToDefender > 0)
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            GameplayTestApi(runtime, root.resolve("tls2"), AirFixtures.tokens).use { api ->
                val token = api.playerToken("Rome")
                check(api.execute(token, rebase) == rebased)
                val turn = api.turn(token)
                val wing = turn.map.units.single { it.handle == carrierWing }
                check(wing.air!!.transported && wing.position == AirFixtures.carrierEnd.position())
                api.finishTurn(token, heartbeat = false)
                api.finishTurn(api.playerToken("Greece"), heartbeat = false)
                val frenchToken = api.playerToken("France")
                val french = api.turn(frenchToken)
                val dropper = french.map.units.single { it.name == "Paratrooper" }
                check(preview(api, frenchToken, dropper.handle, UnitMission.PARADROP, AirFixtures.dropTarget).available)
                val dropped = api.execute(frenchToken, request(french, TurnCommandKind.PARADROP, dropper.handle).copy(destination = AirFixtures.dropTarget.position()))
                check(dropped.mission!!.end == AirFixtures.dropTarget.position())
            }
        }
    }

    private fun nuclear(root: Path) {
        val checkpoint = root.resolve("checkpoint.json")
        lateinit var command: TurnCommandRequest
        lateinit var receipt: TurnCommandReceipt
        AirFixtures.create(root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls"), AirFixtures.tokens).use { api ->
                api.finishTurn(api.playerToken("Rome"), heartbeat = false)
                api.finishTurn(api.playerToken("Greece"), heartbeat = false)
                val token = api.playerToken("France")
                val turn = api.turn(token)
                val nuke = turn.map.units.single { it.name == "Nuclear Missile" }
                val offer = preview(api, token, nuke.handle, UnitMission.NUCLEAR_STRIKE, AirFixtures.nuclearTarget)
                check(offer.available && offer.confirmation != null && "Greece" in offer.knownWarDeclarations)
                command = request(turn, TurnCommandKind.NUCLEAR_STRIKE, nuke.handle).copy(destination = AirFixtures.nuclearTarget.position())
                val native = json().toJson(runtime.game)
                api.reject(token, command, "INVALID_ARGUMENT")
                check(native == json().toJson(runtime.game))
                command = command.copy(confirmAction = true)
                receipt = api.execute(token, command)
                check(receipt.mission!!.destroyed && "Greece" in receipt.mission!!.newWars)
                check(receipt.mission!!.resolution == "Completed")
                check(runtime.game.getCivilization("France").isAtWarWith(runtime.game.getCivilization("Greece")))
                check(runtime.game.tileMap[AirFixtures.nuclearTarget].militaryUnit == null)
                check(api.execute(token, command) == receipt)
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("France")
            check(session.execute(command) == receipt)
            check(PlayerHost(runtime).bindPlayer("Greece").turn().combatReports.any { it.kind == "ObservedNuclearDetonation" })
        }
    }

    private fun privacy(root: Path) = AirFixtures.create(root.resolve("initial")).use { runtime ->
        val rome = runtime.game.getCivilization("Rome")
        rome.viewableTiles = setOf(rome.getCapital()!!.getCenterTile())
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val turn = session.turn()
        val fighter = unitByLabel(runtime, turn, "Sweep wing")
        val query = MissionPreviewRequest(fighter.handle, UnitMission.AIR_SWEEP, AirFixtures.strikeTarget.position())
        val before = session.missionPreview(query)
        for (interceptor in runtime.game.getCivilization("Greece").units.getCivUnits().filter { it.name == "Fighter" }) {
            interceptor.health = 1
            interceptor.currentMovement = 0f
        }
        check(before == session.missionPreview(query))
        check(before.visibleVictims.isEmpty())
        val france = runtime.game.getCivilization("France")
        runtime.game.currentPlayer = france.civID
        runtime.game.currentPlayerCiv = france
        france.viewableTiles = setOf(france.getCapital()!!.getCenterTile())
        val french = PlayerHost(runtime).bindPlayer("France")
        val frenchTurn = french.turn()
        val nuke = frenchTurn.map.units.single { it.name == "Nuclear Missile" }
        val nuclear = MissionPreviewRequest(nuke.handle, UnitMission.NUCLEAR_STRIKE, AirFixtures.nuclearTarget.position())
        val prior = french.missionPreview(nuclear)
        val hidden = runtime.game.tileMap[AirFixtures.nuclearTarget].militaryUnit!!
        hidden.health = 17
        hidden.instanceName = "Private target name"
        check(prior == french.missionPreview(nuclear))
        check(prior.visibleVictims.isEmpty())
        val foreign = nuclear.copy(target = "unowned")
        val rejected = runCatching { french.missionPreview(foreign) }.exceptionOrNull() as PlayerSessionException
        check(rejected.reason == PlayerSessionFailure.UNIT_UNAVAILABLE)
    }

    private fun recovery(root: Path) {
        for (mission in UnitMission.entries) {
            for (afterReplace in listOf(false, true)) {
                val dir = root.resolve("$mission-$afterReplace")
                val checkpoint = dir.resolve("checkpoint.json")
                var failCommit = false
                lateinit var command: TurnCommandRequest
                val player = if (mission in setOf(UnitMission.PARADROP, UnitMission.NUCLEAR_STRIKE)) "France" else "Rome"
                val (label, destination) = when (mission) {
                    UnitMission.REBASE -> "Carrier wing" to AirFixtures.carrierStart
                    UnitMission.PARADROP -> "Drop team" to AirFixtures.dropTarget
                    UnitMission.AIR_SWEEP -> "Sweep wing" to AirFixtures.strikeTarget
                    UnitMission.NUCLEAR_STRIKE -> "Nuclear trial" to AirFixtures.nuclearTarget
                }
                AirFixtures.create(dir.resolve("initial")).use { runtime ->
                    runtime.game.currentPlayer = player
                    runtime.game.currentPlayerCiv = runtime.game.getCivilization(player)
                    runtime.executor.enableCheckpoints(CheckpointStore(checkpoint, beforeReplace = { if (failCommit && !afterReplace) error("Interrupted write") }, afterReplace = { if (failCommit && afterReplace) error("Lost reply") }))
                    val session = PlayerHost(runtime).bindPlayer(player)
                    val turn = session.turn()
                    val selected = unitByLabel(runtime, turn, label)
                    check(session.missionPreview(MissionPreviewRequest(selected.handle, mission, destination.position())).available)
                    val kind = if (mission == UnitMission.REBASE) TurnCommandKind.REBASE_UNIT else TurnCommandKind.valueOf(mission.name)
                    command = request(turn, kind, selected.handle).copy(destination = destination.position(), confirmAction = mission == UnitMission.NUCLEAR_STRIKE)
                    failCommit = true
                    check(runCatching { session.execute(command) }.isFailure)
                    check(runtime.executor.mutationFailure != null)
                }
                HeadlessRuntime.loadCheckpoint(checkpoint, dir.resolve("restored")).use { runtime ->
                    val session = PlayerHost(runtime).bindPlayer(player)
                    val retry = if (afterReplace) {
                        command
                    } else {
                        val fresh = session.turn()
                        val unread = command.copy(revision = fresh.revision)
                        val error = runCatching { session.execute(unread) }.exceptionOrNull() as PlayerSessionException
                        check(error.reason == PlayerSessionFailure.OBSERVATION_REQUIRED)
                        check(session.missionPreview(MissionPreviewRequest(command.target!!, mission, destination.position())).available)
                        command.copy(requestId = "fresh", revision = fresh.revision)
                    }
                    val receipt = session.execute(retry)
                    check(receipt.mission!!.mission == mission)
                    val saved = Files.readString(checkpoint)
                    val native = json().toJson(runtime.game)
                    check(session.execute(retry) == receipt)
                    check(saved == Files.readString(checkpoint) && native == json().toJson(runtime.game))
                }
            }
        }
    }
}
