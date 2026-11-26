package uncivai

import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonPrimitive
import java.nio.file.Path

/** Native authority and accounting are tested through the authenticated transport. */
object RuntimeAuthorityProbe {
    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val checkpoint = root.resolve("game.json")
        HeadlessRuntime.createDuel(root.resolve("initial"), 417L).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                val connected = api.operator(OperatorRequest("connect", leaseId = api.lease, epoch = runtime.epoch, civilization = "Rome"))
                check(api.operator(OperatorRequest("connect", leaseId = api.lease, epoch = runtime.epoch, civilization = "Rome")).playerToken == connected.playerToken)
                api.activate()
                val token = api.playerToken("Rome")
                fun claim(id: String, purpose: String, parent: String? = null) = api.communicate(
                    token,
                    if (parent == null) {
                        obj("op" to "job_claim", "epoch" to runtime.epoch, "job" to id, "purpose" to purpose)
                    } else {
                        obj("op" to "job_claim", "epoch" to runtime.epoch, "job" to id, "purpose" to purpose, "parentJob" to parent)
                    },
                )
                fun operation(op: String, id: String) = api.communicate(token, obj("op" to op, "epoch" to runtime.epoch, "job" to id))
                fun report(id: String, sequence: Int, phase: String, release: Boolean = false) = api.communicate(
                    token,
                    obj(
                        "op" to if (release) "job_release" else "job_report",
                        "epoch" to runtime.epoch,
                        "job" to id,
                        "usage" to ModelUsage(sequence.toLong(), 0, 0, 0).recordJson(),
                        "telemetry" to JobTelemetry(sequence.toLong(), phase, 1000, null, sequence.toLong(), 0, true, false, "test", null, emptyMap()).recordJson(),
                    ),
                )
                check(claim("main", "gameplay").getValue("granted").jsonPrimitive.boolean)
                check(claim("economy", "economy", "main").getValue("granted").jsonPrimitive.boolean)
                report("economy", 1, "WAITING_ROLE")
                report("economy", 2, "RUNNING")
                check(!runtime.executor.communicationState.jobs.getValue("economy").computing)
                check(claim("economy", "economy", "main").getValue("granted").jsonPrimitive.boolean)
                check(claim("economy", "economy", "wrong-parent")["error"] != null)
                report("main", 4, "WAITING_ROLE")
                report("main", 3, "INTERRUPTED", release = true) // stale accounting still releases authority
                check(runtime.executor.communicationState.jobs.getValue("main").telemetry!!.sequence == 4L)
                check(runtime.executor.communicationState.jobs.getValue("economy").expires == 0L)
                check(!operation("job_heartbeat", "economy").getValue("granted").jsonPrimitive.boolean)
                check(!claim("economy", "economy", "main").getValue("granted").jsonPrimitive.boolean)
                report("main", 5, "RUNNING")
                check(!runtime.executor.communicationState.jobs.getValue("main").computing)
                check(claim("expiring-main", "gameplay").getValue("granted").jsonPrimitive.boolean)
                check(claim("expiring-military", "military", "expiring-main").getValue("granted").jsonPrimitive.boolean)
                runtime.executor.accessGame {
                    val state = runtime.executor.communicationState
                    state.jobs["expiring-main"] = state.jobs.getValue("expiring-main").copy(expires = 1)
                    runtime.executor.telemetry.changed("expiring-main")
                }
                check(!operation("job_heartbeat", "expiring-military").getValue("granted").jsonPrimitive.boolean)
                api.operator(OperatorRequest("barrier", api.lease))
                fun recover(id: String) = api.operator(OperatorRequest("resume_job", leaseId = api.lease, epoch = runtime.epoch, civilization = "Rome", job = id))
                check(recover("expiring-military").jobRecovered == false)
                check(recover("expiring-main").jobRecovered == true)
                check(recover("expiring-military").jobRecovered == true)
                api.operator(OperatorRequest("retire_job", leaseId = api.lease, epoch = runtime.epoch, civilization = "Rome", job = "expiring-main"))
                check(runtime.executor.communicationState.jobs.getValue("expiring-military").expires == 0L)
                api.operator(OperatorRequest("unbarrier", api.lease))
                for (index in 0..3) {
                    val id = "consult-$index"
                    check(claim(id, "consultation").getValue("granted").jsonPrimitive.boolean)
                    report(id, 1, "WAITING_ROLE")
                }
                for (index in 0..3) report("consult-$index", 2, "RUNNING")
                check(runtime.executor.communicationState.jobs.values.count { it.computing && it.expires > System.currentTimeMillis() } == 0)
                val invocations = runtime.executor.communicationState.invocations
                val retire = OperatorRequest("retire_job", leaseId = api.lease, epoch = runtime.epoch, civilization = "Rome", job = "never-admitted")
                check(runCatching { api.operator(retire) }.isFailure)
                api.operator(OperatorRequest("barrier", api.lease))
                api.operator(retire)
                api.operator(retire)
                check(runtime.executor.communicationState.invocations == invocations)
                check("never-admitted" !in runtime.executor.communicationState.jobs)
                api.operator(OperatorRequest("unbarrier", api.lease))
                check(!claim("never-admitted", "consultation").getValue("granted").jsonPrimitive.boolean)
                runtime.executor.commitCheckpoint()
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            check("never-admitted" in runtime.executor.communicationState.retiredJobs)
            check("never-admitted" !in runtime.executor.communicationState.jobs)
            check(runtime.executor.communicationState.jobs.getValue("economy").expires == 0L)
        }
    }
}
