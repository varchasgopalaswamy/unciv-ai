package uncivai

import com.unciv.json.json
import com.unciv.logic.civilization.LocationAction
import com.unciv.logic.civilization.NotificationCategory
import com.unciv.logic.civilization.NotificationIcon
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.UnitSettlement
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.jsonObject
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TilePosition
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

@Serializable
private data class NotificationRecovery(
    val before: PlayerTurnSnapshot,
    val request: TurnCommandRequest,
    val receipt: TurnCommandReceipt? = null,
    val after: PlayerTurnSnapshot? = null,
)

object NotificationsProbe {
    private const val CAMP = "A new barbarian encampment has spawned!"
    private const val ECONOMY = "Received [16] Gold\nReview economic plans."

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val checkpoint = root.resolve("checkpoint.json")
        val stage = args[1]
        val runtime = if (stage == "prepare") HeadlessRuntime.createVanillaGame(root.resolve("initial"), 412L) else HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve(stage))
        runtime.use {
            if (stage == "prepare") {
                for (player in runtime.game.civilizations.filter { it.isHuman() && it.isMajorCiv() }) {
                    checkNotNull(UnitSettlement.tryFoundCity(player.units.getCivUnits().single { it.name == "Settler" }))
                    player.notifications.clear()
                    player.popupAlerts.clear()
                }
                val rome = runtime.game.getCivilization("Rome")
                rome.addNotification(CAMP, LocationAction(HexCoord.Zero, HexCoord(0, 7), HexCoord(7, 0)), NotificationCategory.War, NotificationIcon.War)
                repeat(2) { rome.addNotification(ECONOMY, NotificationCategory.Trade, NotificationIcon.Gold) }
                runtime.game.getCivilization("Greece").addNotification("PRIVATE-GREEK-MESSAGE", NotificationCategory.General)
                runtime.executor.enableCheckpoints(checkpoint)
            }
            GameplayTestApi(runtime, root.resolve("tls-$stage")).use { api ->
                when (stage) {
                    "prepare" -> prepare(root, runtime, api)
                    "dismiss" -> dismiss(root, runtime, api)
                    "restore" -> restore(root, runtime, api)
                    else -> error("Unknown stage: $stage")
                }
            }
        }
    }

    private fun prepare(root: Path, runtime: HeadlessRuntime, api: GameplayTestApi) {
        val token = api.playerToken("Rome")
        val native = json().toJson(runtime.game)
        val before = api.turn(token)
        val waiting = api.turn(api.playerToken("Greece"))
        check(before.notifications.size == 3 && before.notifications.all { it.canDismiss })
        check(before.notifications.map { it.handle } == listOf("notice1", "notice2", "notice3"))
        check(before.notifications.first().text == CAMP.tr())
        check(before.notifications.first().category == "War")
        check(before.notifications.first().icons == listOf(NotificationIcon.War))
        check(before.notifications.first().locations == listOf(TilePosition(0, 0), TilePosition(0, 7), TilePosition(7, 0))) { "Incorrect event locations: ${before.notifications.first()}" }
        check(before.notifications.drop(1).all { it.text == ECONOMY.tr() && it.locations.isEmpty() })
        check(waiting.notifications.single().text == "PRIVATE-GREEK-MESSAGE" && !waiting.notifications.single().canDismiss)
        check("PRIVATE-GREEK" !in PlayerTurnJson.encode(before))
        check(json().toJson(runtime.game) == native) { "Reading notifications mutated native state" }
        check(api.turn(token) == before)

        runtime.executor.accessGame { it.getCivilization("Greece").addNotification("ANOTHER-PRIVATE-MESSAGE", NotificationCategory.Production) }
        check(api.turn(token) == before) { "Foreign notifications changed a player's revision" }
        val request = command(before, before.notifications.take(2).map { it.handle })
        api.reject(api.playerToken("Greece"), command(waiting, listOf(waiting.notifications.single().handle)), "NOT_YOUR_TURN")
        for (targets in listOf(emptyList(), listOf("notice1", "notice1"), listOf(" "))) {
            api.reject(token, command(before, targets), "INVALID_ARGUMENT")
        }
        api.reject(token, command(before, listOf("notice1", "notice999")), "OPTION_UNAVAILABLE")
        check(api.turn(token) == before) { "An invalid batch partially dismissed notifications" }
        check(runtime.game.getCivilization("Rome").notifications.size == 3)

        val detached = PlayerHost(runtime).bindPlayer("Rome").turn()
        check(runCatching { (detached.notifications as MutableList).clear() }.exceptionOrNull() is UnsupportedOperationException)
        check(runCatching { (detached.notifications.first().locations as MutableList).clear() }.exceptionOrNull() is UnsupportedOperationException)
        runtime.executor.accessGame { runtime.executor.commitCheckpoint() }
        write(root, NotificationRecovery(before, request))
        verifyLoadNotices(root, runtime, before)
    }

    private fun verifyLoadNotices(root: Path, runtime: HeadlessRuntime, before: PlayerTurnSnapshot) {
        val saved = CheckpointStore.capture(runtime.game, runtime.epoch, false, runtime.executor.turnSessions)
        for (mode in listOf("append", "missing", "reordered", "bad-handles")) {
            val loaded = runtime.game.clone().apply { setTransients() }
            val player = loaded.getCivilization("Rome")
            when (mode) {
                "append" -> player.addNotification("New engine notice during load", NotificationCategory.General)
                "missing" -> player.notifications.removeAt(0)
                "reordered" -> java.util.Collections.swap(player.notifications, 0, 1)
            }
            val checkpoint = if (mode == "bad-handles") {
                val state = saved.players.getValue("Rome")
                saved.copy(players = saved.players + ("Rome" to state.copy(notificationHandles = state.notificationHandles.dropLast(1))))
            } else {
                saved
            }
            GameExecutor(loaded).use { executor ->
                CheckpointStore(root.resolve("load-$mode.json")).use { store ->
                    val result = runCatching { executor.restore(checkpoint, store) }
                    if (mode != "append") {
                        check(result.isFailure) { "Accepted $mode notification state" }
                    } else {
                        result.getOrThrow()
                        val restored = PlayerHost(executor).bindPlayer("Rome").turn()
                        check(restored.notifications.take(3) == before.notifications)
                        check(restored.notifications.last().let { it.handle == "notice4" && it.text == "New engine notice during load" })
                        check(restored.revision != before.revision)
                    }
                }
            }
        }
    }

    private fun dismiss(root: Path, runtime: HeadlessRuntime, api: GameplayTestApi) {
        val saved = read(root)
        val token = api.playerToken("Rome")
        // Restart does not preserve read authorization. A receipt or an unrelated query is insufficient.
        api.reject(token, saved.request, "OBSERVATION_REQUIRED")
        api.tool(token, "player_summary")
        api.reject(token, saved.request, "OBSERVATION_REQUIRED")
        check(api.turn(token) == saved.before)
        val native = json().toJson(runtime.game)
        val receipt = api.execute(token, saved.request)
        check(receipt.dismissedNotifications == saved.before.notifications.take(2))
        check(json().toJson(runtime.game) == native) { "Dismissal erased native notifications or altered gameplay" }
        check(api.execute(token, saved.request) == receipt)
        val pending = saved.before.notifications.last()
        api.reject(token, command(saved.before, listOf(pending.handle)).copy(revision = receipt.revision), "OBSERVATION_REQUIRED")
        val after = api.turn(token)
        check(after.notifications == listOf(pending)) { "Identical messages were collapsed or dismissed together" }
        check(after.combatReports == saved.before.combatReports)
        val cached = PlayerHost(runtime).bindPlayer("Rome").execute(saved.request)
        check(runCatching { (cached.dismissedNotifications as MutableList).clear() }.exceptionOrNull() is UnsupportedOperationException)

        runtime.executor.accessGame { it.getCivilization("Rome").addNotification(CAMP, HexCoord.Zero, NotificationCategory.War) }
        api.reject(token, command(after, listOf(pending.handle)), "STALE_REVISION")
        val fresh = api.turn(token)
        check(fresh.notifications.map { it.handle } == listOf("notice3", "notice4"))
        check(fresh.notifications.last().text == saved.before.notifications.first().text)
        api.reject(token, command(fresh, listOf("notice1")), "OPTION_UNAVAILABLE")
        check(runtime.game.getCivilization("Rome").notifications.size == 4)
        write(root, saved.copy(receipt = receipt, after = fresh))
    }

    private fun restore(root: Path, runtime: HeadlessRuntime, api: GameplayTestApi) {
        val saved = read(root)
        val token = api.playerToken("Rome")
        check(api.execute(token, saved.request) == saved.receipt) { "Committed dismissal did not replay after restart" }
        val restored = api.turn(token)
        check(restored.notifications == saved.after!!.notifications) { "Notifications changed after restart: ${restored.notifications}, expected ${saved.after.notifications}" }
        check(restored == saved.after) {
            val expected = PlayerTurnJson.json.encodeToJsonElement(PlayerTurnSnapshot.serializer(), saved.after).jsonObject
            val actual = PlayerTurnJson.json.encodeToJsonElement(PlayerTurnSnapshot.serializer(), restored).jsonObject
            "Restart changed: " + expected.keys.filter { expected[it] != actual[it] }.joinToString { "$it: expected=${expected[it]} actual=${actual[it]}" }
        }

        api.acquire()
        val activation = api.activate()
        val activeToken = checkNotNull(activation.playerToken)
        val before = api.turn(activeToken)
        runtime.executor.accessGame { it.getCivilization("Rome").addNotification("Discarded attempt", NotificationCategory.General) }
        val attempt = api.turn(activeToken)
        val discardedHandle = attempt.notifications.last().handle
        val discard = command(attempt, attempt.notifications.map { it.handle })
        api.execute(activeToken, discard)
        check(api.turn(activeToken).notifications.isEmpty())
        val status = api.status()
        api.operator(OperatorRequest("rollback", api.lease, status.epoch, status.serverInstance, status.handoff, status.activeCivilization, activation.activationId))
        val resumedToken = checkNotNull(api.activate().playerToken)
        check(api.turn(resumedToken).notifications == before.notifications) { "Rollback did not restore pending notifications" }
        api.reject(resumedToken, discard, "STALE_REVISION")
        runtime.executor.accessGame { it.getCivilization("Rome").addNotification("New attempt", NotificationCategory.General) }
        val fresh = api.turn(resumedToken)
        check(fresh.notifications.last().handle != discardedHandle) { "Rollback recycled a discarded notification handle" }

        // Leaving ordinary messages pending does not block END_TURN; the native log retains even dismissed ones.
        api.finishTurn(resumedToken)
        val rome = runtime.game.getCivilization("Rome")
        val archived = rome.notificationsLog.flatMap { it.notifications }
        check(archived.count { it.text == ECONOMY } == 2)
        check(archived.count { it.text == CAMP } == 2)
        check(runtime.game.getCivilization("Greece").notifications.any { it.text == "PRIVATE-GREEK-MESSAGE" })
        check(api.turn(token).notifications.none { it.handle in fresh.notifications.map { n -> n.handle } })
    }

    private fun command(turn: PlayerTurnSnapshot, targets: List<String>) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, TurnCommandKind.DISMISS_NOTIFICATIONS, targets = targets)
    private fun read(root: Path) = PlayerTurnJson.json.decodeFromString<NotificationRecovery>(Files.readString(root.resolve("expected.json")))
    private fun write(root: Path, value: NotificationRecovery) = Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(value))
}
