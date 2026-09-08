package uncivai.player

import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class ConfirmedTurnAction(val kind: TurnCommandKind, val description: String)

@Serializable
data class ReviewUnit(
    val handle: String,
    val name: String,
    val position: TilePosition,
    val movement: String,
    val idle: Boolean,
    val legalDestinations: Int,
)

/** Facts derived from the same permitted observation as the turn, never from model reports. */
@Serializable
data class TurnReview(val id: String, val actions: List<ConfirmedTurnAction>, val units: List<ReviewUnit>)

@Serializable
data class CompletedTurnReview(
    val turn: Int,
    val actions: List<ConfirmedTurnAction>,
    val deferredUnits: List<ReviewUnit>,
    val reason: String?,
)

@Serializable
data class GameplayScenario(val name: String, val phase: String)

internal fun TurnReview.deferredUnits(): List<ReviewUnit> = units.filter { it.idle && it.legalDestinations > 0 }

internal fun CompletedTurnReview.detached(): CompletedTurnReview = copy(
    actions = Collections.unmodifiableList(actions.toList()),
    deferredUnits = Collections.unmodifiableList(deferredUnits.toList()),
)

/** Include actual move endpoints, not the requested endpoint of a speculative path. */
internal fun confirmedAction(request: TurnCommandRequest, before: PlayerTurnSnapshot, after: PlayerTurnSnapshot, combat: CombatOutcome? = null): ConfirmedTurnAction {
    val unit = before.map.units.singleOrNull { it.handle == request.target }
    val city = before.cities.singleOrNull { it.handle == request.target }
    fun TilePosition.text() = "($x, $y)"
    val description = when (request.kind) {
        TurnCommandKind.MOVE_UNIT -> {
            val moved = after.map.units.singleOrNull { it.handle == request.target }
            if (moved == null) {
                // Entering a tile can trigger effects that remove or replace the original unit.
                "${unit!!.name} moved from ${unit.position.text()}; the original unit is no longer present. Inspect current units."
            } else {
                "${unit!!.name}: ${unit.position.text()} to ${moved.position.text()}; ${moved.movement} movement remaining."
            }
        }

        TurnCommandKind.FOUND_CITY -> {
            val founded = after.cities.single { candidate -> before.cities.none { it.handle == candidate.handle } }
            "Founded ${founded.name} at ${unit!!.position.text()}."
        }

        TurnCommandKind.ATTACK, TurnCommandKind.CITY_ATTACK -> {
            val name = unit?.name ?: city!!.name
            val outcome = checkNotNull(combat)
            if (!outcome.attacked) {
                val moved = after.map.units.singleOrNull { it.handle == request.target }
                "$name stopped ${moved?.position?.text() ?: "before completing the route"}; no attack occurred."
            } else {
                "$name attacked ${request.destination!!.text()}: dealt ${outcome.damageToDefender}, received ${outcome.damageToAttacker} damage." +
                    if (outcome.defenderCaptured) {
                        " Target captured."
                    } else if (outcome.defenderDestroyed) {
                        " Target destroyed."
                    } else {
                        ""
                    }
            }
        }

        TurnCommandKind.SET_UNIT_ORDER -> "${unit!!.name}: ${request.selection}."

        TurnCommandKind.PROMOTE_UNIT -> "${unit!!.name} gained ${request.selection}."

        TurnCommandKind.RESOLVE_CAPTURED_CITY -> "Resolved ${before.alerts.single { it.handle == request.target }.capture!!.title}: ${request.selection}."

        TurnCommandKind.MANAGE_CAPTURED_CITY -> "${city!!.name}: ${request.selection}."

        TurnCommandKind.ACKNOWLEDGE_ALERT -> {
            val alert = before.alerts.single { it.handle == request.target }
            "Read and acknowledged ${alert.content!!.title}."
        }

        TurnCommandKind.SET_RESEARCH_QUEUE -> "Research queue: ${after.researchQueue.joinToString(", ")}."

        TurnCommandKind.QUEUE_CONSTRUCTION, TurnCommandKind.REMOVE_CONSTRUCTION, TurnCommandKind.MOVE_CONSTRUCTION -> {
            val updated = after.cities.single { it.handle == request.target }
            "${updated.name} construction queue: ${updated.constructionQueue.joinToString(", ")}."
        }

        TurnCommandKind.RENAME_CITY -> "Renamed ${city!!.name} to ${after.cities.single { it.handle == city.handle }.name}."

        TurnCommandKind.DECLARE_WAR -> "Declared war on ${request.target}."

        TurnCommandKind.PROPOSE_PEACE -> "Proposed peace to ${request.target}."

        TurnCommandKind.RETRACT_PEACE -> "Retracted peace offer to ${request.target}."

        TurnCommandKind.ACCEPT_PEACE, TurnCommandKind.REJECT_PEACE, TurnCommandKind.COUNTER_PEACE -> {
            val offer = before.diplomacy.incomingOffers.single { it.handle == request.target }
            val verb = when (request.kind) {
                TurnCommandKind.ACCEPT_PEACE -> "Accepted"
                TurnCommandKind.REJECT_PEACE -> "Rejected"
                else -> "Countered"
            }
            "$verb peace offer from ${offer.civilizationId}."
        }

        TurnCommandKind.START_WORKER_JOB -> "${unit!!.name} started ${request.selection} at ${unit.position.text()}."

        TurnCommandKind.STOP_WORKER_JOB -> "${unit!!.name} stopped work at ${unit.position.text()}."

        TurnCommandKind.SET_WORKED_TILE -> "${city!!.name}: ${if (request.worked == true) "assigned" else "unassigned"} citizen at ${request.destination!!.text()}."

        TurnCommandKind.SET_TILE_LOCKED -> "${city!!.name}: ${if (request.locked == true) "locked" else "unlocked"} tile ${request.destination!!.text()}."

        TurnCommandKind.ADOPT_POLICY -> "Adopted ${request.selection}."

        TurnCommandKind.CHOOSE_FREE_TECHNOLOGY -> "Chose free technology ${request.selection}."

        TurnCommandKind.CHOOSE_FREE_GREAT_PERSON -> "Chose free ${request.selection}."

        TurnCommandKind.DISMISS_POLICY_PICKER -> "Deferred policy choice."

        TurnCommandKind.END_TURN -> "Ended turn."
    }
    return ConfirmedTurnAction(request.kind, description)
}
