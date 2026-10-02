package uncivai

import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import uncivai.player.PlayerHost
import java.nio.file.Path

/** A human decision may outlast an agent deadline, including across process restart. */
object HumanNegotiationProbe {
    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args.single())
        val checkpoint = root.resolve("human.json")
        lateinit var trade: JsonObject
        HeadlessRuntime.createBilateralExercise(root.resolve("initial")).use { runtime ->
            runtime.executor.communicationState.humanPlayers.add("Greece")
            runtime.executor.communicationState.trades.timeoutSeconds = 1
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls")).use { api ->
                api.acquire()
                api.activate()
                trade = api.communicate(
                    api.playerToken("Rome"),
                    obj(
                        "op" to "trade_offer",
                        "epoch" to runtime.executor.epoch,
                        "requestId" to "human-offer",
                        "partner" to "Greece",
                        "give" to listOf(obj("type" to "Gold", "name" to "Gold", "amount" to 25, "duration" to -1)),
                        "receive" to emptyList<String>(),
                    ),
                ).getValue("trade").jsonObject
                check(trade["deadlineMillis"] == JsonNull)
                val later = System.currentTimeMillis() + 10_000_000L
                runtime.executor.clockMillis = { later }
                check(api.communicate(api.playerToken("Greece"), obj("op" to "trade_read", "epoch" to runtime.executor.epoch, "proposal" to trade["proposal"]))["trade"]!!.jsonObject.text("status") == "PENDING")
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("resumed")).use { runtime ->
            check(runtime.executor.communicationState.humanPlayers == setOf("Greece"))
            runtime.executor.clockMillis = { Long.MAX_VALUE / 2 }
            val human = PlayerHost(runtime).bindPlayer("Greece")
            val counter = human.communication(
                obj(
                    "op" to "trade_offer",
                    "epoch" to runtime.executor.epoch,
                    "requestId" to "human-counter",
                    "partner" to "Rome",
                    "proposal" to trade["proposal"],
                    "version" to trade["version"],
                    "give" to emptyList<String>(),
                    "receive" to listOf(obj("type" to "Gold", "name" to "Gold", "amount" to 30, "duration" to -1)),
                ),
            ).getValue("trade").jsonObject
            check(counter["deadlineMillis"] == JsonNull)
            val stale = runCatching { human.communication(obj("op" to "trade_decide", "epoch" to runtime.executor.epoch, "requestId" to "stale", "proposal" to trade["proposal"], "version" to trade["version"], "decision" to "accept")) }.exceptionOrNull()
            check(stale is uncivai.player.PlayerSessionException && stale.reason == uncivai.player.PlayerSessionFailure.STALE_REVISION)
            val agent = PlayerHost(runtime).bindPlayer("Rome")
            val request = obj("op" to "trade_decide", "epoch" to runtime.executor.epoch, "requestId" to "accept", "proposal" to counter["proposal"], "version" to counter["version"], "decision" to "accept")
            val accepted = agent.communication(request)
            check(accepted["outcome"] != null) { accepted }
            check(agent.communication(request) == accepted)
            check(runtime.game.getCivilization("Rome").gold == 970)
            check(runtime.game.getCivilization("Greece").gold == 1030)
        }
    }
}
