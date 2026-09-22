package uncivai

import com.unciv.json.json
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.CivFlags
import com.unciv.logic.civilization.NotificationCategory
import com.unciv.logic.civilization.PopupAlert
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.PlayerTurnSnapshot
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID

object CityStateProbe {
    @JvmStatic fun main(args: Array<String>) {
        val root = Path.of(args[0])
        when (args[1]) {
            "ordinary" -> ordinary(root)
            "affordability" -> affordability(root)
            "interactions" -> interactions(root)
            "conflict" -> conflict(root)
            "popups" -> popups(root)
            "privacy" -> privacy(root)
            "recovery" -> recovery(root)
            else -> error(args[1])
        }
    }

    private fun ordinary(root: Path) {
        val checkpoint = root.resolve("checkpoint.json")
        HeadlessRuntime.createVanillaGame(root.resolve("initial"), 412L, cityStates = 2).use { runtime ->
            check(runtime.game.gameParameters.numberOfCityStates == 2)
            check(runtime.game.civilizations.count { it.isCityState } == 2)
            check(runtime.game.civilizations.filter { it.isCityState }.all { !it.isHuman() && it.units.getCivUnits().any() })
            runtime.executor.enableCheckpoints(checkpoint)
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            check(runtime.game.gameParameters.numberOfCityStates == 2)
            check(runtime.game.civilizations.count { it.isCityState } == 2)
        }
    }

    private fun command(turn: PlayerTurnSnapshot, kind: TurnCommandKind = TurnCommandKind.CITY_STATE_ACTION, target: String? = "Geneva", selection: String? = null) = TurnCommandRequest(
        UUID.randomUUID().toString(),
        turn.epoch,
        turn.revision,
        kind,
        target,
        selection,
        confirmAction = kind == TurnCommandKind.CITY_STATE_ACTION,
        confirmWar = kind == TurnCommandKind.DECLARE_WAR,
    ).reviewedFor(turn)

