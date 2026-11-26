package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import java.nio.file.Path
import java.sql.DriverManager

/** One transport-origin category per real HTTPS call, independent of its tool/suboperation. */
object McpOriginProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        val checkpoint = root.resolve("game.json")
        HeadlessRuntime.createDuel(root.resolve("initial"), 421L).use { runtime ->
            // An older checkpoint has counters without origin labels. They must
            // remain unknown, rather than being retroactively guessed from op.
            val key = "${runtime.epoch}/0"
            runtime.executor.communicationState.telemetry.windows[key] = ApiWindow(
                runtime.epoch,
                0,
                0,
                "Rome",
                linkedMapOf(
                    "Rome" to linkedMapOf(
                        "tool:player_communication" to ApiCounts(calls = 2, succeeded = 2),
                        "communication:read" to ApiCounts(calls = 2, succeeded = 2),
                    ),
                ),
            )
            runtime.executor.enableCheckpoints(checkpoint)
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                val token = api.playerToken("Rome")
                val request = obj("request" to obj("op" to "read", "epoch" to runtime.epoch, "cursor" to 0, "waitSeconds" to 0))
                val legacy = runtime.executor.communicationState.telemetry.windows.values.single().actors.getValue("Rome")
                check(legacy["tool:player_communication"]!!.calls == 2L && legacy.keys.none { it.startsWith("origin:") })
                val valid = listOf("background_poll", "agent", "coordinator")
                for (origin in valid) {
                    val result = api.tool(token, "player_communication", request, obj("civAgentOrigin" to origin))
                    check(result.getValue("epoch").jsonPrimitive.content == runtime.epoch)
                }
                for (meta in listOf(null, obj("civAgentOrigin" to "unknown"), obj("civAgentOrigin" to ""), obj("civAgentOrigin" to 1))) {
                    api.tool(token, "player_communication", request, meta)
                }
                val counts = runtime.executor.communicationState.telemetry.windows.values.single().actors.getValue("Rome")
                valid.forEach { origin -> check(counts.getValue("origin:$origin").calls == 1L) }
                check(counts.getValue("origin:unclassified").calls == 4L)
                check(counts.getValue("tool:player_communication").calls == 9L)
                check(counts.getValue("communication:read").calls == 9L)
                check(counts.filterKeys { it.startsWith("origin:") }.values.sumOf { it.calls } == 7L)
                check(counts.values.all { it.succeeded == it.calls && it.errors == 0L })
                runtime.executor.telemetry.flushMonitoring()
                val monitoring = DriverManager.getConnection("jdbc:sqlite:${root.resolve("runner/monitoring.sqlite3")}").use { db ->
                    db.prepareStatement("SELECT payload FROM monitor_records WHERE collection='state' AND key='result'").use { statement ->
                        statement.executeQuery().use { rows ->
                            check(rows.next())
                            Json.parseToJsonElement(rows.getString(1)).jsonObject
                        }
                    }
                }.getValue("hostCalls").jsonObject
                check(monitoring.getValue("origin:agent").jsonObject.getValue("calls").jsonPrimitive.long == 1L)
                check(monitoring.getValue("tool:player_communication").jsonObject.getValue("calls").jsonPrimitive.long == 9L)
                runtime.executor.commitCheckpoint()
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart-again")).use { runtime ->
            val counts = runtime.executor.communicationState.telemetry.windows.values.single().actors.getValue("Rome")
            check(counts.getValue("communication:read").calls == 9L)
            check(counts.getValue("origin:background_poll").calls == 1L)
            check(counts.getValue("origin:coordinator").calls == 1L)
            check(counts.getValue("origin:unclassified").calls == 4L)
            check(counts.getValue("tool:player_communication").calls - counts.filterKeys { it.startsWith("origin:") }.values.sumOf { it.calls } == 2L)
        }
    }
}
