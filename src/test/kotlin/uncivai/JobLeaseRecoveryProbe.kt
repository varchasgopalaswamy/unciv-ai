package uncivai

import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonPrimitive
import java.nio.file.Path

/** Administrative lease renewal must retain the original paid job and accounting. */
object JobLeaseRecoveryProbe {
    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val checkpoint = root.resolve("game.json")
        lateinit var epoch: String
        lateinit var retained: CommunicationJob
        var invocations = 0L
        HeadlessRuntime.createDuel(root.resolve("initial"), 417L).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            epoch = runtime.epoch
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                api.activate()
                val token = api.playerToken("Rome")
                fun operation(op: String, id: String) = api.communicate(
                    token,
                    if (op == "job_claim") {
                        obj("op" to op, "epoch" to epoch, "job" to id, "purpose" to "gameplay")
                    } else {
                        obj("op" to op, "epoch" to epoch, "job" to id)
                    },
                )
                fun recover(id: String, civilization: String = "Rome", claimedEpoch: String = epoch, lease: String = api.lease) = api.operator(OperatorRequest("resume_job", leaseId = lease, epoch = claimedEpoch, civilization = civilization, job = id))
                check(operation("job_claim", "main").getValue("granted").jsonPrimitive.boolean)
                val usage = ModelUsage(100, 20, 30, 10)
                val report = JobTelemetry(3, "WAITING_ROLE", 1000, null, 500, 0, true, false, "model", "medium", emptyMap(), modelRequests = 4)
                api.communicate(token, obj("op" to "job_report", "epoch" to epoch, "job" to "main", "usage" to usage.recordJson(), "telemetry" to report.recordJson()))
                runtime.executor.accessGame {
                    val state = runtime.executor.communicationState
                    state.jobs["main"] = state.jobs.getValue("main").copy(expires = 1, computing = true)
                }
                check(!operation("job_heartbeat", "main").getValue("granted").jsonPrimitive.boolean)
                check(runCatching { recover("main") }.isFailure) // active games are not recovery barriers
                api.operator(OperatorRequest("pause", api.lease))
                check(runCatching { recover("main", lease = "wrong") }.isFailure)
                check(runCatching { recover("main", claimedEpoch = "wrong") }.isFailure)
                check(runCatching { recover("main", civilization = "Greece") }.isFailure)
                val before = runtime.executor.communicationState.jobs.getValue("main")
                invocations = runtime.executor.communicationState.invocations
                check(recover("main").jobRecovered == true)
                val after = runtime.executor.communicationState.jobs.getValue("main")
                check(after.expires > System.currentTimeMillis() && !after.computing)
                check(after.copy(expires = before.expires, computing = before.computing) == before)
                check(recover("main").jobRecovered == true) // lost reply retries do not change paid identity
                check(operation("job_claim", "main").getValue("granted").jsonPrimitive.boolean)
                check(runtime.executor.communicationState.invocations == invocations)
                check(operation("job_release", "main").getValue("granted").jsonPrimitive.boolean)
                check(recover("main").jobRecovered == false)
                check(!operation("job_claim", "main").getValue("granted").jsonPrimitive.boolean)
                check(runCatching { recover("missing") }.isFailure)
                check(operation("job_claim", "retained").getValue("granted").jsonPrimitive.boolean)
                runtime.executor.accessGame {
                    val state = runtime.executor.communicationState
                    state.jobs["retained"] = state.jobs.getValue("retained").copy(expires = 1)
                    runtime.executor.telemetry.changed("retained")
                    runtime.executor.commitAccounting()
                    runtime.executor.commitCheckpoint()
                    retained = state.jobs.getValue("retained")
                    invocations = state.invocations
                }
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            GameplayTestApi(runtime, root.resolve("restart-tls")).use { api ->
                api.acquire()
                check(runtime.epoch == epoch)
                check(runtime.executor.communicationState.jobs.getValue("retained") == retained)
                val recovery = OperatorRequest("resume_job", leaseId = api.lease, epoch = epoch, civilization = "Rome", job = "retained")
                check(api.operator(recovery).jobRecovered == true)
                check(runtime.executor.communicationState.invocations == invocations)
                check(runtime.executor.communicationState.jobs.getValue("main").expires == 0L)
                api.operator(OperatorRequest("barrier", api.lease))
                val snapshot = runtime.executor.turnSnapshot()
                api.operator(OperatorRequest("restore", leaseId = api.lease, epoch = "rewound", checkpoint = snapshot))
                check(runtime.executor.communicationState.jobs.getValue("retained").expires == 0L)
                check(api.operator(recovery.copy(epoch = "rewound")).jobRecovered == false)
            }
        }
    }
}
