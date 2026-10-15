package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerCityManagementOperations
import com.unciv.logic.copyForPreview
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import uncivai.obj

/** Native citizen allocation on detached worlds. Never try a focus on the live city. */
internal fun inspectCityFocus(player: Civilization, state: TurnSessionState, turn: PlayerTurnSnapshot, target: String?): kotlinx.serialization.json.JsonObject {
    val city = player.cities.singleOrNull { state.cityHandles[it.id] == target }
        ?: inspectionFailure(PlayerSessionFailure.CITY_UNAVAILABLE)
    val observed = turn.cities.single { it.handle == target }
    val options = observed.management?.focusOptions.orEmpty().filter { it.action.available }
    val construction = city.cityConstructions.constructionQueue.firstOrNull()
    return obj(
        "city" to target,
        "name" to city.name,
        "available" to options.isNotEmpty(),
        "currentFocus" to city.getCityFocus().name,
        "options" to options.map { option ->
            // A fresh copy for each alternative preserves the same initial
            // locks, specialist assignments and neighbouring-city allocations.
            val game = player.gameInfo.copyForPreview()
            val owner = game.getCivilization(player.civID)
            val preview = owner.cities.single { it.id == city.id }
            val operations = PlayerCityManagementOperations(owner)
            val focus = operations.focuses(preview).single { it.name == option.name }
            check(operations.trySetFocus(preview, focus))
            val development = observeCityDevelopment(preview)
            obj(
                "name" to option.name,
                "label" to option.label,
                "command" to obj("kind" to "SET_CITY_FOCUS", "target" to target, "selection" to option.name),
                "yields" to JsonObject(development.yields.associate { it.stat to JsonPrimitive(it.amount) }),
                "foodPerTurn" to development.foodPerTurn,
                "turnsToGrowth" to development.turnsToGrowth,
                "turnsToStarvation" to development.turnsToStarvation,
                "construction" to construction,
                "constructionTurns" to construction?.let { constructionTurns(preview, it) },
                "avoidGrowth" to development.avoidGrowth,
                "manualSpecialists" to preview.manualSpecialists,
                "workedTiles" to development.tiles.filter { it.worked }.map { obj("x" to it.position.x, "y" to it.position.y) },
                "lockedTiles" to development.tiles.filter { it.locked }.map { obj("x" to it.position.x, "y" to it.position.y) },
                "specialists" to JsonObject(preview.population.getNewSpecialists().toSortedMap().mapValues { JsonPrimitive(it.value) }),
            )
        },
        "limitations" to listOf(
            "Each alternative applies the native focus operation independently at current population, preserving tile locks, growth settings and manual specialists.",
            "Construction estimates refer to the current queue head, excluding earlier or subsequent builds. No future population, improvements, enemy actions or combined construction/focus choice is simulated.",
            "Changing focus can reassign citizens even when its name matches the current focus. Keeping current assignments is a separate no-action choice.",
        ),
    )
}
