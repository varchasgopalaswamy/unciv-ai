package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import java.nio.file.Files
import java.sql.DriverManager

class AccountingMonitoringTest {
    @Test fun `failed publication retains event order and coalesces newer accounting without double counting`() {
        val root = Files.createTempDirectory("monitoring-retry-")
        try {
            val path = root.resolve("monitoring.sqlite3")
            MonitoringStore(path).write(emptyList())
            DriverManager.getConnection("jdbc:sqlite:$path").use { db ->
                db.createStatement().use { it.execute("CREATE TRIGGER fail_write BEFORE INSERT ON monitor_records BEGIN SELECT RAISE(ABORT,'injected'); END") }
                AccountingMonitoring(path).use { publisher ->
                    val context = obj("epoch" to "epoch", "matchId" to "match", "humanPlayers" to emptyList<String>(), "scores" to listOf(obj("civilization" to "Rome")), "startedAtMs" to 1, "result" to null)
                    fun job(tokens: Long) = AccountingRow("jobs", "job", CommunicationJob("Rome", "gameplay", 0, ModelUsage(tokens), "epoch", 1, 0).recordJson())
                    fun event(id: String) = MonitoringStore.Record("diplomacy", id, obj("id" to id))
                    publisher.enqueue(listOf(AccountingRow("meta", "game", context), job(10)))
                    publisher.enqueueMonitoring(listOf(event("first"), event("second")))
                    publisher.enqueueMonitoring(listOf(event("third")))
                    publisher.flush()
                    assertNotNull(publisher.error)
                    publisher.enqueue(listOf(job(20)))
                    publisher.enqueueMonitoring(listOf(event("fourth")))
                    db.createStatement().use { it.execute("DROP TRIGGER fail_write") }
                    publisher.flush()
                    assertNull(publisher.error)
                    db.createStatement().use { statement ->
                        statement.executeQuery("SELECT key FROM monitor_records WHERE collection='diplomacy' ORDER BY revision").use { rows ->
                            val ids = buildList { while (rows.next()) add(rows.getString(1)) }
                            assertEquals(listOf("first", "second", "third", "fourth"), ids)
                        }
                        statement.executeQuery("SELECT payload FROM monitor_records WHERE collection='state' AND key='result'").use { rows ->
                            check(rows.next())
                            val result = Json.parseToJsonElement(rows.getString(1)).jsonObject
                            assertEquals(20L, result.getValue("usage").jsonObject.getValue("input_tokens").jsonPrimitive.long)
                            assertEquals(1L, result.getValue("modelJobs").jsonPrimitive.long)
                        }
                    }
                }
            }
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
