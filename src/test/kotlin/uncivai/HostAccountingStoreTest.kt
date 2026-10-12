package uncivai

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.nio.file.Files
import java.sql.DriverManager

class HostAccountingStoreTest {
    @Test
    fun `failed accounting transaction rolls back every row and stops acknowledgements`() {
        val root = Files.createTempDirectory("accounting-failure-")
        try {
            val path = root.resolve("accounting.sqlite3")
            HostAccountingStore(path).use { store ->
                store.importLegacy(CommunicationState(jobs = mutableMapOf("job" to job(1))), "initial")
                DriverManager.getConnection("jdbc:sqlite:$path").use { db ->
                    db.createStatement().use { it.execute("CREATE TRIGGER fail_write BEFORE INSERT ON records WHEN NEW.key='bad' BEGIN SELECT RAISE(ABORT,'injected'); END") }
                }
                val batch = AccountingBatch(rows = listOf(AccountingRow("jobs", "job", job(2).recordJson()), AccountingRow("jobs", "bad", job(2).recordJson())))
                assertTrue(runCatching { store.submit(batch).join() }.isFailure)
                assertTrue(store.failure != null)
                assertTrue(runCatching { store.submit(AccountingBatch(rows = emptyList())).join() }.isFailure)
            }
            HostAccountingStore(path).use { store -> assertEquals(1L, store.restore(CommunicationState()).jobs.getValue("job").telemetry!!.sequence) }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    private fun job(sequence: Long) = CommunicationJob(
        "Rome",
        "gameplay",
        0,
        ModelUsage(sequence * 10),
        "epoch",
        1,
        0,
        telemetry = JobTelemetry(sequence, "COMPLETED", 1, 2, 1, 0, true, true),
    )

    @Test fun `crash after game replacement recovers its accounting outbox`() {
        for (after in listOf(false, true)) {
            val root = Files.createTempDirectory("accounting-crash-")
            try {
                val path = root.resolve("game.json")
                var armed = false
                val failure = { if (armed) throw IOException("injected") }
                val initial = GameCheckpoint(epoch = "epoch", paused = true, gameJson = "before", players = emptyMap(), communication = CommunicationState(jobs = mutableMapOf("job" to job(1))))
                CheckpointStore(path, beforeReplace = if (after) ({}) else failure, afterReplace = if (after) failure else ({})).use { store ->
                    store.writeInitial(initial)
                    val batch = AccountingBatch(rows = listOf(AccountingRow("jobs", "job", job(2).recordJson())))
                    store.accounting!!.stage(batch)
                    armed = true
                    assertTrue(runCatching { store.write(initial.copy(gameJson = "after")) }.isFailure)
                }
                CheckpointStore(path).use { store ->
                    val restored = store.read()
                    assertEquals(if (after) "after" else "before", restored.gameJson)
                    assertEquals(if (after) 2L else 1L, restored.communication.jobs.getValue("job").telemetry!!.sequence)
                }
            } finally {
                deleteTreeWithoutFollowingLinks(root)
            }
        }
    }

    @Test fun `old outbox cannot downgrade reports committed after the game checkpoint`() {
        val root = Files.createTempDirectory("accounting-replay-")
        try {
            val path = root.resolve("game.json")
            val initial = GameCheckpoint(epoch = "epoch", paused = true, gameJson = "{}", players = emptyMap(), communication = CommunicationState(jobs = mutableMapOf("job" to job(1))))
            CheckpointStore(path).use { store ->
                store.writeInitial(initial)
                val batch = AccountingBatch(rows = listOf(AccountingRow("jobs", "job", job(2).recordJson())))
                store.accounting!!.stage(batch)
                store.write(initial)
                store.accounting!!.submit(batch).join()
                store.accounting!!.submit(AccountingBatch(rows = listOf(AccountingRow("jobs", "job", job(3).recordJson())))).join()
            }
            repeat(2) {
                CheckpointStore(path).use { assertEquals(3L, it.read().communication.jobs.getValue("job").telemetry!!.sequence) }
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }

    @Test fun `legacy import preserves original file and cannot silently lose its database`() {
        val root = Files.createTempDirectory("accounting-import-")
        try {
            val path = root.resolve("game.json")
            val legacy = GameCheckpoint(formatVersion = 8, epoch = "epoch", paused = true, gameJson = "{}", players = emptyMap(), communication = CommunicationState(jobs = mutableMapOf("job" to job(2))))
            val raw = Json.encodeToString(legacy)
            Files.writeString(path, raw)
            CheckpointStore(path).use { store ->
                assertEquals(2L, store.read().communication.jobs.getValue("job").telemetry!!.sequence)
                assertEquals(raw, Files.readString(root.resolve("game.json.v8.json")))
            }
            Files.move(root.resolve("game.json.accounting.sqlite3"), root.resolve("removed.sqlite3"))
            CheckpointStore(path).use { assertTrue(runCatching { it.read() }.isFailure) }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
