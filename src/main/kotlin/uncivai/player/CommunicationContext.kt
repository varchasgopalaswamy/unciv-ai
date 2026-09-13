package uncivai.player

import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerDiplomacyOperations
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import uncivai.GameExecutor
import uncivai.TradeScenarioSetup
import uncivai.VerbalBetrayalScenario
import uncivai.obj
import java.util.Collections

/** Small, player-bound UI facts for conversation; no map, action menus or foreign economy. */
internal fun communicationContext(runtime: GameExecutor, game: GameInfo, player: Civilization): JsonObject {
    val economy = observeEconomy(player)
    val diplomacy = PlayerDiplomacyOperations(player)
    return obj(
        "turn" to game.turns,
        "finished" to gameFinished(game),
        "defeated" to player.isDefeated(),
        "communicationAvailable" to !runtime.communicationPaused,
        "scenario" to communicationScenario(runtime, game, player)?.let { Json.encodeToJsonElement(GameplayScenario.serializer(), it) },
        "economy" to obj(
            "treasury" to economy.treasury,
            "goldPerTurn" to economy.goldPerTurn,
            "happiness" to economy.happiness,
            "resources" to economy.resources.map { obj("name" to it.name, "type" to it.type, "available" to it.available, "imported" to it.imported, "exported" to it.exported) },
        ),
        "knownCivilizations" to player.diplomacyFunctions.getKnownCivsSorted(includeCityStates = false).filter { it.isMajorCiv() }.mapNotNull { other ->
            val options = diplomacy.options(other) ?: return@mapNotNull null
            obj("civilizationId" to other.civID, "atWar" to player.isAtWarWith(other), "status" to options.status.name, "peaceTreatyTurns" to options.peaceTreatyTurns)
        }.toList(),
    )
}

/** One source of private communication roles for both gameplay and conversation views. */
internal fun communicationScenario(runtime: GameExecutor, game: GameInfo, player: Civilization): GameplayScenario? {
    fun scenario(name: String, phase: String, instructions: List<String>) = GameplayScenario(name, phase, Collections.unmodifiableList(instructions))
    return when (game.tileMap.mapParameters.name) {
        TradeScenarioSetup.MAP_NAME -> {
            val enacted = runtime.communicationState.trades.proposals.values.any { versions -> versions.last().let { player.civID in it.participants() && it.status == "ENACTED" } }
            scenario("ASYNC_TRADE_EXERCISE", if (enacted) "ENACTED" else "NEGOTIATING", TradeScenarioSetup.instructions(player.civID))
        }

        VerbalBetrayalScenario.MAP_NAME -> scenario(
            "DIPLOMACY_MEMORY_EXERCISE",
            if (player.diplomacyFunctions.getKnownCivsSorted(includeCityStates = false).any { it.isMajorCiv() && player.isAtWarWith(it) }) "AT_WAR" else "AT_PEACE",
            VerbalBetrayalScenario.instructions(player.civID),
        )

        "Asynchronous chat exercise" -> scenario(
            "ASYNC_CHAT_EXERCISE",
            "DEVELOP_AND_COMMUNICATE",
            listOf(
                "Chat exercise: remain at peace, found a capital, explore nearby, and keep research and production running. Each civilization should send one public introduction without mentions and one private greeting to the other player. Do this once; keep a concise STRATEGY note so later turns do not repeat it.",
                "Rome should offer Greece a social peace commitment: no attacks through turn 5, unconditional, kind=peace, expiresTurn=5. Greece should read the exact offer and explicitly accept it. Communication jobs can reply off turn. Do not wait or poll during gameplay, and do not claim that a social promise enacted an engine treaty.",
                "Record a concise private relationship assessment with sources and uncertainty, and an economic plan. Continue ordinary turns after the agreement. Player statements remain attributed claims, not observations of their army or economy.",
            ),
        )

        else -> null
    }
}