    private fun interactions(root: Path) {
        lateinit var last: TurnCommandRequest
        lateinit var serializedReceipt: String
        val checkpoint = root.resolve("checkpoint.json")
        CityStateFixtures.create(root.resolve("initial")).use { runtime ->
            runtime.executor.enableCheckpoints(checkpoint)
            GameplayTestApi(runtime, root.resolve("tls"), CityStateFixtures.tokens).use { api ->
                val token = api.playerToken("Rome")
                var turn = api.turn(token)
                check(turn.diplomacy.cityStates.map { it.name } == listOf("Geneva", "Sidon"))
                val give = command(turn, selection = "giftGold1000")
                api.reject(api.playerToken("Greece"), give, "NOT_YOUR_TURN")
                api.reject(token, give.copy(target = "unknown"), "OPTION_UNAVAILABLE")
                api.reject(token, give.copy(selection = "giftGold1"), "OPTION_UNAVAILABLE")
                api.reject(token, give.copy(destination = uncivai.player.TilePosition(0, 0)), "INVALID_ARGUMENT")
                val receipt = api.execute(token, give)
                check(receipt.cityState!!.goldChange == -1000 && receipt.cityState.allyAfter!!.civilizationId == "Rome")
                check(receipt.cityState.completedOrRemovedQuests == listOf("Give Gold"))
                check(api.execute(token, give) == receipt)
                api.reject(token, give.copy(selection = "giftGold500"), "REQUEST_ID_CONFLICT")
                turn = api.turn(token)
                val improvement = turn.diplomacy.cityStates.first().actions.singleOrNull { it.improvement == "Mine" && it.available } ?: error("No improvement: ${turn.diplomacy.cityStates.first()}")
                val built = api.execute(token, command(turn, selection = improvement.name))
                check(built.cityState!!.goldChange == -200 && built.cityState.improvement == "Mine")
                turn = api.turn(token)
                check(turn.diplomacy.cityStates.first().resources.any { it.name == "Gems" && it.amount > 0 })
                val gift = turn.map.units.firstOrNull { it.cityStateGift?.action?.available == true } ?: error("No gift: ${turn.map.units.map { it.name to it.cityStateGift }}")
                val gifted = api.execute(token, command(turn, TurnCommandKind.GIFT_UNIT, gift.handle))
                check(gifted.cityState!!.unitRemoved == gift.handle && gifted.cityState.influenceAfter == gifted.cityState.influenceBefore + 5)
                turn = api.turn(token)
                val merchant = turn.map.units.single { it.name == "Great Merchant" }
                val mission = merchant.greatPerson!!.abilities.single { it.kind == "ConductTradeMission" }
                check(mission.supported && mission.available)
                val completed = api.execute(token, command(turn, TurnCommandKind.ACTIVATE_GREAT_PERSON, merchant.handle, mission.name))
                check(completed.cityState!!.influenceAfter > completed.cityState.influenceBefore && completed.greatPerson!!.goldChange > 0 && completed.greatPerson.consumed)
                turn = api.turn(token)
                api.reject(token, command(turn, selection = "pledgeProtection").copy(confirmAction = false), "INVALID_ARGUMENT")
                last = command(turn, selection = "pledgeProtection")
                val protected = api.execute(token, last)
                check(protected.cityState!!.protectingAfter && !protected.cityState.protectingBefore)
                serializedReceipt = PlayerTurnJson.encode(protected)
                turn = api.turn(token)
                api.reject(token, command(turn, selection = "revokeProtection"), "OPTION_UNAVAILABLE")
                api.reject(token, give.copy(requestId = "stale"), "STALE_REVISION")
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("restored")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(PlayerTurnJson.encode(session.execute(last)) == serializedReceipt)
            val turn = session.turn()
            check(turn.diplomacy.cityStates.first().protecting)
            check(turn.map.units.none { it.name == "Great Merchant" })
            check(turn.diplomacy.cityStates.first().quests.none { it.name == "Give Gold" })
        }
    }

    private fun conflict(root: Path) = CityStateFixtures.create(root.resolve("initial")).use { runtime ->
        GameplayTestApi(runtime, root.resolve("tls"), CityStateFixtures.tokens).use { api ->
            val rome = api.playerToken("Rome")
            val declared = api.execute(rome, command(api.turn(rome), TurnCommandKind.DECLARE_WAR, "Geneva"))
            check(declared.cityState!!.atWarAfter)
            api.reject(rome, command(api.turn(rome), selection = "makePeace"), "OPTION_UNAVAILABLE")
            runtime.game.getCivilization("Rome").getDiplomacyManager("Geneva")!!.removeFlag(DiplomacyFlags.DeclaredWar)
            val peace = api.execute(rome, command(api.turn(rome), selection = "makePeace"))
            check(!peace.cityState!!.atWarAfter && peace.cityState.atWarBefore)
            api.reject(rome, command(api.turn(rome), TurnCommandKind.DECLARE_WAR, "Geneva"), "OPTION_UNAVAILABLE")
            // Normal turn handoffs run the built-in city-state turns too.
            for (id in listOf("Rome", "Greece")) finish(api, api.playerToken(id))
            val france = api.playerToken("France")
            val turn = api.turn(france)
            val sidon = turn.diplomacy.cityStates.single { it.name == "Sidon" }
            check(sidon.tributeGoldWillingness >= 0) { sidon.toString() }
            val tribute = api.execute(france, command(turn, target = "Sidon", selection = "goldTribute"))
            check(tribute.cityState!!.goldChange > 0 && tribute.cityState.influenceAfter == tribute.cityState.influenceBefore - 15)
            api.reject(france, command(api.turn(france), target = "Sidon", selection = "workerTribute"), "OPTION_UNAVAILABLE")
            runtime.game.getCivilization("Sidon").removeFlag(CivFlags.RecentlyBullied.name)
            val worker = api.execute(france, command(api.turn(france), target = "Sidon", selection = "workerTribute"))
            check(worker.cityState!!.gainedUnits.isNotEmpty()) { "Worker tribute result: ${worker.cityState}" }
        }
    }

    internal fun finish(api: GameplayTestApi, token: String) {
        repeat(40) {
            val turn = api.readAlerts(token)
            val city = turn.cities.firstOrNull { !it.puppet && it.constructionQueue.isEmpty() }
            val next = when {
                turn.researchQueue.isEmpty() -> command(turn, TurnCommandKind.SET_RESEARCH_QUEUE, null).copy(queue = listOf(turn.researchOptions.first()))
                city != null -> command(turn, TurnCommandKind.QUEUE_CONSTRUCTION, city.handle, city.constructionOptions.first())
                turn.pending.any { it.kind == "Policy" } -> command(turn, TurnCommandKind.ADOPT_POLICY, null, turn.policyOptions.first())
                turn.canEndTurn -> command(turn, TurnCommandKind.END_TURN, null)
                else -> error("Unexpected blocker ${turn.pending}")
            }
            api.execute(token, next)
            if (next.kind == TurnCommandKind.END_TURN) return
        }
        error("Fixture budget exhausted")
    }

    private fun popups(root: Path) {
        val checkpoint = root.resolve("checkpoint.json")
        lateinit var last: TurnCommandRequest
        lateinit var receiptJson: String
        CityStateFixtures.create(root.resolve("initial")).use { runtime ->
            val rome = runtime.game.getCivilization("Rome")
            val geneva = runtime.game.getCivilization("Geneva")
            geneva.cityStateFunctions.updateDiplomaticRelationshipForCityState()
            check(rome.popupAlerts.any { it.type == AlertType.BorderConflict && it.value == geneva.civID })
            runtime.executor.enableCheckpoints(checkpoint)
        }
        // An already-pending popup in a saved game must become actionable too.
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("pending")).use { runtime ->
            val rome = runtime.game.getCivilization("Rome")
            val geneva = runtime.game.getCivilization("Geneva")
            GameplayTestApi(runtime, root.resolve("tls"), CityStateFixtures.tokens).use { api ->
                val token = api.playerToken("Rome")
                for ((type, selection) in listOf(AlertType.BorderConflict to "Acknowledge", AlertType.BorderConflict to "Protest", AlertType.TilesStolen to "Acknowledge")) {
                    if (rome.popupAlerts.none { it.type == type }) rome.popupAlerts.add(PopupAlert(type, geneva.civID))
                    val turn = api.turn(token)
                    val alert = turn.alerts.single { it.kind == type.name }
                    val native = checkNotNull(alert.nativeDiplomacy)
                    check(native.sender == geneva.civID && native.responseCommand == TurnCommandKind.RESPOND_CITY_STATE_ALERT)
                    check(native.options.any { it.name == selection && it.available })
                    check(turn.pending.single { it.kind == "PopupAlerts" }.supported && !turn.canEndTurn)
                    last = command(turn, TurnCommandKind.RESPOND_CITY_STATE_ALERT, alert.handle, selection)
                    val before = json().toJson(runtime.game)
                    api.reject(token, command(turn, TurnCommandKind.END_TURN, null), "TURN_BLOCKED")
                    api.reject(token, command(turn, TurnCommandKind.ACKNOWLEDGE_ALERT, alert.handle), "OPTION_UNAVAILABLE")
                    api.reject(api.playerToken("Greece"), last, "NOT_YOUR_TURN")
                    api.reject(token, last.copy(target = "unknown"), "OPTION_UNAVAILABLE")
                    api.reject(token, last.copy(selection = "DeclareWar"), "OPTION_UNAVAILABLE")
                    api.reject(token, last.copy(revision = "stale"), "STALE_REVISION")
                    check(json().toJson(runtime.game) == before)
                    val receipt = api.execute(token, last)
                    val outcome = checkNotNull(receipt.cityState)
                    check(outcome.influenceBefore == outcome.influenceAfter && outcome.goldChange == 0)
                    check(!outcome.atWarBefore && !outcome.atWarAfter)
                    check(rome.popupAlerts.none { it.type == type })
                    check(api.execute(token, last) == receipt)
                    receiptJson = PlayerTurnJson.encode(receipt)
                }
                // Major-player responses remain on the existing diplomacy path.
                val majorAlert = PopupAlert(AlertType.Denounced, "Greece")
                rome.popupAlerts.add(majorAlert)
                val turn = api.turn(token)
                val alert = turn.alerts.single { it.kind == "Denounced" }
                check(alert.nativeDiplomacy!!.responseCommand == null)
                api.reject(token, command(turn, TurnCommandKind.RESPOND_CITY_STATE_ALERT, alert.handle, "Acknowledge"), "OPTION_UNAVAILABLE")
                rome.popupAlerts.remove(majorAlert)
                finish(api, token)
                check(runtime.game.currentPlayer == "Greece")
            }
        }
        HeadlessRuntime.loadCheckpoint(checkpoint, root.resolve("completed")).use { runtime ->
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val before = json().toJson(runtime.game)
            check(PlayerTurnJson.encode(session.execute(last)) == receiptJson)
            check(json().toJson(runtime.game) == before)
            check(runtime.game.getCivilization("Rome").popupAlerts.none { it.type in setOf(AlertType.BorderConflict, AlertType.TilesStolen) })
        }
    }

