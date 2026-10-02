package uncivai.player

import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerDiplomacyOperations
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import uncivai.AgreementLifecycleScenarioSetup
import uncivai.GameExecutor
import uncivai.TradeCycleScenarioSetup
import uncivai.WarScenarioSetup
import uncivai.bilateralStatus
import uncivai.obj

/** Small, player-bound UI facts for conversation; no map, action menus or foreign economy. */
internal fun communicationContext(runtime: GameExecutor, game: GameInfo, player: Civilization): JsonObject {
    val economy = observeEconomy(player, includeDevelopment = false)
    val diplomacy = PlayerDiplomacyOperations(player)
    return obj(
        "turn" to game.turns,
        "rules" to Json.encodeToJsonElement(GameRules.serializer(), observeGameRules(game)),
        "finished" to gameFinished(game),
        "defeated" to player.isDefeated(),
        "communicationAvailable" to !runtime.communicationPaused,
        "canQueueMessage" to runtime.communication.canQueueMessage(player.civID),
        "scenario" to communicationScenario(runtime, game, player)?.let { Json.encodeToJsonElement(GameplayScenario.serializer(), it) },
        "economy" to obj(
            "treasury" to economy.treasury,
            "goldPerTurn" to economy.goldPerTurn,
            "happiness" to economy.happiness,
            "resources" to economy.resources.map { obj("name" to it.name, "type" to it.type, "available" to it.available, "imported" to it.imported, "exported" to it.exported) },
        ),
        "knownCivilizations" to player.diplomacyFunctions.getKnownCivsSorted(includeCityStates = false).filter { it.isMajorCiv() }.mapNotNull { other ->
            val options = diplomacy.options(other) ?: return@mapNotNull null
            JsonObject(bilateralStatus(player, other) + obj("civilizationId" to other.civID, "status" to options.status.name))
        }.toList(),
    )
}

/** One source of private communication roles for both gameplay and conversation views. */
internal fun communicationScenario(runtime: GameExecutor, game: GameInfo, player: Civilization): GameplayScenario? = when (game.tileMap.mapParameters.name) {
    uncivai.MixedDiplomacyScenario.MAP_NAME -> uncivai.MixedDiplomacyScenario.scenario(runtime, game, player)
    TradeCycleScenarioSetup.MAP_NAME -> TradeCycleScenarioSetup.scenario(runtime, game, player.civID)
    AgreementLifecycleScenarioSetup.MAP_NAME -> AgreementLifecycleScenarioSetup.scenario(runtime, game, player)
    else -> WarScenarioSetup.scenario(runtime, game, player)
}
