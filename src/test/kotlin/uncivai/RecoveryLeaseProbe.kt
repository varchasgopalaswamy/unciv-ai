package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import uncivai.mcp.OperatorMcpBinding
import uncivai.mcp.PlayerMcpBinding
import uncivai.mcp.PlayerMcpServer
import uncivai.player.PlayerHost
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Files
import java.nio.file.Path
import java.time.Duration
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/** Actual HTTPS recovery of a native lease never reopens a paused graph. */
object RecoveryLeaseProbe {
    private const val LEASE = "original-frozen-lease"

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val checkpoint = root.resolve("checkpoint.json")
        val clock = AtomicLong(100)
        val usage = ModelUsage(100, 30, 70, 20)
        lateinit var retained: Map<String, CommunicationJob>
        lateinit var target: OperatorRequest
        HeadlessRuntime.createDuel(root.resolve("initial"), 814L).use { runtime ->
            val control = OperatorControl(runtime.executor) { clock.get() }
            Api(runtime, control, root.resolve("tls")).use { api ->
                val initial = api.operator(OperatorRequest("status")).status
                val request = OperatorRequest("recover_lease", leaseId = LEASE, epoch = initial.epoch, handoff = initial.handoff, turn = initial.turn)
                api.reject("DURABLE_CHECKPOINT_REQUIRED", request)
                runtime.executor.enableCheckpoints(checkpoint)
                api.operator(OperatorRequest("acquire", LEASE))
                api.operator(OperatorRequest("barrier", LEASE))
                runtime.executor.accessGame {
                    val state = runtime.executor.communicationState
                    state.invocations = 7
                    state.jobs["retained-paid-job"] = CommunicationJob(
                        civilization = "Rome", purpose = "gameplay", expires = System.currentTimeMillis() + 90000,
                        usage = usage, epoch = initial.epoch, handoff = initial.handoff, gameTurn = initial.turn,
                        claimedAtMs = 1000, computing = false,
                    )
                    runtime.executor.telemetry.changed("retained-paid-job")
                    runtime.executor.commitAccounting()
                    runtime.executor.commitCheckpoint()
                }
                val original = checkNotNull(api.operator(OperatorRequest("snapshot", LEASE)).checkpoint)
                val jobs = runtime.executor.communicationState.jobs.toMap()
                check(api.raw(request, api.playerToken).statusCode() == 401)
                api.reject("INVALID_ARGUMENT", request.copy(turn = null))
                api.reject("INVALID_ARGUMENT", request.copy(turn = -1))
                api.reject("INVALID_ARGUMENT", request.copy(handoff = -1))
                api.reject("INVALID_ARGUMENT", request.copy(civilization = "Rome"))
                expire(api, clock)
                api.reject("STALE_TURN", request.copy(epoch = "other-epoch"))
                api.reject("STALE_TURN", request.copy(turn = initial.turn + 1))
                api.reject("STALE_TURN", request.copy(handoff = initial.handoff + 1))
                check(api.operator(OperatorRequest("status")).status.leaseId == null)

                // Discard the successful reply, then retry the same frozen ID.
                api.operator(request)
                val recovered = api.operator(request)
                check(recovered.leaseId == LEASE && recovered.status.leaseId == LEASE && recovered.status.leaseExpiresInMs > 0)
                unchanged(runtime, api, original, jobs, usage)

                api.operator(OperatorRequest("release", LEASE))
                api.operator(OperatorRequest("acquire", "foreign-owner"))
                api.reject("LEASE_BUSY", request)
                check(api.operator(OperatorRequest("status")).status.leaseId == "foreign-owner")
                api.operator(OperatorRequest("release", "foreign-owner"))
                api.operator(request)
                api.operator(OperatorRequest("unbarrier", LEASE))
                api.operator(OperatorRequest("release", LEASE))
                api.reject("RECOVERY_BARRIER_REQUIRED", request)
                check(api.operator(OperatorRequest("status")).status.leaseId == null)
                api.operator(OperatorRequest("manual"))
                api.reject("RECOVERY_BARRIER_REQUIRED", request)
                check(!api.operator(OperatorRequest("status")).status.scheduled)
                api.operator(OperatorRequest("pause"))
                api.reject("RECOVERY_BARRIER_REQUIRED", request)
                check(api.operator(OperatorRequest("status")).status.phase == "PAUSED" && !runtime.executor.scheduled)
                api.operator(OperatorRequest("acquire", LEASE))
                api.operator(OperatorRequest("barrier", LEASE))

                // Both requests wait behind the real game executor while the
                // old lease expires. Their atomic guards run after that wait.
                val entered = CountDownLatch(1)
                val release = CountDownLatch(1)
                val started = CountDownLatch(2)
                val workers = Executors.newFixedThreadPool(3)
                try {
                    val busy = workers.submit {
                        runtime.executor.accessOperatorGame {
                            entered.countDown()
                            check(release.await(10, TimeUnit.SECONDS))
                        }
                    }
                    check(entered.await(10, TimeUnit.SECONDS))
                    clock.addAndGet(OperatorControl.LEASE_MILLIS + 1)
                    val first = workers.submit<OperatorResponse> {
                        started.countDown()
                        api.operator(request)
                    }
                    val second = workers.submit<OperatorResponse> {
                        started.countDown()
                        api.operator(request)
                    }
                    check(started.await(10, TimeUnit.SECONDS))
                    check(!first.isDone && !second.isDone)
                    check(api.operator(OperatorRequest("status")).status.leaseId == null)
                    release.countDown()
                    busy.get(10, TimeUnit.SECONDS)
                    check(first.get(10, TimeUnit.SECONDS).leaseId == LEASE)
                    check(second.get(10, TimeUnit.SECONDS).leaseId == LEASE)
                } finally {
                    release.countDown()
                    workers.shutdownNow()
                    check(workers.awaitTermination(10, TimeUnit.SECONDS))
                }
                unchanged(runtime, api, original, jobs, usage)

                api.operator(OperatorRequest("restore", leaseId = LEASE, epoch = "restored-target", checkpoint = original))
                check(runtime.executor.communicationState.restorePending)
                target = request.copy(epoch = "restored-target")
                expire(api, clock)
                api.operator(target)
                api.operator(target)
                check(runtime.executor.communicationState.restorePending)
                api.reject("RESTORE_INCOMPLETE", OperatorRequest("unbarrier", LEASE))
                val status = api.operator(OperatorRequest("status")).status
                check(status.modelInvocations == 7L && status.modelUsage == usageMap(usage))
                retained = runtime.executor.communicationState.jobs.toMap()
            }
        }
        // Lease state is ephemeral. A process restart must recover from the
        // actual target checkpoint without ordinary acquire or a new ID.
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            val control = OperatorControl(runtime.executor) { clock.get() }
            Api(runtime, control, root.resolve("restart-tls")).use { api ->
                check(api.operator(OperatorRequest("status")).status.leaseId == null)
                check(runtime.executor.communicationState.restorePending && runtime.executor.scheduled)
                val recovered = api.operator(target)
                check(recovered.leaseId == LEASE && recovered.status.epoch == target.epoch)
                check(recovered.status.phase == "PAUSED" && recovered.status.scheduled)
                check(recovered.status.modelInvocations == 7L && recovered.status.modelUsage == usageMap(usage))
                check(runtime.executor.communicationState.jobs == retained)
                check(runtime.executor.communicationState.restorePending && runtime.executor.communicationPaused)
                api.operator(target)
                check(runtime.executor.communicationState.jobs == retained)
            }
        }
    }

    private fun expire(api: Api, clock: AtomicLong) {
        clock.addAndGet(OperatorControl.LEASE_MILLIS + 1)
        check(api.operator(OperatorRequest("status")).status.leaseId == null)
        api.reject("LEASE_LOST", OperatorRequest("heartbeat", LEASE))
    }

    private fun usageMap(usage: ModelUsage) = mapOf(
        "input_tokens" to usage.inputTokens,
        "cached_input_tokens" to usage.cachedInputTokens,
        "output_tokens" to usage.outputTokens,
        "reasoning_output_tokens" to usage.reasoningOutputTokens,
    )

    private fun unchanged(runtime: HeadlessRuntime, api: Api, before: GameCheckpoint, jobs: Map<String, CommunicationJob>, usage: ModelUsage) {
        val current = checkNotNull(api.operator(OperatorRequest("snapshot", LEASE)).checkpoint)
        check(current.gameJson == before.gameJson && current.players == before.players)
        check(current.epoch == before.epoch && current.handoff == before.handoff)
        check(current.paused && current.communicationPaused && current.scheduled)
        check(current.communication.restorePending == before.communication.restorePending)
        check(runtime.executor.communicationState.jobs == jobs && runtime.executor.communicationState.invocations == 7L)
        check(api.operator(OperatorRequest("status")).status.modelUsage == usageMap(usage))
    }

    private class Api(runtime: HeadlessRuntime, control: OperatorControl, tlsRoot: Path) : AutoCloseable {
        private val tls = TlsTestFixture(Files.createDirectories(tlsRoot))
        private val client = HttpClient.newBuilder().sslContext(tls.context).connectTimeout(Duration.ofSeconds(5)).build()
        private val operatorToken = "O".repeat(43)
        val playerToken = "R".repeat(43)
        private val server = PlayerMcpServer(
            listOf(PlayerMcpBinding(playerToken, PlayerHost(runtime).bindPlayer("Rome"))),
            0,
            OperatorMcpBinding(operatorToken, control),
            tls.tls,
        )
        private val url = URI.create("https://localhost:${server.start()}/operator")

        fun raw(request: OperatorRequest, token: String = operatorToken): HttpResponse<String> = client.send(
            HttpRequest.newBuilder(url).timeout(Duration.ofSeconds(15))
                .header("Authorization", "Bearer $token").header("Content-Type", "application/json")
                .header("Accept", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(OperatorControl.json.encodeToString(request))).build(),
            HttpResponse.BodyHandlers.ofString(),
        )

        fun operator(request: OperatorRequest): OperatorResponse {
            val response = raw(request)
            check(response.statusCode() == 200) { "Operator ${request.op}: ${response.statusCode()} ${response.body()}" }
            return OperatorControl.json.decodeFromString(response.body())
        }

        fun reject(code: String, request: OperatorRequest) {
            val response = raw(request)
            check(response.statusCode() == if (code == "INVALID_ARGUMENT") 400 else 409) { response.body() }
            check(Json.parseToJsonElement(response.body()).jsonObject.getValue("error").jsonPrimitive.content == code) { response.body() }
        }

        override fun close() {
            server.close()
            client.close()
        }
    }
}
