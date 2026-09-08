package uncivai

import com.unciv.json.json
import kotlinx.serialization.Serializable
import uncivai.player.CompletedTurnReview
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandReceipt
import uncivai.player.TurnCommandRequest
import uncivai.player.confirmedAction
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

@Serializable
private data class ReviewExpected(
    val snapshot: PlayerTurnSnapshot,
    val request: TurnCommandRequest,
    val receipt: TurnCommandReceipt? = null,
    val completed: CompletedTurnReview? = null,
)

/** A new process for every stage exercises durable facts separately from the observation gate. */
object TurnReviewProbe {
    private const val DEFER_REASON = "Hold the Scout and Warrior in place while establishing the capital."

    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args[0])
        when (args[1]) {
            "prepare" -> prepare(root)
            "complete" -> complete(root)
            "verify" -> verify(root)
            else -> error("Unknown turn review probe stage")
        }
    }

    private fun prepare(root: Path) {
        HeadlessRuntime.createDuel(root.resolve("initial"), 42L, closeStarts = true).use { runtime ->
            runtime.executor.enableCheckpoints(root.resolve("checkpoint.json"))
            val host = PlayerHost(runtime)
            val rome = host.bindPlayer("Rome")
            val greece = host.bindPlayer("Greece")
            val initial = rome.turn()
            check(initial.turnReview!!.actions.isEmpty() && initial.previousTurnReview == null)
            check(greece.turn().turnReview == null && greece.turn().previousTurnReview == null)
            checkReviewMatchesUnits(initial)
            checkMovementDescriptions(initial)
            val completed = prepareCity(rome)
            var snapshot = rome.turn()
            val review = checkNotNull(snapshot.turnReview)
            check(snapshot.canEndTurn)
            check(review.units.map { it.name }.sorted() == listOf("Scout", "Warrior")) { "Founding retained the consumed Settler in the review" }
            check(review.units.all { it.idle && it.legalDestinations > 0 }) { "Founding hid remaining unit moves" }
            check(review.actions.map { it.kind } == completed.map { it.first.kind })
            check(review.actions.all { it.description.isNotBlank() })
            check(review.actions.single { it.kind == TurnCommandKind.FOUND_CITY }.description.contains("Rome"))
            checkReviewMatchesUnits(snapshot)
            checkFrozen(review.actions)
            checkFrozen(review.units)

            val beforeRejected = json().toJson(runtime.game)
            expect(PlayerSessionFailure.TURN_REVIEW_REQUIRED) { rome.execute(command(snapshot, TurnCommandKind.END_TURN)) }
            expect(PlayerSessionFailure.TURN_REVIEW_REQUIRED) {
                rome.execute(command(snapshot, TurnCommandKind.END_TURN).copy(reviewId = review.id))
            }
            expect(PlayerSessionFailure.OPTION_UNAVAILABLE) {
                rome.execute(command(snapshot, TurnCommandKind.QUEUE_CONSTRUCTION, snapshot.cities.single().handle, "Unavailable construction"))
            }
            check(json().toJson(runtime.game) == beforeRejected)
            check(rome.turn().turnReview == review) { "Rejected commands polluted the review" }
            for ((request, receipt) in completed) check(rome.execute(request) == receipt)
            check(rome.turn().turnReview == review) { "Receipt replay duplicated confirmed actions" }

            val rename = command(snapshot, TurnCommandKind.RENAME_CITY, snapshot.cities.single().handle, "Roma Review")
            val renamed = rome.execute(rename)
            val staleReview = command(snapshot, TurnCommandKind.END_TURN).copy(revision = renamed.revision, reviewId = review.id, deferReason = DEFER_REASON)
            expect(PlayerSessionFailure.OBSERVATION_REQUIRED) { rome.execute(staleReview) }
            snapshot = rome.turn()
            check(snapshot.turnReview!!.id != review.id)
            expect(PlayerSessionFailure.TURN_REVIEW_REQUIRED) { rome.execute(staleReview) }
            check(snapshot.turnReview.actions.map { it.kind } == completed.map { it.first.kind } + TurnCommandKind.RENAME_CITY)
            check(snapshot.turnReview.actions.last().description.contains("Roma Review"))
            val end = command(snapshot, TurnCommandKind.END_TURN).copy(reviewId = snapshot.turnReview.id, deferReason = DEFER_REASON)
            save(root, ReviewExpected(snapshot, end))
        }
    }

    private fun complete(root: Path) {
        val saved = expected(root)
        HeadlessRuntime.loadCheckpoint(root.resolve("checkpoint.json"), root.resolve("restored")).use { runtime ->
            val host = PlayerHost(runtime)
            val rome = host.bindPlayer("Rome")
            val greece = host.bindPlayer("Greece")
            expect(PlayerSessionFailure.OBSERVATION_REQUIRED) { rome.execute(saved.request) }
            val beforeEnd = rome.turn()
            check(beforeEnd.turnReview == saved.snapshot.turnReview) { "Restart lost the current review" }
            val receipt = rome.execute(saved.request)
            val afterEnd = json().toJson(runtime.game)
            check(rome.execute(saved.request) == receipt)
            check(json().toJson(runtime.game) == afterEnd) { "Review acknowledgment replay advanced a second turn" }
            check(rome.turn().turnReview == null)
            val completed = checkNotNull(rome.turn().previousTurnReview)
            check(completed.turn == saved.snapshot.turn)
            check(completed.actions == saved.snapshot.turnReview!!.actions)
            check(completed.deferredUnits == saved.snapshot.turnReview.units)
            check(completed.reason == DEFER_REASON)
            check(greece.turn().previousTurnReview == null) { "Another player received Rome's review" }
            check(greece.turn().turnReview!!.actions.isEmpty())

            prepareCity(greece)
            // Consuming the remaining movement removes the need for a defer reason.
            repeat(16) {
                val turn = greece.turn()
                val unit = turn.map.units.firstOrNull { it.idle && it.moves.isNotEmpty() } ?: return@repeat
                val move = unit.moves.filterNot { it.speculative }.maxByOrNull { it.path.size } ?: unit.moves.first()
                greece.execute(command(turn, TurnCommandKind.MOVE_UNIT, unit.handle).copy(destination = move.destination))
                val after = greece.turn()
                val moved = after.map.units.single { it.handle == unit.handle }
                val action = after.turnReview!!.actions.last()
                check(action.kind == TurnCommandKind.MOVE_UNIT && action.description.contains("(${moved.position.x}, ${moved.position.y})"))
            }
            val greekEnd = greece.turn()
            check(greekEnd.turnReview!!.units.none { it.idle && it.legalDestinations > 0 })
            greece.execute(command(greekEnd, TurnCommandKind.END_TURN).copy(reviewId = greekEnd.turnReview.id))
            val nextRome = rome.turn()
            check(nextRome.turn == saved.snapshot.turn + 1 && nextRome.phase == "ACTIVE")
            check(nextRome.previousTurnReview == completed && nextRome.turnReview!!.actions.isEmpty())
            check(greece.turn().previousTurnReview!!.let { it.reason == null && it.deferredUnits.isEmpty() })
            save(root, saved.copy(receipt = receipt, completed = completed))
        }
    }

    private fun verify(root: Path) {
        val saved = expected(root)
        HeadlessRuntime.loadCheckpoint(root.resolve("checkpoint.json"), root.resolve("completed-restored")).use { runtime ->
            val rome = PlayerHost(runtime).bindPlayer("Rome")
            val beforeReplay = json().toJson(runtime.game)
            check(rome.execute(saved.request) == saved.receipt) { "Restart blocked a committed acknowledgment replay" }
            check(json().toJson(runtime.game) == beforeReplay)
            val current = rome.turn()
            check(current.previousTurnReview == saved.completed)
            check(current.turnReview!!.actions.isEmpty())
            checkFrozen(current.previousTurnReview!!.actions)
            checkFrozen(current.previousTurnReview.deferredUnits)
            expect(PlayerSessionFailure.STALE_REVISION) { rome.execute(saved.request.copy(requestId = UUID.randomUUID().toString())) }
        }
    }

    private fun prepareCity(player: PlayerSession): List<Pair<TurnCommandRequest, TurnCommandReceipt>> {
        val completed = mutableListOf<Pair<TurnCommandRequest, TurnCommandReceipt>>()
        repeat(20) {
            val turn = player.turn()
            val alert = turn.alerts.firstOrNull { it.canAcknowledge }
            val settler = turn.map.units.firstOrNull { it.canFoundCity }
            val city = turn.cities.firstOrNull { it.constructionQueue.isEmpty() }
            val request = when {
                alert != null -> command(turn, TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle)
                settler != null -> command(turn, TurnCommandKind.FOUND_CITY, settler.handle)
                turn.researchQueue.isEmpty() -> command(turn, TurnCommandKind.SET_RESEARCH_QUEUE).copy(queue = listOf(turn.researchOptions.first()))
                city != null -> command(turn, TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, city.constructionOptions.first())
                turn.canEndTurn -> return completed
                else -> error("Unexpected starting decision: ${turn.pending}")
            }
            completed += request to player.execute(request)
        }
        error("Starting decisions did not finish")
    }

    private fun checkReviewMatchesUnits(turn: PlayerTurnSnapshot) {
        val review = checkNotNull(turn.turnReview)
        check(review.units.map { it.handle } == turn.map.units.map { it.handle })
        for (unit in review.units) {
            val mapUnit = turn.map.units.single { it.handle == unit.handle }
            check(unit.name == mapUnit.name && unit.position == mapUnit.position && unit.movement == mapUnit.movement)
            check(unit.idle == mapUnit.idle && unit.legalDestinations == mapUnit.moves.size)
        }
    }

    private fun checkMovementDescriptions(before: PlayerTurnSnapshot) {
        val warrior = before.map.units.single { it.name == "Warrior" }
        val requested = warrior.moves.first { it.destination != warrior.position }.destination
        val request = command(before, TurnCommandKind.MOVE_UNIT, warrior.handle).copy(destination = requested)
        // Reporting must handle an original unit disappearing without guessing its endpoint.
        val disappeared = before.copy(map = before.map.copy(units = before.map.units.filterNot { it.handle == warrior.handle }))
        val unavailable = confirmedAction(request, before, disappeared)
        check(unavailable.kind == TurnCommandKind.MOVE_UNIT && unavailable.description.contains(warrior.name))
        check(!unavailable.description.contains("(${requested.x}, ${requested.y})"))
        // A speculative move may stop before its requested destination: report the observed tile.
        val partial = confirmedAction(request, before, before)
        check(partial.description.contains("(${warrior.position.x}, ${warrior.position.y})"))
        check(!partial.description.contains("(${requested.x}, ${requested.y})"))
    }

    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null, selection: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), turn.epoch, turn.revision, kind, target, selection)

    private fun expect(reason: PlayerSessionFailure, action: () -> Any?) {
        val failure = runCatching(action).exceptionOrNull()
        check(failure is PlayerSessionException && failure.reason == reason) { "Expected $reason, got $failure" }
    }

    private fun checkFrozen(values: List<*>) {
        check(runCatching { (values as MutableList<*>).clear() }.exceptionOrNull() is UnsupportedOperationException) { "A review exposed mutable state" }
    }

    private fun save(root: Path, value: ReviewExpected) = Files.writeString(root.resolve("expected.json"), PlayerTurnJson.json.encodeToString(value))
    private fun expected(root: Path) = PlayerTurnJson.json.decodeFromString<ReviewExpected>(Files.readString(root.resolve("expected.json")))
}
