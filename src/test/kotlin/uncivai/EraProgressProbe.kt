package uncivai

import com.unciv.json.json
import kotlinx.serialization.json.jsonObject
import uncivai.player.EraProgress
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.communicationContext
import java.nio.file.Path

object EraProgressProbe {
    fun run(root: Path) = HeadlessRuntime.createVanillaGame(root.resolve("game"), seed = 412L, cityStates = 1).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        val rival = game.getCivilization("Greece")
        val minor = game.civilizations.single { it.isCityState }
        val session = PlayerHost(runtime).bindPlayer("Rome")
        check(!player.knows(rival))
        rival.tech.addTechnology("Plastics", false)
        check(rival.tech.era.name == "Modern era")
        player.diplomacyFunctions.makeCivilizationsMeet(minor)
        minor.tech.addTechnology("Plastics", false)
        val before = json().toJson(game)
        val early = session.turn().eraProgress!!
        check(!early.modernOrLater && early.highestKnownMajor == player.tech.era.name) {
            "An unmet rival or known city-state must not trigger the major-civilization threshold"
        }
        check(json().toJson(game) == before) { "Era observation changed the game" }
        player.tech.addTechnology("Steam Power", false)
        check(session.turn().eraProgress!!.let { it.current == "Industrial era" && !it.modernOrLater })
        player.diplomacyFunctions.makeCivilizationsMeet(rival)
        val modern = session.turn().eraProgress!!
        check(modern == EraProgress("Industrial era", "Modern era", true))
        val context = communicationContext(runtime.executor, game, player)
        check(PlayerTurnJson.json.decodeFromJsonElement(EraProgress.serializer(), context.getValue("eraProgress")) == modern)
        GameplayTestApi(runtime, root.resolve("tls")).use { api ->
            val token = api.playerToken("Rome")
            check(api.turn(token).eraProgress == modern)
            val social = api.communicate(token, obj("op" to "state", "epoch" to runtime.executor.epoch))
            check(social.getValue("context").jsonObject.getValue("eraProgress") == context.getValue("eraProgress"))
        }
        // Later eras remain enabled, and an earlier detached snapshot is stable.
        rival.tech.addTechnology("Future Tech", false)
        check(session.turn().eraProgress!!.let { it.modernOrLater && it.highestKnownMajor == "Future era" })
        check(!early.modernOrLater && modern.highestKnownMajor == "Modern era")
        player.tech.addTechnology("Plastics", false)
        check(PlayerHost(runtime).bindPlayer("France").turn().eraProgress!!.modernOrLater.not())
        game.getCivilization("France").tech.addTechnology("Plastics", false)
        check(PlayerHost(runtime).bindPlayer("France").turn().eraProgress!!.modernOrLater)
        check(session.turn().eraProgress!!.current == "Modern era")
    }
}
