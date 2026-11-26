package uncivai

import com.unciv.logic.civilization.managers.TurnManager
import com.unciv.logic.map.MapSize
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import uncivai.player.PlayerHost
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.io.IOException
import java.nio.file.Files
import java.nio.file.Path
import java.util.zip.GZIPInputStream

class ReplayArchiveTest {
    private fun saved(path: Path) = Json.decodeFromString<GameCheckpoint>(Files.readString(path))

    private fun frame(path: Path, cursor: ReplayCursor): JsonObject {
        val frame = path.resolveSibling("${path.fileName}.replay").resolve(cursor.filename)
        val json = GZIPInputStream(Files.newInputStream(frame)).bufferedReader().use { it.readText() }
        return Json.parseToJsonElement(json).jsonObject
    }

    private fun events(path: Path, cursor: ReplayCursor): JsonArray = frame(path, cursor).getValue("events").jsonArray

    @Test
    fun recordsNativeMovesAndKeepsCommittedCursorAcrossInterruptedWrite() {
        val root = Files.createTempDirectory("native-replay-")
        try {
            val path = root.resolve("exploration.json")
            var interrupt = false
            val store = CheckpointStore(path, beforeReplace = {
                if (interrupt) throw IOException("Test interruption before checkpoint replacement")
            }, recordReplay = true)
            lateinit var cursor: ReplayCursor
            HeadlessRuntime.createVanillaGame(root.resolve("data"), 422L, builtInAI = listOf("Egypt"),
                agentPlayers = listOf("Rome"), mapSize = MapSize.Tiny,
                options = VanillaOptions(startingPositionSeed = 17L)).use { runtime ->
                runtime.executor.enableCheckpoints(store)
                val initial = checkNotNull(saved(path).replay)
                assertEquals(0L, initial.sequence)
                assertTrue(events(path, initial).any { it.jsonObject["civilization"]?.jsonPrimitive?.content == "Egypt" })
                val session = PlayerHost(runtime).bindPlayer("Rome")
                val turn = session.turn()
                val settler = turn.map.units.single { it.name == "Settler" }
                val found = TurnCommandRequest("settle", turn.epoch, turn.revision, TurnCommandKind.FOUND_CITY, settler.handle)
                val receipt = session.execute(found)
                val settled = checkNotNull(saved(path).replay)
                assertEquals(receipt, session.execute(found))
                assertEquals(settled, saved(path).replay)
                assertTrue(events(path, settled).any { it.jsonObject["kind"]?.jsonPrimitive?.content == "input" })
                runtime.executor.accessGame { game ->
                    val ai = game.getCivilization("Egypt")
                    TurnManager(ai).startTurn()
                    val unit = ai.units.getCivUnits().first { it.name == "Warrior" }
                    val destination = unit.currentTile.neighbors.first { unit.movement.canMoveTo(it) }
                    unit.movement.moveToTile(destination)
                    runtime.executor.commitCheckpoint()
                }
                cursor = checkNotNull(saved(path).replay)
                val nativeMoves = events(path, cursor).map { it.jsonObject }.filter {
                    it["kind"]?.jsonPrimitive?.content == "unitMoved"
                }
                assertTrue(nativeMoves.any { it["owner"]?.jsonPrimitive?.content == "Egypt" })
                interrupt = true
                assertThrows(RuntimeHaltedException::class.java) {
                    runtime.executor.accessGame { game ->
                        game.getCivilization("Rome").addGold(1)
                        runtime.executor.commitCheckpoint()
                    }
                }
                assertEquals(cursor, saved(path).replay)
            }
            HeadlessRuntime.loadCheckpoint(path, root.resolve("restored")).use { runtime ->
                assertTrue(checkNotNull(saved(path).replay).sequence > cursor.sequence)
                val frame = events(path, checkNotNull(saved(path).replay))
                assertTrue(frame.any { it is JsonObject && it["kind"]?.jsonPrimitive?.content == "visibilityReset" })
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    @Test
    fun recordsPrivateDiplomacyForItsSenderAndRecipient() {
        val root = Files.createTempDirectory("native-replay-diplomacy-")
        try {
            val path = root.resolve("exploration.json")
            HeadlessRuntime.createVanillaGame(root.resolve("data"), 422L, builtInAI = listOf("Egypt"),
                agentPlayers = listOf("Rome", "Greece"), mapSize = MapSize.Tiny).use { runtime ->
                val executor = runtime.executor
                executor.enableCheckpoints(path, recordReplay = true)
                val request = obj("op" to "send", "epoch" to executor.epoch, "requestId" to "greeting",
                    "to" to "Greece", "text" to "Would you agree to keep scouts east of the river?")
                val response = executor.communication.handle("Rome", request)
                val checkpoint = saved(path)
                val cursor = checkNotNull(checkpoint.replay)
                val archive = events(path, cursor).map { it.jsonObject }
                val deliveries = archive.filter { it["kind"]?.jsonPrimitive?.content == "diplomacy" }
                assertEquals(listOf("Greece", "Rome"), deliveries.map { it.getValue("civilization").jsonPrimitive.content })
                deliveries.forEach { event ->
                    val recipient = event.getValue("civilization").jsonPrimitive.content
                    val canonical = checkpoint.communication.streams.getValue(recipient).single()
                    assertEquals(canonical.recordJson(), event.getValue("delivered"))
                }
                assertTrue(archive.indexOf(deliveries.last()) < archive.indexOfFirst { it["kind"]?.jsonPrimitive?.content == "input" })
                assertEquals(response, executor.communication.handle("Rome", request))
                assertEquals(cursor, saved(path).replay)
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    @Test
    fun restoreStartsNewEpochAtSnapshotAndOmitsAbandonedFrames() {
        val root = Files.createTempDirectory("native-replay-rewind-")
        try {
            val path = root.resolve("exploration.json")
            HeadlessRuntime.createVanillaGame(root.resolve("data"), 422L, agentPlayers = listOf("Rome")).use { runtime ->
                val executor = runtime.executor
                executor.enableCheckpoints(path, recordReplay = true)
                executor.scheduled = true
                executor.communicationPaused = true
                executor.pause()
                val snapshot = executor.turnSnapshot()
                val branch = checkNotNull(snapshot.replay)
                executor.accessGame { game ->
                    game.getCivilization("Rome").addGold(13)
                    executor.commitCheckpoint()
                }
                val abandoned = checkNotNull(saved(path).replay)
                executor.restoreTurn(snapshot, "new-epoch")
                val replacement = checkNotNull(saved(path).replay)
                assertEquals(abandoned.sequence, replacement.sequence)
                assertTrue(abandoned.sha256 != replacement.sha256)
                assertEquals(branch.sha256, frame(path, replacement).getValue("previous").jsonPrimitive.content)
                assertEquals("new-epoch", frame(path, replacement).getValue("epoch").jsonPrimitive.content)
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
