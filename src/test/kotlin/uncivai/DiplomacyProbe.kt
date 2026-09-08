package uncivai

import com.unciv.json.json
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.PopupAlert
import com.unciv.logic.civilization.diplomacy.DiplomaticStatus
import com.unciv.models.translations.tr
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Path
import java.util.UUID

/** Raw fixture writes model paired worlds and malicious requests, never player discovery. */
object DiplomacyProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        HeadlessRuntime.createDuel(Path.of(args.single()).resolve("data"), 412L).use { runtime ->
            val game = runtime.game
            val player = game.getCivilization("Rome")
            val other = game.getCivilization("Greece")
            val host = PlayerHost(runtime)
            val rome = host.bindPlayer("Rome")
            val greece = host.bindPlayer("Greece")
            check(!player.knows(other))
            val initial = rome.turn()
            check(initial.diplomacy.knownCivilizations.isEmpty())
            check(initial.alerts.none { it.kind == "FirstContact" })
            val foreignUnit = other.units.getCivUnits().first()
            check(!foreignUnit.isVisibleTo(player))
            other.addGold(123)
            foreignUnit.promotions.XP += 5
            other.tech.techsToResearch.add("Pottery")
            check(rome.turn() == initial) { "Unmet opponent changed observations or revision" }

            player.diplomacyFunctions.makeCivilizationsMeet(other)
            val raw = json().toJson(game)
            val met = rome.turn()
            check(json().toJson(game) == raw) { "Encounter query changed engine state" }
            check(met.revision != initial.revision && met == rome.turn())
            val alert = met.alerts.single { it.kind == "FirstContact" }
            val detail = checkNotNull(alert.firstContact)
            check(alert.subject == "Greece" && alert.canAcknowledge)
            check(detail.civilizationId == other.civID && detail.civilizationName == other.civName)
            check(detail.leaderName == other.getLeaderDisplayName())
            check(detail.message == other.nation.introduction.tr())
            check(detail.acknowledgement == "A pleasure to meet you.".tr())
            val known = met.diplomacy.knownCivilizations.single()
            check(known.civilizationId == "Greece" && known.leaderName == detail.leaderName && !known.atWar)
            check(met.pending.single { it.kind == "PopupAlerts" }.supported && !met.canEndTurn)
            val waiting = greece.turn()
            check(waiting.alerts.single { it.kind == "FirstContact" }.firstContact!!.civilizationId == "Rome")
            check(waiting.alerts.none { it.canAcknowledge })
            check(waiting.pending.single { it.kind == "PopupAlerts" }.supported)

            other.addGold(456)
            foreignUnit.promotions.XP += 20
            foreignUnit.health -= 1
            other.tech.techsToResearch.clear()
            check(rome.turn() == met) { "Hidden met-opponent state changed encounter data or revision" }
            check(met == PlayerTurnJson.json.decodeFromString<PlayerTurnSnapshot>(PlayerTurnJson.encode(met)))
            check(runCatching { (met.diplomacy.knownCivilizations as MutableList).clear() }.exceptionOrNull() is UnsupportedOperationException)

            expect(PlayerSessionFailure.NOT_YOUR_TURN) {
                greece.execute(command(greece, waiting.alerts.single { it.kind == "FirstContact" }.handle))
            }
            expect(PlayerSessionFailure.OPTION_UNAVAILABLE) {
                rome.execute(command(rome, waiting.alerts.single { it.kind == "FirstContact" }.handle))
            }
            expect(PlayerSessionFailure.OPTION_UNAVAILABLE) { rome.execute(command(rome, "unknown-alert")) }
            val stale = command(rome, alert.handle)
            runtime.executor.pause()
            check(rome.turn().alerts.none { it.canAcknowledge })
            check(rome.turn().alerts.single { it.kind == "FirstContact" }.firstContact == detail)
            expect(PlayerSessionFailure.GAME_PAUSED) { rome.execute(stale) }
            runtime.executor.resume()
            expect(PlayerSessionFailure.STALE_REVISION) { rome.execute(stale) }

            // A supported introduction may be dismissed without silently consuming a consequential choice.
            val decision = PopupAlert(AlertType.DeclarationOfFriendship, other.civID)
            player.popupAlerts.add(decision)
            val mixed = rome.turn()
            check(!mixed.pending.single { it.kind == "PopupAlerts" }.supported)
            val unsupported = mixed.alerts.single { it.kind == "DeclarationOfFriendship" }
            check(!unsupported.canAcknowledge && unsupported.firstContact == null && unsupported.subject == null)
            expect(PlayerSessionFailure.OPTION_UNAVAILABLE) { rome.execute(command(rome, unsupported.handle)) }
            val acknowledge = command(rome, alert.handle)
            val alertIndex = player.popupAlerts.indexOfFirst { it.type == AlertType.FirstContact }
            val rawAlert = player.popupAlerts.removeAt(alertIndex)
            val beforeAck = json().toJson(game)
            player.popupAlerts.add(alertIndex, rawAlert)
            val receipt = rome.execute(acknowledge)
            check(json().toJson(game) == beforeAck) { "Acknowledgement changed more than its popup" }
            check(rome.execute(acknowledge) == receipt)
            check(json().toJson(game) == beforeAck)
            check(rome.turn().alerts.none { it.kind == "FirstContact" })
            check(rome.turn().diplomacy == met.diplomacy) { "Acknowledgement changed diplomacy" }
            expect(PlayerSessionFailure.OPTION_UNAVAILABLE) { rome.execute(command(rome, alert.handle)) }
            expect(PlayerSessionFailure.TURN_BLOCKED) {
                rome.execute(command(rome, null).copy(kind = TurnCommandKind.END_TURN).reviewedFor(rome.turn()))
            }
            check(other.popupAlerts.any { it.type == AlertType.FirstContact })
            check(player.popupAlerts.any { it === decision })

            // Invalid references fail closed and disclose neither their values nor another introduction.
            for (target in listOf("SECRET-NONEXISTENT-CIV", player.civID)) {
                val malformed = PopupAlert(AlertType.FirstContact, target)
                player.popupAlerts.add(malformed)
                val snapshot = rome.turn()
                val invalid = snapshot.alerts.single { it.kind == "FirstContact" }
                check(invalid.firstContact == null && invalid.subject == null && !invalid.canAcknowledge)
                check(target != "SECRET-NONEXISTENT-CIV" || target !in PlayerTurnJson.encode(snapshot))
                expect(PlayerSessionFailure.OPTION_UNAVAILABLE) { rome.execute(command(rome, invalid.handle)) }
                player.popupAlerts.remove(malformed)
            }

            player.getDiplomacyManager(other)!!.diplomaticStatus = DiplomaticStatus.War
            other.getDiplomacyManager(player)!!.diplomaticStatus = DiplomaticStatus.War
            val war = rome.turn()
            check(war.diplomacy.knownCivilizations.single().atWar == other.isAtWarWith(player))
            check(war.diplomacy.knownCivilizations.single().atWar && !known.atWar)
            check(war.revision != receipt.revision)
        }
    }

    private fun command(session: PlayerSession, target: String?): TurnCommandRequest {
        val snapshot = session.turn()
        return TurnCommandRequest(UUID.randomUUID().toString(), snapshot.epoch, snapshot.revision, TurnCommandKind.ACKNOWLEDGE_ALERT, target)
    }

    private fun expect(reason: PlayerSessionFailure, action: () -> Any?) {
        val failure = runCatching(action).exceptionOrNull()
        check(failure is PlayerSessionException && failure.reason == reason) { "Expected $reason, got $failure" }
        check(failure.cause == null)
    }
}
