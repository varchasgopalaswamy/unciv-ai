package uncivai

import com.unciv.Constants
import com.unciv.json.json
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.diplomacy.DiplomaticStatus
import com.unciv.logic.trade.Trade
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import com.unciv.logic.trade.TradeRequest
import uncivai.player.PlayerHost
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path
import java.util.UUID

/** Raw writes construct adversarial worlds; player reads and commands stay on the bound facade. */
object DiplomacyCommandsProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        HeadlessRuntime.createDuel(Path.of(args.single()).resolve("data"), 412L).use { runtime ->
            val game = runtime.game
            val player = game.getCivilization("Rome")
            val other = game.getCivilization("Greece")
            val hidden = Civilization(game.ruleset.nations.getValue("Egypt")).also {
                it.gameInfo = game
                it.cache.updateState()
                game.civilizations.add(it)
                it.setTransients()
                it.tech.addTechnology("Agriculture")
            }
            other.diplomacyFunctions.makeCivilizationsMeet(hidden)
            player.diplomacyFunctions.makeCivilizationsMeet(other)
            player.popupAlerts.clear()
            other.popupAlerts.clear()
            val host = PlayerHost(runtime)
            val rome = host.bindPlayer(player.civID)
            val greece = host.bindPlayer(other.civID)
            fun raw() = json().toJson(game)
            fun rejectUnchanged(reason: PlayerSessionFailure, request: TurnCommandRequest) {
                val before = raw()
                expect(reason) { rome.execute(request) }
                check(before == raw()) { "Rejected ${request.kind} changed the game" }
            }

            val peace = rome.turn()
            check(peace.diplomacy.knownCivilizations.map { it.civilizationId } == listOf(other.civID))
            check(!peace.diplomacy.knownCivilizations.single().negotiatePeace.available)
            check(!player.knows(hidden))
            val beforeRead = raw()
            check(rome.turn() == peace && raw() == beforeRead)
            other.addGold(123456)
            hidden.addGold(654321)
            hidden.tech.techsToResearch.add("Pottery")
            other.units.getCivUnits().first().promotions.XP += 20
            hidden.tradeRequests.add(
                TradeRequest(
                    other.civID,
                    Trade().apply {
                        theirOffers.add(TradeOffer(Constants.flatGold, TradeOfferType.Gold, 47, game.speed))
                    },
                ),
            )
            check(rome.turn() == peace) { "Hidden third-party deal or peaceful rival treasury changed the actor's snapshot" }
            check(hidden.civID !in PlayerTurnJson.encode(peace))
            for (target in listOf(hidden.civID, "UNKNOWN-CIV-SENTINEL", player.civID)) {
                rejectUnchanged(
                    PlayerSessionFailure.OPTION_UNAVAILABLE,
                    request(rome.turn(), TurnCommandKind.DECLARE_WAR, target).copy(confirmWar = true),
                )
            }
            rejectUnchanged(
                PlayerSessionFailure.INVALID_ARGUMENT,
                request(rome.turn(), TurnCommandKind.DECLARE_WAR, other.civID),
            )

            // A receipt is not a reading of the updated confirmation and messages.
            val research = rome.turn()
            val changed = rome.execute(request(research, TurnCommandKind.SET_RESEARCH_QUEUE).copy(queue = listOf(research.researchOptions.first())))
            rejectUnchanged(
                PlayerSessionFailure.OBSERVATION_REQUIRED,
                TurnCommandRequest(
                    UUID.randomUUID().toString(),
                    changed.epoch,
                    changed.revision,
                    TurnCommandKind.DECLARE_WAR,
                    other.civID,
                    confirmWar = true,
                ),
            )
            val current = rome.turn()
            val stale = request(current, TurnCommandKind.DECLARE_WAR, other.civID).copy(confirmWar = true)
            runtime.executor.pause()
            val paused = rome.turn()
            check(!paused.diplomacy.knownCivilizations.single().declareWar.available)
            check(paused.diplomacy.knownCivilizations.single().declareWar.unavailableReasons.isNotEmpty())
            rejectUnchanged(PlayerSessionFailure.GAME_PAUSED, stale)
            runtime.executor.resume()
            rejectUnchanged(PlayerSessionFailure.STALE_REVISION, stale)
            val waiting = greece.turn()
            check(waiting.phase == "WAITING" && waiting.diplomacy.knownCivilizations.none { it.declareWar.available })
            val beforeWaiting = raw()
            expect(PlayerSessionFailure.NOT_YOUR_TURN) {
                greece.execute(request(waiting, TurnCommandKind.DECLARE_WAR, player.civID).copy(confirmWar = true))
            }
            check(raw() == beforeWaiting)

            // Enter a war whose negotiation cooldown has elapsed, independently of normal-flow tests.
            player.getDiplomacyManager(other)!!.diplomaticStatus = DiplomaticStatus.War
            other.getDiplomacyManager(player)!!.diplomaticStatus = DiplomaticStatus.War
            player.addGold(100)
            val war = rome.turn()
            check(war.diplomacy.knownCivilizations.single().negotiatePeace.available)
            check(greece.turn().diplomacy.knownCivilizations.single().negotiatePeace.available)
            check(war == PlayerTurnJson.json.decodeFromString<PlayerTurnSnapshot>(PlayerTurnJson.encode(war)))
            immutable(war.diplomacy.knownCivilizations.single().declarationConfirmation)
            immutable(war.diplomacy.knownCivilizations.single().declareWar.unavailableReasons)

            // Unsupported imported UI requests disclose categories, never hidden raw identifiers.
            val unsupported = TradeRequest(
                other.civID,
                Trade().apply {
                    ourOffers.add(TradeOffer("SECRET-CITY-ID", TradeOfferType.City, duration = -1))
                    theirOffers.add(TradeOffer(hidden.civID, TradeOfferType.WarDeclaration, duration = -1))
                },
            )
            player.tradeRequests.add(unsupported)
            val snapshot = rome.turn()
            val offered = snapshot.diplomacy.incomingRequests.single()
            check(offered.give.single().name == null && offered.receive.single().name == null)
            check(offered.message.isNotBlank() && !offered.unavailableReason.isNullOrBlank())
            check("SECRET-CITY-ID" !in PlayerTurnJson.encode(snapshot))
            check(hidden.civID !in PlayerTurnJson.encode(snapshot))
            check(!snapshot.canEndTurn && !snapshot.pending.single { it.kind == "TradeRequests" }.supported)
            immutable(offered.give)
            immutable(offered.receive)
            // Unknown senders cannot be resolved or disclosed by the observation.
            player.tradeRequests.add(TradeRequest("UNKNOWN-SENDER-SENTINEL", Trade()))
            val invalidSnapshot = rome.turn()
            check(invalidSnapshot.diplomacy.incomingRequests.size == 1)
            check("UNKNOWN-SENDER-SENTINEL" !in PlayerTurnJson.encode(invalidSnapshot))
        }
    }

    private fun request(snapshot: PlayerTurnSnapshot, kind: TurnCommandKind, target: String? = null) = TurnCommandRequest(UUID.randomUUID().toString(), snapshot.epoch, snapshot.revision, kind, target)

    private fun immutable(values: List<*>) {
        check(runCatching { (values as MutableList).clear() }.exceptionOrNull() is UnsupportedOperationException)
    }

    private fun expect(reason: PlayerSessionFailure, action: () -> Any?) {
        val failure = runCatching(action).exceptionOrNull()
        check(failure is PlayerSessionException && failure.reason == reason) { "Expected $reason, got $failure" }
        check(failure.cause == null && "SENTINEL" !in failure.message.orEmpty())
    }
}