    private fun privacy(root: Path) = CityStateFixtures.create(root.resolve("initial"), meetAll = false).use { runtime ->
        val game = runtime.game
        val rome = game.getCivilization("Rome")
        val france = game.getCivilization("France")
        val geneva = game.getCivilization("Geneva")
        check(!rome.knows(france))
        france.diplomacyFunctions.makeCivilizationsMeet(geneva)
        geneva.getDiplomacyManager(france)!!.setInfluence(70f)
        geneva.cityStateFunctions.addProtectorCiv(france)
        CityStateFixtures.addQuest(geneva, rome, "Find Player", france.civID)
        CityStateFixtures.addQuest(geneva, france, "Construct Wonder", "Stonehenge")
        CityStateFixtures.addQuest(geneva, rome, "Contest Culture", "0")
        CityStateFixtures.addQuest(geneva, france, "Contest Culture", "0")
        CityStateFixtures.addQuest(geneva, rome, "Connect Resource", "Gems")
        CityStateFixtures.addQuest(geneva, rome, "Acquire Great Person", "Great Scientist")
        CityStateFixtures.addQuest(geneva, rome, "Invest", "50")
        CityStateFixtures.addQuest(geneva, rome, "Give Gold", france.civID)
        france.totalCultureForContests = 900
        rome.addNotification("We have lost our alliance with [Geneva] to [France].", NotificationCategory.Diplomacy, "France")
        rome.addNotification("The [Contest Culture] quest for [Geneva] has ended. It was won by [{France}, {Rome}].", NotificationCategory.Diplomacy)
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val before = session.turn()
        val detail = before.diplomacy.cityStates.single()
        check(detail.ally!!.unmet && detail.protectors.single().unmet)
        check(detail.quests.none { it.name == "Construct Wonder" })
        check(detail.quests.single { it.name == "Find Player" }.targetCivilization!!.unmet)
        check(detail.quests.single { it.name == "Contest Culture" }.contest!!.leaders.single().unmet)
        check(detail.quests.all { it.objective!!.issuer == geneva.civID })
        check(detail.quests.single { it.name == "Connect Resource" }.objective!!.targetName == "Gems")
        check(detail.quests.single { it.name == "Acquire Great Person" }.objective!!.targetName == "Great Scientist")
        check(detail.quests.single { it.name == "Invest" }.objective!!.goldGiftInfluenceBonusPercent == 50)
        check(detail.quests.single { it.name == "Find Player" }.objective!!.targetName == null)
        check(detail.quests.single { it.name == "Give Gold" }.objective!!.targetName == null)
        check(detail.quests.single { it.name == "Contest Culture" }.objective!!.targetName == null)
        check(detail.actions.none { it.position != null })
        val view = PlayerTurnJson.encode(before)
        check(!view.contains("France") && !view.contains("Sidon"))
        val native = json().toJson(game)
        session.turn()
        check(json().toJson(game) == native) { "Observation mutated the game" }
        france.addGold(1234)
        check(session.turn() == before) { "Private rival treasury changed the observation" }
    }

