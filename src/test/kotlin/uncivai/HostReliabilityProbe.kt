package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.nio.file.Files
import java.nio.file.Path
import java.security.MessageDigest
import java.sql.DriverManager
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** No model calls. Exercises concurrent accounting against real HTTPS and copied late-game saves. */
object HostReliabilityProbe {
    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args[0])
        Files.createDirectories(root)
        val checkpoint = root.resolve("game.json")
        val source = args.getOrNull(1)?.let(Path::of)
        if (source != null) copyCheckpoint(source, checkpoint)
        val store = CheckpointStore(checkpoint)
        val runtime = if (source == null) {
            HeadlessRuntime.createVanillaGame(root.resolve("data"), 42, 0, emptyList()).also { it.executor.enableCheckpoints(store) }
        } else {
            HeadlessRuntime.loadCheckpoint(store, root.resolve("data"))
        }
        val pool = Executors.newCachedThreadPool { task -> Thread(task, "reliability-client").apply { isDaemon = true } }
        val times = mutableListOf<Long>()
        var reports = 0
        runtime.use {
            val oldJobs = it.executor.communicationState.jobs.size
            val civilizations = it.game.civilizations.filter { it.isHuman() && it.isMajorCiv() }.map { it.civID }
            val tokens = civilizations.mapIndexed { index, civ -> civ to ('A' + index).toString().repeat(43) }.toMap()
            GameplayTestApi(it, root.resolve("tls"), tokens).use { api ->
                api.acquire()
                api.operator(
                    OperatorRequest(
                        "job_limits",
                        api.lease,
                        maxInvocations = runtime.executor.communicationState.maxInvocations,
                        maxConcurrent = maxOf(4, civilizations.size),
                        maxPerCivilization = 3,
                    ),
                )
                api.operator(OperatorRequest("unbarrier", api.lease))
                api.activate()
                val epoch = api.status().epoch
                val before = MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(checkpoint)).toList()
                val beforeWrites = store.writes
                val workers = civilizations.map { civ ->
                    pool.submit {
                        val token = api.playerToken(civ)
                        val id = "reliability-$civ"
                        fun call(op: String, extra: JsonObject = obj()) = api.communicate(token, JsonObject(obj("op" to op, "epoch" to epoch, "job" to id) + extra))
                        check(call("job_claim", obj("purpose" to "communication")).getValue("granted").jsonPrimitive.boolean)
                        repeat(20) { index ->
                            val start = System.nanoTime()
                            val report = JobTelemetry(index.toLong(), "RUNNING", 1, null, index.toLong(), 0, true, false)
                            check(call("job_report", obj("usage" to ModelUsage(index.toLong()).recordJson(), "telemetry" to report.recordJson())).getValue("granted").jsonPrimitive.boolean)
                            check(call("job_heartbeat").getValue("granted").jsonPrimitive.boolean)
                            synchronized(times) { times.add((System.nanoTime() - start) / 1_000_000) }
                        }
                        check(call("job_release").getValue("granted").jsonPrimitive.boolean)
                    }
                }
                workers.forEach { it.get(60, TimeUnit.SECONDS) }
                reports = civilizations.size * 20
                check(store.writes == beforeWrites) { "Accounting rewrote the game checkpoint" }
                check(before == MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(checkpoint)).toList())
                check(runtime.executor.communicationState.jobs.size == oldJobs + civilizations.size)

                val entered = CountDownLatch(1)
                val release = CountDownLatch(1)
                val busy = pool.submit {
                    runtime.executor.accessGame {
                        entered.countDown()
                        check(release.await(10, TimeUnit.SECONDS))
                    }
                }
                try {
                    check(entered.await(2, TimeUnit.SECONDS))
                    // Two blocked player requests must not consume the HTTP threads needed by control.
                    val reads = civilizations.take(2).map { civ -> pool.submit { api.turn(api.playerToken(civ)) } }
                    val health = pool.submit<OperatorStatus> {
                        api.operator(OperatorRequest("heartbeat", api.lease))
                        api.status()
                    }.get(2, TimeUnit.SECONDS)
                    check(health.phase == "ACTIVE" && health.busyOperation != null && health.leaseExpiresInMs > 0)
                    release.countDown()
                    reads.forEach { it.get(30, TimeUnit.SECONDS) }
                } finally {
                    release.countDown()
                    busy.get(12, TimeUnit.SECONDS)
                }
                runtime.executor.telemetry.flushMonitoring()
            }
        }
        pool.shutdownNow()
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { restored ->
            val own = restored.executor.communicationState.jobs.filterKeys { it.startsWith("reliability-") }
            check(own.isNotEmpty() && own.values.all { it.telemetry!!.sequence == 19L && it.usage!!.inputTokens == 19L })
            if (source == null) {
                val control = OperatorControl(restored.executor)
                val before = Files.readString(checkpoint)
                DriverManager.getConnection("jdbc:sqlite:${store.accountingPath}").use { db ->
                    db.createStatement().use { it.execute("CREATE TRIGGER fail_write BEFORE INSERT ON records BEGIN SELECT RAISE(ABORT,'injected'); END") }
                }
                val failure = runCatching { restored.executor.telemetry.startCall("Rome", "player_summary", obj()) }.exceptionOrNull()
                check(failure is RuntimeHaltedException) { "A failed accounting write was acknowledged: $failure" }
                check(control.handle(OperatorRequest("status")).status.phase == "HALTED")
                check(before == Files.readString(checkpoint)) { "Accounting failure rewrote the game checkpoint" }
            }
        }
        times.sort()
        val result = obj(
            "reports" to reports,
            "checkpointWritesForAccounting" to 0,
            "reportAndHeartbeatMedianMs" to times[times.size / 2],
            "reportAndHeartbeatP95Ms" to times[(times.size * .95).toInt()],
            "checkpointBytes" to Files.size(checkpoint),
            "accountingBytes" to Files.size(store.accountingPath),
            "timings" to JsonObject(HostTimings.snapshot()),
        )
        Files.writeString(root.resolve("result.json"), result.toString())
        println(result)
    }
}
