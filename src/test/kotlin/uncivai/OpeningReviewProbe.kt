package uncivai

import com.unciv.json.json
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import uncivai.player.PlayerHost
import java.nio.file.Path
import java.sql.DriverManager

object OpeningReviewProbe {
    fun run(root: Path) = HeadlessRuntime.createVanillaGame(root.resolve("game"), seed = 412L).use { runtime ->
        val civilizations = listOf("Rome", "Greece", "France")
        val tokens = civilizations.associateWith { it.first().toString().repeat(43) }
        runtime.executor.enableCheckpoints(root.resolve("game.json"))
        val epoch = runtime.epoch
        GameplayTestApi(runtime, root.resolve("tls"), tokens).use { api ->
            api.acquire()
            val gameBefore = json().toJson(runtime.game)
            for (civilization in civilizations) {
                val credential = tokens.getValue(civilization)
                val turn = api.turn(credential)
                check(turn.turn == 0 && turn.cities.isEmpty())
                check(turn.civilizationProfile.name == civilization && turn.civilizationProfile.ability.effects.isNotEmpty())
                check(turn.map.units.any { it.name == "Settler" })
                val player = runtime.game.getCivilization(civilization)
                check(turn.map.tiles.all { runtime.game.tileMap[it.position.x, it.position.y].isExplored(player) })
                // Opening capture reads these before the player has founded a city
                // or received its first gameplay activation.
                val victory = api.tool(credential, "victory_progress", obj("sections" to listOf("our_status", "global_status", "rankings", "charts", "demographics"), "fromTurn" to 0, "toTurn" to 0))
                check(victory.getValue("turn").jsonPrimitive.int == 0)
                for ((name, field) in listOf("research_details" to "technologies", "policy_details" to "policies")) {
                    val names = api.tool(credential, name).getValue(field).jsonArray.map { it.jsonObject.getValue("name").jsonPrimitive.content }
                    check(names.isNotEmpty())
                    for (batch in names.chunked(16)) {
                        val details = api.tool(credential, name, obj("names" to batch)).getValue(field).jsonArray
                        check(details.map { it.jsonObject.getValue("name").jsonPrimitive.content }.toSet() == batch.toSet())
                    }
                }
                val review = "opening-$civilization"
                check(api.communicate(credential, obj("op" to "job_claim", "epoch" to epoch, "job" to review, "purpose" to "review")).getValue("granted").jsonPrimitive.boolean)
                check(runtime.executor.communicationState.jobs.getValue(review).handoff == null)
            }
            check(json().toJson(runtime.game) == gameBefore) { "Opening observations or review permits changed gameplay" }
            check(runtime.executor.communicationState.jobs.values.count { it.expires > 0 } == 3)
            check(!api.communicate(tokens.getValue("Rome"), obj("op" to "job_claim", "epoch" to epoch, "job" to "over-budget", "purpose" to "review")).getValue("granted").jsonPrimitive.boolean)
            check(api.communicate(tokens.getValue("Greece"), obj("op" to "job_claim", "epoch" to epoch, "job" to "wrong-parent", "purpose" to "review", "parentJob" to "opening-Rome")).containsKey("error"))

            val report = JobTelemetry(1, "COMPLETED", 1000, 2000, 1000, 0, true, true, "test-model", "max", emptyMap())
            civilizations.forEachIndexed { index, civilization ->
                // Reviews finish before their owner's gameplay activation exists.
                api.communicate(tokens.getValue(civilization), obj("op" to "job_release", "epoch" to epoch, "job" to "opening-$civilization", "usage" to ModelUsage(30L + index, 0, 5, 0).recordJson(), "telemetry" to report.recordJson()))
            }
            for (civilization in civilizations) {
                val activation = api.activate()
                val job = activation.activationId!!
                val credential = tokens.getValue(civilization)
                api.communicate(credential, obj("op" to "job_claim", "epoch" to epoch, "job" to job, "purpose" to "gameplay"))
                api.finishTurn(activation.playerToken!!)
                api.communicate(credential, obj("op" to "job_release", "epoch" to epoch, "job" to job, "usage" to ModelUsage(10, 0, 1, 0).recordJson(), "telemetry" to report.recordJson()))
            }
            check(api.status().turn == 1)
            val rows = DriverManager.getConnection("jdbc:sqlite:${root.resolve("runner/monitoring.sqlite3")}").use { db ->
                db.prepareStatement("SELECT payload FROM monitor_records WHERE collection='turns'").use { statement ->
                    statement.executeQuery().use { results -> buildList { while (results.next()) add(Json.parseToJsonElement(results.getString(1)).jsonObject) } }
                }
            }
            check(rows.size == 3)
            civilizations.forEachIndexed { index, civilization ->
                val row = rows.single { it.getValue("turn").jsonObject.getValue("civilization").jsonPrimitive.content == civilization }
                check(row.getValue("usage").jsonObject.getValue("total_tokens").jsonPrimitive.long == 46L + index)
                check(row.getValue("accountingComplete").jsonPrimitive.boolean)
                check(row.getValue("jobs").toString().contains("opening-$civilization"))
            }
            check(api.communicate(tokens.getValue("Rome"), obj("op" to "job_claim", "epoch" to epoch, "job" to "late-opening", "purpose" to "review")).containsKey("error"))
            check(PlayerHost(runtime).bindPlayer("Greece").turn().civilizationProfile.name == "Greece")
        }
    }
}