    private fun affordability(root: Path) = CityStateFixtures.create(root.resolve("initial")).use { runtime ->
        val rome = runtime.game.getCivilization("Rome")
        rome.addGold(249 - rome.gold)
        GameplayTestApi(runtime, root.resolve("tls"), CityStateFixtures.tokens).use { api ->
            val token = api.playerToken("Rome")
            val before = api.turn(token)
            val option = before.diplomacy.cityStates.first().actions.single { it.name == "giftGold250" }
            check(!option.available && option.unavailableReasons.any { "Gold" in it })
            api.reject(token, command(before, selection = option.name), "OPTION_UNAVAILABLE")
            check(rome.gold == 249 && api.turn(token).diplomacy.cityStates.first().quests.any { it.name == "Give Gold" })
            rome.addGold(1)
            val fresh = api.turn(token)
            check(fresh.diplomacy.cityStates.first().actions.single { it.name == "giftGold250" }.available)
            val receipt = api.execute(token, command(fresh, selection = "giftGold250"))
            check(rome.gold == 0 && receipt.cityState!!.goldChange == -250)
            check(receipt.cityState.completedOrRemovedQuests == listOf("Give Gold"))
            check(api.turn(token).diplomacy.cityStates.first().actions.filter { it.kind == "GiftGold" }.none { it.available })
            check(api.execute(token, command(fresh, selection = "giftGold250").copy(requestId = receipt.requestId)) == receipt)
        }
    }

