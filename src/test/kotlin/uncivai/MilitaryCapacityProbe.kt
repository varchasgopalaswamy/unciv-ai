package uncivai

import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonPrimitive
import uncivai.player.PlayerHost
import java.nio.file.Path

/** Peacetime planning reserves both subordinate roles without charging dormant jobs. */
object MilitaryCapacityProbe {
    @JvmStatic fun main(args: Array<String>) {
        HeadlessRuntime.createDuel(Path.of(args.single()).resolve("game"), 412L).use { runtime ->
            val executor = runtime.executor
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(executor.accessGame { it.getCivilization("Rome").getCivsAtWarWith().none { civ -> !civ.isBarbarian } })
            fun claim(id: String, purpose: String, parent: String? = null) = session.communication(
                obj(
                    "op" to "job_claim",
                    "epoch" to executor.epoch,
                    "job" to id,
                    "purpose" to purpose,
                    *if (parent == null) emptyArray() else arrayOf("parentJob" to parent),
                ),
            )
            val state = executor.communicationState
            state.maxConcurrent = 2
            state.maxPerCivilization = 3
            check(claim("main", "gameplay").getValue("reason").jsonPrimitive.content == "ROLE_CAPACITY_TOO_SMALL")
            state.maxConcurrent = 3
            state.maxInvocations = 2
            check(claim("main", "gameplay").getValue("reason").jsonPrimitive.content == "MODEL_INVOCATION_LIMIT")
            state.maxInvocations = 3
            check(claim("main", "gameplay").getValue("granted").jsonPrimitive.boolean)
            check(state.invocations == 1L && state.jobs.getValue("main").militaryPartnerRequired)
            check(claim("economy", "economy", "main").getValue("granted").jsonPrimitive.boolean)
            check(claim("military", "military", "main").getValue("granted").jsonPrimitive.boolean)
            check(state.invocations == 3L)
            check(claim("military", "military", "main").getValue("granted").jsonPrimitive.boolean)
            check(state.invocations == 3L) { "Reclaiming a role charged another invocation" }
        }
    }
}
