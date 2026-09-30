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
data class GameplayScenario(val name: String, val phase: String, val instructions: List<String> = emptyList())

internal fun TurnReview.deferredUnits(): List<ReviewUnit> = units.filter { it.idle && it.legalDestinations > 0 }

internal fun CompletedTurnReview.detached(): CompletedTurnReview = copy(
    actions = Collections.unmodifiableList(actions.toList()),
    deferredUnits = Collections.unmodifiableList(deferredUnits.toList()),
)

/** Include actual move endpoints, not the requested endpoint of a speculative path. */
internal fun confirmedAction(request: TurnCommandRequest, before: PlayerTurnSnapshot, after: PlayerTurnSnapshot, combat: CombatOutcome? = null, economy: EconomyOutcome? = null): ConfirmedTurnAction {
    val unit = before.map.units.singleOrNull { it.handle == request.target }
    val city = before.cities.singleOrNull { it.handle == request.target }
    fun TilePosition.text() = "($x, $y)"
    val description = when (request.kind) {
        TurnCommandKind.MOVE_UNIT, TurnCommandKind.SET_UNIT_DESTINATION, TurnCommandKind.REBASE_UNIT, TurnCommandKind.PARADROP -> {
            val moved = after.map.units.singleOrNull { it.handle == request.target }
            if (moved == null) {
                // Entering a tile can trigger effects that remove or replace the original unit.
                "${unit!!.name} moved from ${unit.position.text()}; the original unit is no longer present. Inspect current units."
            } else {
                "${unit!!.name}: ${unit.position.text()} to ${moved.position.text()}; ${moved.movement} movement remaining." +
                    (if (moved.position != request.destination) " Requested destination ${request.destination!!.text()} not reached." else "") +
                    (if (moved.movementOrder != null) " Destination order remains active." else "")
            }
        }

        TurnCommandKind.FOUND_CITY -> {
            val founded = after.cities.single { candidate -> before.cities.none { it.handle == candidate.handle } }
            "Founded ${founded.name} at ${unit!!.position.text()}."
        }

        TurnCommandKind.CANCEL_UNIT_MOVEMENT -> "${unit!!.name}: cancelled destination order."

        TurnCommandKind.CREATE_WATER_IMPROVEMENT -> "${unit!!.name}: created ${request.selection} at ${unit.position.text()}; builder consumed."

        TurnCommandKind.AIR_SWEEP, TurnCommandKind.NUCLEAR_STRIKE ->
            "${unit!!.name}: ${request.kind.mission()} at ${request.destination!!.text()}; inspect mission receipt and combat reports for effects."

        TurnCommandKind.ATTACK, TurnCommandKind.CITY_ATTACK -> {
            val name = unit?.name ?: city!!.name
            val outcome = checkNotNull(combat)
            val target = (unit?.combat?.attacks ?: city?.attacks.orEmpty()).firstOrNull { it.destination == request.destination }?.defender ?: "target"
            if (!outcome.attacked) {
                val moved = after.map.units.singleOrNull { it.handle == request.target }
                "$name stopped ${moved?.position?.text() ?: "before completing the route"}; no attack on $target at ${request.destination!!.text()} occurred."
            } else {
                "$name attacked $target at ${request.destination!!.text()}: dealt ${outcome.damageToDefender}, received ${outcome.damageToAttacker} damage." +
                    (
                        if (outcome.defenderCaptured) {
                            " Target captured."
                        } else if (outcome.defenderDestroyed) {
                            " Target destroyed."
                        } else {
                            ""
                        }
                        ) + (if (outcome.attackerDestroyed) " $name destroyed." else " $name health ${outcome.attackerHealth}.")
            }
        }

        TurnCommandKind.SET_UNIT_ORDER -> "${unit!!.name}: ${request.selection}."

        TurnCommandKind.PROMOTE_UNIT -> {
            val choice = unit!!.combat!!.promotions!!.choices.single { it.name == request.selection }
            val promoted = after.map.units.single { it.handle == unit.handle }
            "${unit.name} selected ${request.selection}; health ${unit.health} to ${promoted.health}." +
                if (choice.retainedAfterSelection) " Promotion retained." else " One-time effect; promotion not retained."
        }

        TurnCommandKind.ADD_SPACESHIP_PART -> "Assembled ${unit!!.name} in the capital; consumed the unit."

        TurnCommandKind.CAST_DIPLOMATIC_VOTE -> "Voted for ${request.selection} as world leader."

        TurnCommandKind.ABSTAIN_DIPLOMATIC_VOTE -> "Abstained in the world leader election."

        TurnCommandKind.ACKNOWLEDGE_DIPLOMATIC_RESULTS -> "Reviewed the world leader election results."

        TurnCommandKind.RESOLVE_CAPTURED_CITY -> "Resolved ${before.alerts.single { it.handle == request.target }.capture!!.title}: ${request.selection}."

        TurnCommandKind.RESPOND_FRIENDSHIP -> "${request.selection} friendship with ${before.alerts.single { it.handle == request.target }.friendship!!.civilizationId}."

        TurnCommandKind.RESPOND_CITY_STATE_ALERT -> {
            val alert = before.alerts.single { it.handle == request.target }
            "${request.selection} response to ${alert.kind} from ${alert.nativeDiplomacy!!.civilizationId}."
        }

        TurnCommandKind.MANAGE_CAPTURED_CITY -> "${city!!.name}: ${request.selection}."

        TurnCommandKind.BUY_CONSTRUCTION -> "${city!!.name} purchased ${request.selection}; treasury changed by ${economy!!.goldChange} gold."

        TurnCommandKind.BUY_TILE -> "${city!!.name} purchased ${request.destination!!.text()}; treasury changed by ${economy!!.goldChange} gold."

        TurnCommandKind.UPGRADE_UNIT -> "${unit!!.name} upgraded to ${request.selection}; treasury changed by ${economy!!.goldChange} gold."

        TurnCommandKind.DISBAND_UNIT -> "Disbanded ${unit!!.name} at ${unit.position.text()}; treasury changed by ${economy!!.goldChange} gold."

        TurnCommandKind.ACTIVATE_GREAT_PERSON -> {
            val ability = unit!!.greatPerson!!.abilities.single { it.name == request.selection }
            "${unit.name}: ${ability.title} at ${unit.position.text()}." +
                if (after.map.units.none { it.handle == unit.handle }) " Unit consumed." else " Unit remains."
        }

        TurnCommandKind.PILLAGE -> {
            val remaining = after.map.units.singleOrNull { it.handle == request.target }
            "${unit!!.name} pillaged at ${unit.position.text()}; treasury changed by ${economy!!.goldChange} gold" +
                (remaining?.let { "; health ${unit.health} to ${it.health}, ${it.movement} movement remaining" } ?: "") + "."
        }

        TurnCommandKind.ACKNOWLEDGE_ALERT -> {
            val alert = before.alerts.single { it.handle == request.target }
            "Read and acknowledged ${alert.content!!.title}."
        }

        TurnCommandKind.DISMISS_NOTIFICATIONS -> "Dismissed ${request.targets.size} ordinary notifications."

        TurnCommandKind.SET_RESEARCH_QUEUE -> "Research queue: ${after.researchQueue.joinToString(", ")}."

        TurnCommandKind.QUEUE_CONSTRUCTION, TurnCommandKind.REMOVE_CONSTRUCTION, TurnCommandKind.MOVE_CONSTRUCTION -> {
            val updated = after.cities.single { it.handle == request.target }
            "${updated.name} construction queue: ${updated.constructionQueue.joinToString(", ")}."
        }

        TurnCommandKind.RENAME_CITY -> "Renamed ${city!!.name} to ${after.cities.single { it.handle == city.handle }.name}."

        TurnCommandKind.CITY_STATE_ACTION -> "${request.selection} with ${request.target}."

        TurnCommandKind.GIFT_UNIT -> "Gifted ${unit!!.name} to ${unit.cityStateGift!!.civilizationId}."

        TurnCommandKind.DECLARE_WAR -> "Declared war on ${request.target}."

        TurnCommandKind.START_WORKER_JOB -> {
            val work = after.map.units.single { it.handle == request.target }.worker!!
            "${unit!!.name} at ${unit.position.text()}: working on ${work.currentJob}; queue " +
                work.queue.joinToString(" then ") { "${it.name} (${it.remainingTurns} turns)" } + "."
        }

        TurnCommandKind.STOP_WORKER_JOB -> "${unit!!.name} stopped work at ${unit.position.text()}."

        TurnCommandKind.SET_WORKED_TILE -> "${city!!.name}: ${if (request.worked == true) "assigned" else "unassigned"} citizen at ${request.destination!!.text()}."

        TurnCommandKind.SET_TILE_LOCKED -> "${city!!.name}: ${if (request.locked == true) "locked" else "unlocked"} tile ${request.destination!!.text()}."

        TurnCommandKind.SET_CITY_FOCUS -> "${city!!.name}: citizen focus ${request.selection}."

        TurnCommandKind.SET_AVOID_GROWTH -> "${city!!.name}: avoid growth ${request.enabled}."

        TurnCommandKind.SET_MANUAL_SPECIALISTS -> "${city!!.name}: manual specialists ${request.enabled}."

        TurnCommandKind.ASSIGN_SPECIALIST, TurnCommandKind.UNASSIGN_SPECIALIST -> {
            val assigned = after.cities.single { it.handle == request.target }.management!!.specialists.single { it.name == request.selection }.assigned
            "${city!!.name}: ${request.selection} assignment is now $assigned."
        }

        TurnCommandKind.RESET_CITIZENS -> "${city!!.name}: reset citizen assignments and tile locks."

        TurnCommandKind.SELL_BUILDING -> "${city!!.name} sold ${request.selection}; treasury changed by ${economy!!.goldChange} gold."

        TurnCommandKind.REASSIGN_CITY_TILE -> "${city!!.name} now works ${request.destination!!.text()}, released by another owned city."

        TurnCommandKind.ADOPT_POLICY -> "Adopted ${request.selection}."

        TurnCommandKind.CHOOSE_FREE_TECHNOLOGY -> "Chose free technology ${request.selection}."

        TurnCommandKind.CHOOSE_FREE_GREAT_PERSON -> "Chose free ${request.selection}."

        TurnCommandKind.DISMISS_POLICY_PICKER -> "Deferred policy choice."

        TurnCommandKind.END_TURN -> "Ended turn."
    }
    return ConfirmedTurnAction(request.kind, description)
}
