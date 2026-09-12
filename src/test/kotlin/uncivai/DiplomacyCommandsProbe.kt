package uncivai

import com.unciv.Constants
import com.unciv.json.json
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.diplomacy.DiplomaticStatus
import com.unciv.logic.trade.Trade
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import com.unciv.logic.trade.TradeRequest
import uncivai.player.PeaceTerm
import uncivai.player.PeaceTermSelection
import uncivai.player.PeaceTerms
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
            check(peace.diplomacy.knownCivilizations.single().peaceOptions == null)
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
            val options = checkNotNull(war.diplomacy.knownCivilizations.single().peaceOptions)
            check(war.diplomacy.knownCivilizations.single().negotiatePeace.available)
            val ourTreaty = options.give.single { it.type == "Treaty" && it.name == Constants.peaceTreaty }.selection()
            val theirTreaty = options.receive.single { it.type == "Treaty" && it.name == Constants.peaceTreaty }.selection()
            val ourGold = options.give.single { it.type == "Gold" }.selection().copy(amount = 20)
            val terms = PeaceTerms(listOf(ourTreaty, ourGold), listOf(theirTreaty))
            check(war == PlayerTurnJson.json.decodeFromString<PlayerTurnSnapshot>(PlayerTurnJson.encode(war)))
            immutable(options.give)
            immutable(options.receive)
            immutable(war.diplomacy.knownCivilizations.single().declarationConfirmation)
            immutable(war.diplomacy.knownCivilizations.single().declareWar.unavailableReasons)
            val goldBefore = options.receive.single { it.type == "Gold" }.amount
            other.addGold(17)
            val changedWealth = rome.turn()
            check(changedWealth.revision != war.revision)
            check(changedWealth.diplomacy.knownCivilizations.single().peaceOptions!!.receive.single { it.type == "Gold" }.amount == goldBefore + 17)
            check(options.receive.single { it.type == "Gold" }.amount == goldBefore) { "A detached offer changed after an engine write" }
            rejectUnchanged(
                PlayerSessionFailure.STALE_REVISION,
                request(war, TurnCommandKind.PROPOSE_PEACE, other.civID).copy(peaceTerms = terms),
            )

            for (bad in listOf(
                terms.copy(give = emptyList()),
                terms.copy(give = listOf(ourTreaty, ourTreaty)),
                terms.copy(give = listOf(ourTreaty, ourGold.copy(amount = 0))),
                terms.copy(give = listOf(ourTreaty, ourGold.copy(amount = -1))),
                terms.copy(give = listOf(ourTreaty, ourGold.copy(type = "NOT_A_TRADE_TYPE"))),
            )) {
                rejectUnchanged(
                    PlayerSessionFailure.INVALID_ARGUMENT,
                    request(rome.turn(), TurnCommandKind.PROPOSE_PEACE, other.civID).copy(peaceTerms = bad),
                )
            }
            for (bad in listOf(
                terms.copy(give = listOf(ourGold)),
                terms.copy(give = listOf(ourTreaty.copy(amount = 2))),
                terms.copy(give = listOf(ourTreaty.copy(duration = Int.MAX_VALUE))),
                terms.copy(give = listOf(ourTreaty, ourGold.copy(amount = Int.MAX_VALUE))),
                terms.copy(give = listOf(ourTreaty, ourGold.copy(type = "City", name = "SECRET-CITY-ID"))),
                terms.copy(receive = listOf(theirTreaty, ourGold.copy(type = "WarDeclaration", name = hidden.civID))),
            )) {
                rejectUnchanged(
                    PlayerSessionFailure.OPTION_UNAVAILABLE,
                    request(rome.turn(), TurnCommandKind.PROPOSE_PEACE, other.civID).copy(peaceTerms = bad),
                )
            }

            val mutableGive = terms.give.toMutableList()
            val mutableReceive = terms.receive.toMutableList()
            val proposal = request(rome.turn(), TurnCommandKind.PROPOSE_PEACE, other.civID)
                .copy(peaceTerms = PeaceTerms(mutableGive, mutableReceive))
            val receipt = rome.execute(proposal)
            val proposedState = raw()
            mutableGive.clear()
            mutableReceive.add(theirTreaty.copy(amount = 999))
            check(raw() == proposedState) { "Caller-owned lists reached the engine trade" }
            check(rome.execute(proposal.copy(peaceTerms = terms)) == receipt) { "Caller mutation altered the cached request" }
            check(raw() == proposedState)
            rejectUnchanged(PlayerSessionFailure.REQUEST_ID_CONFLICT, proposal)
            val outgoing = rome.turn().diplomacy.outgoingOffers.single()
            check(outgoing.give.single { it.type == "Gold" }.amount == 20)
            immutable(outgoing.give)
            immutable(outgoing.receive)
            rome.execute(request(rome.turn(), TurnCommandKind.RETRACT_PEACE, other.civID))
            check(other.tradeRequests.none { it.requestingCiv == player.civID })

            // Unsupported offers disclose their category, never raw city or third-party IDs.
            val unsupported = TradeRequest(
                other.civID,
                trade(terms).apply {
                    ourOffers.add(TradeOffer("SECRET-CITY-ID", TradeOfferType.City, duration = -1))
                    theirOffers.add(TradeOffer(hidden.civID, TradeOfferType.WarDeclaration, duration = -1))
                },
            )
            player.tradeRequests.add(unsupported)
            val unsupportedSnapshot = rome.turn()
            val offered = unsupportedSnapshot.diplomacy.incomingOffers.single()
            check(offered.handle == "offer1")
            check(offered.give.single { it.type == "City" }.name == null)
            check(offered.receive.single { it.type == "WarDeclaration" }.name == null)
            check("SECRET-CITY-ID" !in PlayerTurnJson.encode(unsupportedSnapshot))
            check(hidden.civID !in PlayerTurnJson.encode(unsupportedSnapshot))
            check(!offered.accept.available && !offered.counteroffer.available && offered.reject.available)
            check(unsupportedSnapshot.pending.single { it.kind == "TradeRequests" }.supported)
            rejectUnchanged(
                PlayerSessionFailure.OPTION_UNAVAILABLE,
                request(rome.turn(), TurnCommandKind.ACCEPT_PEACE, offered.handle),
            )
            rejectUnchanged(
                PlayerSessionFailure.OPTION_UNAVAILABLE,
                request(rome.turn(), TurnCommandKind.COUNTER_PEACE, offered.handle).copy(peaceTerms = terms),
            )
            val rejection = request(rome.turn(), TurnCommandKind.REJECT_PEACE, offered.handle)
            val rejected = rome.execute(rejection)
            val afterRejection = raw()
            check(rome.execute(rejection) == rejected && raw() == afterRejection)
            check(player.tradeRequests.isEmpty())

            player.tradeRequests.add(TradeRequest(other.civID, trade(terms)))
            other.tradeRequests.add(TradeRequest(player.civID, trade(terms).reverse()))
            val own = rome.turn().diplomacy.incomingOffers.single()
            val foreign = greece.turn().diplomacy.incomingOffers.single()
            check(own.handle == "offer2" && own.handle != offered.handle) { "A rejected offer's handle was recycled" }
            check(foreign.handle.matches(Regex("offer[1-9][0-9]*")))
            for (handle in listOf("FORGED-OFFER-HANDLE", foreign.handle, other.civID)) {
                rejectUnchanged(
                    PlayerSessionFailure.OPTION_UNAVAILABLE,
                    request(rome.turn(), TurnCommandKind.ACCEPT_PEACE, handle),
                )
            }
            immutable(own.give)
            immutable(own.receive)
            immutable(own.accept.unavailableReasons)
            runtime.executor.pause()
            val pausedOffer = rome.turn().diplomacy.incomingOffers.single()
            check(!pausedOffer.accept.available && !pausedOffer.reject.available && !pausedOffer.counteroffer.available)
            check(pausedOffer.give == own.give && pausedOffer.receive == own.receive)
            runtime.executor.resume()

            // Unknown senders cannot be resolved into an offer or disclosed by an error.
            val invalidSender = TradeRequest("UNKNOWN-SENDER-SENTINEL", trade(terms))
            player.tradeRequests.add(invalidSender)
            val invalidSnapshot = rome.turn()
            check(invalidSnapshot.diplomacy.incomingOffers.size == 1)
            check(!invalidSnapshot.pending.single { it.kind == "TradeRequests" }.supported)
            check("UNKNOWN-SENDER-SENTINEL" !in PlayerTurnJson.encode(invalidSnapshot))
        }
    }

    private fun PeaceTerm.selection() = PeaceTermSelection(type, checkNotNull(name), amount, duration)
    private fun trade(terms: PeaceTerms) = Trade().apply {
        ourOffers.addAll(terms.give.map { TradeOffer(it.name, TradeOfferType.valueOf(it.type), it.amount, it.duration) })
        theirOffers.addAll(terms.receive.map { TradeOffer(it.name, TradeOfferType.valueOf(it.type), it.amount, it.duration) })
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
