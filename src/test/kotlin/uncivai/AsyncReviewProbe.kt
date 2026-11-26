package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path
import java.sql.DriverManager

/** Frozen reviews run between turns without obtaining gameplay authority. */
object AsyncReviewProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        val checkpoint = root.resolve("game.json")
        lateinit var retained: CommunicationJob
        var invocations = 0L
        HeadlessRuntime.createDuel(root.resolve("initial"), 419L).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                val epoch = runtime.epoch
                val rome = api.playerToken("Rome")
                val greece = api.playerToken("Greece")
                fun claim(token: String, id: String, purpose: String, review: ReviewJobBinding? = null) = api.communicate(
                    token,
                    obj(
                        "op" to "job_claim",
                        "epoch" to epoch,
                        "job" to id,
                        "purpose" to purpose,
                        *if (review == null) emptyArray() else arrayOf("review" to review.recordJson()),
                    ),
                )
                fun granted(token: String, id: String, purpose: String, review: ReviewJobBinding? = null) = claim(token, id, purpose, review).getValue("granted").jsonPrimitive.boolean
                fun release(token: String, id: String) = api.communicate(
                    token,
                    obj("op" to "job_release", "epoch" to epoch, "job" to id),
                )

                val initial = api.activate()
                check(granted(rome, "main-0", "gameplay"))
                val opening = ReviewJobBinding("a".repeat(64), 0, 0)
                check(granted(greece, "opening", "review", opening))
                check(!runtime.executor.isPaused() && !runtime.executor.communicationPaused)
                check(runtime.executor.communicationState.jobs.getValue("opening").review == opening)
                check(!granted(rome, "extra", "review", ReviewJobBinding("b".repeat(64), 0, 0)))
                check(claim(greece, "opening", "review", opening.copy(targetTurn = 1)).containsKey("error"))
                check(claim(rome, "wrong-owner", "review", opening).containsKey("error"))
                check(claim(greece, "wrong-cycle", "review", opening.copy(targetTurn = 1)).containsKey("error"))
                check(claim(greece, "wrong-purpose", "communication", opening).containsKey("error"))
                check(
                    api.communicate(
                        greece,
                        obj(
                            "op" to "job_claim",
                            "epoch" to epoch,
                            "job" to "parented-review",
                            "purpose" to "review",
                            "parentJob" to "main-0",
                            "review" to opening.recordJson(),
                        ),
                    ).containsKey("error"),
                )
                release(greece, "opening")
                release(rome, "main-0")
                api.finishTurn(initial.playerToken!!)
                val second = api.activate()
                api.finishTurn(second.playerToken!!)
                check(api.status().turn == 1)
                api.activate()

                val between = ReviewJobBinding("c".repeat(64), 0, 1)
                check(granted(greece, "between", "review", between))
                check(!runtime.executor.isPaused() && !runtime.executor.communicationPaused)
                check(claim(greece, "legacy-late", "review").containsKey("error"))
                check(claim(rome, "future", "review", ReviewJobBinding("d".repeat(64), 2, 3)).containsKey("error"))
                check(claim(rome, "stale", "review", ReviewJobBinding("e".repeat(64), 0, 0)).containsKey("error"))
                val turn = api.turn(greece)
                check(turn.phase == "WAITING")
                api.reject(
                    greece,
                    TurnCommandRequest(
                        "review-cannot-act",
                        epoch,
                        turn.revision,
                        TurnCommandKind.SET_RESEARCH_QUEUE,
                        queue = listOf(turn.researchOptions.first()),
                    ),
                    "COMMAND_UNAVAILABLE",
                )

                val usage = ModelUsage(40, 10, 5, 0)
                val report = JobTelemetry(
                    1, "WAITING_ROLE", 1000, null, 500, 0, true, false,
                    "review-model", "medium", emptyMap(), modelRequests = 2,
                )
                api.communicate(
                    greece,
                    obj(
                        "op" to "job_report",
                        "epoch" to epoch,
                        "job" to "between",
                        "usage" to usage.recordJson(),
                        "telemetry" to report.recordJson(),
                    ),
                )
                runtime.executor.telemetry.flushMonitoring()
                val greekOpening = DriverManager.getConnection("jdbc:sqlite:${root.resolve("runner/monitoring.sqlite3")}").use { db ->
                    db.prepareStatement("SELECT payload FROM monitor_records WHERE collection='turns'").use { statement ->
                        statement.executeQuery().use { rows -> buildList { while (rows.next()) add(Json.parseToJsonElement(rows.getString(1)).jsonObject) } }
                    }
                }.single { it.getValue("turn").jsonObject.getValue("civilization").jsonPrimitive.content == "Greece" }
                check(greekOpening.getValue("usage").jsonObject.getValue("total_tokens").jsonPrimitive.long == 0L)
                check(!greekOpening.getValue("jobs").toString().contains("between"))
                check(runtime.executor.telemetry.usageSnapshot.getValue("input_tokens") == 40L)
                val before = runtime.executor.communicationState.invocations
                check(granted(greece, "between", "review", between))
                check(runtime.executor.communicationState.invocations == before)
                api.communicate(
                    greece,
                    obj(
                        "op" to "job_report",
                        "epoch" to epoch,
                        "job" to "between",
                        "usage" to usage.recordJson(),
                        "telemetry" to report.recordJson(),
                    ),
                )
                runtime.executor.communicationState.maxInvocations = before
                val limited = claim(rome, "limited", "review", ReviewJobBinding("f".repeat(64), 1, 2))
                check(limited["reason"]?.jsonPrimitive?.content == "MODEL_INVOCATION_LIMIT")
                api.operator(OperatorRequest("barrier", api.lease))
                retained = runtime.executor.communicationState.jobs.getValue("between")
                invocations = runtime.executor.communicationState.invocations
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restart")).use { runtime ->
            GameplayTestApi(runtime, root.resolve("restart-tls")).use { api ->
                api.acquire()
                val stored = runtime.executor.communicationState.jobs.getValue("between")
                check(stored == retained && stored.parentJob == null && stored.review != null)
                val token = api.playerToken("Greece")
                val request = obj(
                    "op" to "job_claim",
                    "epoch" to runtime.epoch,
                    "job" to "between",
                    "purpose" to "review",
                    "review" to stored.review.recordJson(),
                )
                check(api.communicate(token, request).getValue("granted").jsonPrimitive.boolean)
                check(runtime.executor.communicationState.invocations == invocations)
                check(runtime.executor.communicationState.jobs.getValue("between").reportedUsage == retained.reportedUsage)
                api.communicate(token, obj("op" to "job_release", "epoch" to runtime.epoch, "job" to "between"))
                check(!api.communicate(token, request).getValue("granted").jsonPrimitive.boolean)
            }
        }
    }
}
