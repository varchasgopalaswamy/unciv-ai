package uncivai

import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path

/** Fixture and native boundary validation; live role decisions are audited separately. */
object ManagerScenarioProbe {
    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args[0])
        val variant = args[1]
        val checkpoint = root.resolve("checkpoint.json")
        HeadlessRuntime.createManagerExercise(root.resolve("initial"), variant).use { runtime ->
            check(runtime.game.tileMap.mapParameters.name == ManagerScenarioSetup.mapName(variant))
            check(runtime.game.getCivilization("France").isAI())
            for (name in listOf("Rome", "Greece")) {
                val player = runtime.game.getCivilization(name)
                check(player.isHuman() && player.cities.size == 2)
                check(player.tech.canBeResearched(player.tech.techsToResearch.first()))
            }
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                val token = checkNotNull(api.activate().playerToken)
                var before = api.readAlerts(token)
                check(before.scenario!!.name.endsWith("MANAGER_ACCEPTANCE"))
                check(before.scenario!!.instructions.any { "city_plan" in it })
                exerciseRoleTelemetry(api, runtime, variant == "military-manager")
                val epoch = runtime.executor.epoch
                val player = runtime.game.getCivilization("Rome")
                if (variant == "economy-manager") {
                    check(before.economy!!.committedGold == 480 && before.economy!!.availableGold == 20)
                    check(!player.isAtWar())
                    val capital = before.cities.single { it.name == player.cities.single { it.isOriginalCapital }.name }
                    api.reject(
                        token,
                        TurnCommandRequest(
                            "blocked-worker",
                            epoch,
                            before.revision,
                            TurnCommandKind.BUY_CONSTRUCTION,
                            target = capital.handle,
                            selection = "Worker",
                            confirmAction = true,
                        ),
                        "OPTION_UNAVAILABLE",
                    )
                    val proposal = runtime.executor.communicationState.trades.proposals.values.single().last()
                    val withdrawn = api.communicate(
                        api.playerToken("Rome"),
                        obj(
                            "op" to "trade_decide",
                            "epoch" to epoch,
                            "requestId" to "release-funds",
                            "proposal" to proposal.proposal,
                            "version" to proposal.version,
                            "decision" to "withdraw",
                        ),
                    )
                    check(withdrawn["error"] == null)
                    before = api.turn(token)
                    check(before.economy!!.committedGold == 0 && before.economy!!.availableGold == 500)
                    api.execute(
                        token,
                        TurnCommandRequest(
                            "buy-worker",
                            epoch,
                            before.revision,
                            TurnCommandKind.BUY_CONSTRUCTION,
                            target = capital.handle,
                            selection = "Worker",
                            confirmAction = true,
                        ),
                    )
                    check(player.units.getCivUnits().any { it.name == "Worker" })
                } else {
                    check(player.isAtWarWith(runtime.game.getCivilization("Greece")))
                    check(before.diplomacy.knownCivilizations.single { it.civilizationId == "Greece" }.negotiatePeace.available)
                    check(before.map.units.any { it.displayName.contains("Road Crew") && !it.military })
                    check(before.map.units.any { it.displayName.contains("Escort Guard") && it.military })
                    check(before.map.tiles.any { tile -> tile.visible && tile.units.any { it.civilization == "Greece" } })
                    val options = api.communicate(api.playerToken("Rome"), obj("op" to "trade_options", "epoch" to epoch, "partner" to "Greece"))
                    check(options["error"] == null)
                    val offer = api.communicate(
                        api.playerToken("Rome"),
                        obj(
                            "op" to "trade_offer",
                            "epoch" to epoch,
                            "requestId" to "offer-peace",
                            "partner" to "Greece",
                            "give" to listOf(obj("type" to "Treaty", "name" to "Peace Treaty", "amount" to 1, "duration" to runtime.game.speed.peaceDealDuration)),
                            "receive" to listOf(obj("type" to "Treaty", "name" to "Peace Treaty", "amount" to 1, "duration" to runtime.game.speed.peaceDealDuration)),
                        ),
                    )["trade"]!!.jsonObject
                    val accepted = api.communicate(
                        api.playerToken("Greece"),
                        obj(
                            "op" to "trade_decide",
                            "epoch" to epoch,
                            "requestId" to "accept-peace",
                            "proposal" to offer["proposal"],
                            "version" to offer["version"],
                            "decision" to "accept",
                        ),
                    )
                    check(accepted["error"] == null && !player.isAtWar())
                }
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("reloaded"), ManagerScenarioSetup.mapName(variant)).use { runtime ->
            check(runtime.game.getCivilization("Rome").cities.size == 2)
            check(runtime.game.getCivilization("France").isAI())
        }
    }

    /** Exercise the public schema and computing leases, not only in-process capacity helpers. */
    private fun exerciseRoleTelemetry(api: GameplayTestApi, runtime: HeadlessRuntime, military: Boolean) {
        val epoch = runtime.executor.epoch
        val credential = api.playerToken("Rome")
        val roles = if (military) listOf("gameplay", "economy", "military") else listOf("gameplay", "economy")
        fun claim(role: String) = api.communicate(
            credential,
            obj(
                "op" to "job_claim",
                "epoch" to epoch,
                "job" to "probe-$role",
                "purpose" to role,
                *if (role == "gameplay") emptyArray() else arrayOf("parentJob" to "probe-gameplay"),
            ),
        )
        for (role in roles) check(claim(role)["granted"] == JsonPrimitive(true))
        for (role in roles) {
            for ((index, phase) in listOf("RUNNING", "ROLE_FINISHED", "RUNNING", "WAITING_NEGOTIATION", "RUNNING").withIndex()) {
                if (phase == "RUNNING") check(claim(role)["granted"] == JsonPrimitive(true))
                val report = api.communicate(
                    credential,
                    obj(
                        "op" to "job_report",
                        "epoch" to epoch,
                        "job" to "probe-$role",
                        "usage" to obj("input_tokens" to 0, "cached_input_tokens" to 0, "output_tokens" to 0, "reasoning_output_tokens" to 0),
                        "telemetry" to obj(
                            "sequence" to index + 1, "phase" to phase, "startedAtMs" to 1,
                            "endedAtMs" to null, "elapsedMs" to index * 10, "negotiationWaitMs" to 0,
                            "usageReported" to false, "usageComplete" to false, "model" to null,
                            "reasoningEffort" to null, "modelRequests" to 0, "apiCalls" to obj(),
                        ),
                    ),
                )
                check(report["granted"] == JsonPrimitive(true)) { report.toString() }
                runtime.executor.accessGame {
                    check(runtime.executor.communicationState.jobs.getValue("probe-$role").computing == (phase == "RUNNING"))
                }
            }
        }
        for (role in roles.reversed()) {
            check(
                api.communicate(
                    credential,
                    obj(
                        "op" to "job_release",
                        "epoch" to epoch,
                        "job" to "probe-$role",
                    ),
                )["granted"] == JsonPrimitive(true),
            )
        }
        check(runtime.executor.communicationState.invocations == roles.size.toLong())
    }
}