    private fun recovery(root: Path) {
        for (action in listOf("giftGold250", "giftUnit", "mission1")) {
            for (afterReplace in listOf(false, true)) {
                val directory = root.resolve("$action-$afterReplace")
                val checkpoint = directory.resolve("checkpoint.json")
                lateinit var request: TurnCommandRequest
                var failCommit = false
                CityStateFixtures.create(directory.resolve("initial")).use { runtime ->
                    runtime.executor.enableCheckpoints(CheckpointStore(checkpoint, beforeReplace = { if (failCommit && !afterReplace) error("Interrupted write") }, afterReplace = { if (failCommit && afterReplace) error("Lost reply") }))
                    val session = PlayerHost(runtime).bindPlayer("Rome")
                    val turn = session.turn()
                    request = when (action) {
                        "giftUnit" -> command(turn, TurnCommandKind.GIFT_UNIT, turn.map.units.first { it.cityStateGift?.action?.available == true }.handle)
                        "mission1" -> command(turn, TurnCommandKind.ACTIVATE_GREAT_PERSON, turn.map.units.single { it.name == "Great Merchant" }.handle, action)
                        else -> command(turn, selection = action)
                    }
                    failCommit = true
                    check(runCatching { session.execute(request) }.isFailure)
                    check(runtime.executor.mutationFailure != null)
                }
                HeadlessRuntime.loadCheckpoint(checkpoint, directory.resolve("restored")).use { runtime ->
                    val session = PlayerHost(runtime).bindPlayer("Rome")
                    val retry = if (afterReplace) request else request.copy(requestId = "fresh", revision = session.turn().revision)
                    val receipt = session.execute(retry)
                    check(receipt.cityState != null)
                    val native = json().toJson(runtime.game)
                    val saved = Files.readString(checkpoint)
                    check(session.execute(retry) == receipt)
                    check(json().toJson(runtime.game) == native && Files.readString(checkpoint) == saved)
                    check(
                        receipt.cityState.goldChange == if (action == "giftGold250") {
                            -250
                        } else if (action == "giftUnit") {
                            0
                        } else {
                            receipt.greatPerson!!.goldChange
                        },
                    )
                }
            }
        }
    }
}
