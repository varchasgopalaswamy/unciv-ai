package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.models.ruleset.PerpetualConstruction
import kotlinx.serialization.Serializable

/** Operator-requested restriction on agent-controlled production choices. */
@Serializable
data class ProductionPolicy(val conversionUnlockEra: String, val blockedConversions: List<String>)

internal fun productionPolicy(player: Civilization): ProductionPolicy {
    val last = player.gameInfo.ruleset.eras.values.maxBy { it.eraNumber }
    return ProductionPolicy(last.name, if (player.getEraNumber() < last.eraNumber) listOf("Gold", "Science", "Culture") else emptyList())
}

internal fun productionAllowed(player: Civilization, name: String) = name !in productionPolicy(player).blockedConversions

/** Remove blocked legacy/automated entries, preserving work on ordinary items.
 * Runs at the player session boundary and before turn advancement, so automation
 * cannot collect conversion yields between API calls. Native AI seats are untouched.
 */
internal fun enforceProductionPolicy(player: Civilization): Boolean {
    val blocked = productionPolicy(player).blockedConversions
    if (blocked.isEmpty()) return false
    var changed = false
    for (city in player.cities) {
        val queue = city.cityConstructions
        if (queue.constructionQueue.none { it in blocked }) continue
        queue.constructionQueue.removeAll(blocked.toSet())
        if (queue.constructionQueue.isEmpty()) queue.constructionQueue.add(PerpetualConstruction.Idle.name)
        queue.currentConstructionIsUserSet = true
        city.cityStats.update()
        changed = true
    }
    return changed
}
