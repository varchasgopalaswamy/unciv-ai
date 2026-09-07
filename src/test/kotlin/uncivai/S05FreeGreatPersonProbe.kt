package uncivai

import com.badlogic.gdx.Gdx
import com.unciv.Constants
import com.unciv.json.json
import com.unciv.logic.GameInfo
import com.unciv.logic.city.City
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerType
import com.unciv.logic.map.mapunit.MapUnit
import uncivai.player.ChooseFreeGreatPersonReceipt
import uncivai.player.ChooseFreeGreatPersonRequest
import uncivai.player.FreeGreatPersonDecision
import uncivai.player.FreeGreatPersonSnapshot
import uncivai.player.PlayerCommandJson
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.RenameCityRequest
import java.nio.file.Files
import java.nio.file.Path
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.CyclicBarrier
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

/** Exercises the player decision API in the display-less JVM that owns each loaded game. */
object S05FreeGreatPersonProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        require(args.size == 2) {
            "Usage: S05FreeGreatPersonProbe <absolute-scenario-directory> <absolute-data-directory>"
        }
        val scenarios = Path.of(args[0])
        val data = Path.of(args[1])
        require(scenarios.isAbsolute && data.isAbsolute) { "Probe paths must be absolute" }
        check(Gdx.app == null && Gdx.graphics == null && Gdx.gl == null)

        verifyEveryOrdinaryOption(
            scenarios.resolve("ordinary-vanilla.json"),
            data.resolve("ordinary-vanilla"),
            humanName = "Mongolia",
            equivalentName = "Khan",
        )
        verifyEveryOrdinaryOption(
            scenarios.resolve("ordinary.json"),
            data.resolve("ordinary-gods-and-kings"),
            humanName = "The Maya",
            equivalentName = null,
        )
        verifyMayaAndHistoricalRetries(
            scenarios.resolve("maya.json"),
            data.resolve("maya-retries"),
        )
        verifyValidationInvalidationAndPrivacy(
            scenarios.resolve("ordinary.json"),
            data.resolve("validation"),
        )
        verifyCommandNamespace(
            scenarios.resolve("ordinary.json"),
            data.resolve("command-namespace"),
        )
        verifyEmptyDecisions(
            scenarios.resolve("no-city.json"),
            scenarios.resolve("empty-maya-pool.json"),
            data.resolve("empty-decisions"),
        )
        verifyPlacementFailureAndRetry(
            scenarios.resolve("blocked-placement.json"),
            data.resolve("blocked-placement"),
        )
        verifyConcurrentDecisions(
            scenarios.resolve("ordinary.json"),
            data.resolve("concurrency"),
        )
        verifyReloadExpiry(
            scenarios.resolve("ordinary.json"),
            data.resolve("reload"),
        )
        verifyMutationFailureHaltsGame(
            scenarios.resolve("ordinary.json"),
            data.resolve("mutation-failure"),
        )

        check(Gdx.app == null && Gdx.graphics == null && Gdx.gl == null)
        println("S05-VERIFIED")
    }

    private fun verifyEveryOrdinaryOption(
        save: Path,
        data: Path,
        humanName: String,
        equivalentName: String?,
    ) {
        val (offeredNames, waterOptions) = HeadlessRuntime.load(save, data.resolve("discover")).use { runtime ->
            val game = runtime.game
            val human = game.getCivilization(humanName)
            val session = PlayerHost(runtime).bindPlayer(humanName)
            val rawBefore = raw(game)
            val first = session.freeGreatPersonChoice()
            val repeated = session.freeGreatPersonChoice()
            val decision = requireDecision(first)

            check(first == repeated) { "An unchanged decision did not retain its handle" }
            check(first.epoch == runtime.epoch)
            check(decision.remainingChoices == 1)
            check(!decision.mayaLimited)
            check(decision.options.isNotEmpty())
            check(decision.options == decision.options.sorted())
            check(decision.options.distinct() == decision.options)
            check(raw(game) == rawBefore) { "Decision discovery changed engine state" }
            check(session.summary().freeGreatPeople == 1)
            check(human.greatPeople.freeGreatPeople == 1)
            check(PlayerCommandJson.encode(first).contains(decision.handle))

            val encoded = PlayerCommandJson.encode(first)
            val attemptedMutation = runCatching {
                @Suppress("UNCHECKED_CAST")
                (decision.options as MutableList<String>).clear()
            }
            check(attemptedMutation.isFailure) { "Returned option list was mutable" }
            check(PlayerCommandJson.encode(first) == encoded)
            check(session.freeGreatPersonChoice() == first)

            if (equivalentName != null) {
                check(equivalentName in decision.options)
                check("Great General" !in decision.options) {
                    "The replaced Great General leaked beside Mongolia's equivalent"
                }
            }
            decision.options to decision.options.filter { name ->
                game.ruleset.units.getValue(name).isWaterUnit
            }.toSet()
        }

        for ((index, unitName) in offeredNames.withIndex()) {
            val optionSave = if (unitName in waterOptions) {
                prepareCoastalSave(
                    source = save,
                    target = data.resolve("coastal-$index.json"),
                    data = data.resolve("coastal-$index-prepare"),
                    humanName = humanName,
                )
            } else {
                save
            }
            HeadlessRuntime.load(optionSave, data.resolve("option-$index")).use { runtime ->
                val game = runtime.game
                val human = game.getCivilization(humanName)
                val session = PlayerHost(runtime).bindPlayer(humanName)
                val snapshot = session.freeGreatPersonChoice()
                val decision = requireDecision(snapshot)
                check(decision.options == offeredNames)
                if (unitName in waterOptions) {
                    check(checkNotNull(human.getCapital()).isNaval()) {
                        "$unitName fixture is not coastal after reload"
                    }
                }
                val unitIdsBefore = human.units.getCivUnits().map { it.id }.toSet()
                val otherCounters = Triple(
                    human.policies.freePolicies,
                    human.tech.freeTechs,
                    human.greatPeople.mayaLimitedFreeGP,
                )
                val receipt = session.chooseFreeGreatPerson(
                    choiceRequest("ordinary-$index", snapshot, unitName),
                )

                check(receipt.unitName == unitName)
                check(receipt.remainingChoices == 0)
                check(human.greatPeople.freeGreatPeople == 0)
                check(
                    Triple(
                        human.policies.freePolicies,
                        human.tech.freeTechs,
                        human.greatPeople.mayaLimitedFreeGP,
                    ) == otherCounters,
                ) { "A great-person choice changed another pending choice" }
                val added = human.units.getCivUnits().filter { it.id !in unitIdsBefore }.toList()
                check(added.size == 1) { "$unitName did not add exactly one live unit" }
                val unit = added.single()
                check(unit.name == unitName)
                check(unit.owner == human.civID && unit.civ === human)
                check(unit.getTile().getUnits().any { it === unit })
                check(game.tileMap[unit.getTile().position] === unit.getTile())
                check(session.freeGreatPersonChoice().decision == null)
            }
        }
    }

    private fun verifyMayaAndHistoricalRetries(save: Path, data: Path) {
        HeadlessRuntime.load(save, data).use { runtime ->
            val game = runtime.game
            val human = game.getCivilization("The Maya")
            val manager = human.greatPeople
            val sessionA = PlayerHost(runtime).bindPlayer(human.civID)
            val sessionB = PlayerHost(runtime).bindPlayer(human.civID)
            val snapshotA = sessionA.freeGreatPersonChoice()
            val snapshotB = sessionB.freeGreatPersonChoice()
            val decisionA = requireDecision(snapshotA)
            val decisionB = requireDecision(snapshotB)

            check(snapshotA.epoch == snapshotB.epoch)
            check(decisionA.handle != decisionB.handle) { "Separate sessions shared a decision handle" }
            check(decisionA.options == listOf("Great Engineer", "Great Scientist"))
            check(decisionA.remainingChoices == 2 && decisionA.mayaLimited)
            check(decisionA.copy(handle = decisionB.handle) == decisionB)

            expectNoMutation(game, PlayerSessionFailure.DECISION_UNAVAILABLE) {
                sessionA.chooseFreeGreatPerson(
                    choiceRequest("foreign-session-handle", snapshotA, "Great Scientist")
                        .copy(decisionHandle = decisionB.handle),
                )
            }

            val unitsBeforeA = human.units.getCivUnits().map { it.id }.toSet()
            val requestA = choiceRequest("maya-a", snapshotA, "Great Scientist")
            val receiptA = sessionA.chooseFreeGreatPerson(requestA)
            check(receiptA.remainingChoices == 1)
            check(manager.freeGreatPeople == 1 && manager.mayaLimitedFreeGP == 0)
            check("Great Scientist" !in manager.longCountGPPool)
            check("Great Engineer" in manager.longCountGPPool) {
                "The Maya selection removed an unselected stored option"
            }
            checkAddedUnit(human, unitsBeforeA, "Great Scientist")
            expectNoMutation(game, PlayerSessionFailure.REQUEST_ID_CONFLICT) {
                sessionA.chooseFreeGreatPerson(requestA.copy(unitName = "Great Engineer"))
            }

            expectNoMutation(game, PlayerSessionFailure.DECISION_UNAVAILABLE) {
                sessionB.chooseFreeGreatPerson(
                    choiceRequest("competing-old-decision", snapshotB, "Great Engineer"),
                )
            }
            val refreshedB = sessionB.freeGreatPersonChoice()
            val refreshedDecisionB = requireDecision(refreshedB)
            check(refreshedDecisionB.handle != decisionB.handle)
            check(refreshedDecisionB.remainingChoices == 1)
            check(!refreshedDecisionB.mayaLimited)
            check(refreshedDecisionB.options.size > decisionB.options.size)

            val unitsBeforeB = human.units.getCivUnits().map { it.id }.toSet()
            val requestB = choiceRequest("ordinary-b", refreshedB, "Great Engineer")
            val receiptB = sessionB.chooseFreeGreatPerson(requestB)
            check(receiptB.remainingChoices == 0)
            check(manager.freeGreatPeople == 0 && manager.mayaLimitedFreeGP == 0)
            check("Great Engineer" in manager.longCountGPPool) {
                "An unrestricted choice changed the stored Maya pool"
            }
            checkAddedUnit(human, unitsBeforeB, "Great Engineer")

            setCurrentPlayer(game, game.getCivilization("Greece"))
            val rawBeforeRetry = raw(game)
            check(sessionA.chooseFreeGreatPerson(requestA) == receiptA)
            check(raw(game) == rawBeforeRetry) { "Historical retry placed another unit" }

            human.playerType = PlayerType.AI
            expectNoMutation(game, PlayerSessionFailure.PLAYER_UNAVAILABLE) {
                sessionA.chooseFreeGreatPerson(requestA)
            }
            human.playerType = PlayerType.Human
            check(sessionA.chooseFreeGreatPerson(requestA) == receiptA)
        }
    }

    private fun verifyValidationInvalidationAndPrivacy(save: Path, data: Path) {
        HeadlessRuntime.load(save, data).use { runtime ->
            val game = runtime.game
            val human = game.getCivilization("The Maya")
            val opponent = game.getCivilization("Greece")
            val manager = human.greatPeople
            val sessionA = PlayerHost(runtime).bindPlayer(human.civID)
            val sessionB = PlayerHost(runtime).bindPlayer(human.civID)
            val initialA = sessionA.freeGreatPersonChoice()
            val initialB = sessionB.freeGreatPersonChoice()
            val initialDecision = requireDecision(initialA)
            val validOption = ordinaryLandOption(initialA)

            check(!human.knows(opponent)) { "Privacy check requires an unmet opponent" }
            val hiddenName = "Unseen city ${UUID.randomUUID()}"
            opponent.cities.single().name = hiddenName
            opponent.greatPeople.freeGreatPeople = 29
            opponent.greatPeople.mayaLimitedFreeGP = 17
            opponent.greatPeople.longCountGPPool.add("hidden-${UUID.randomUUID()}")
            val hiddenWorld = raw(game)
            check(sessionA.freeGreatPersonChoice() == initialA) {
                "An opponent's hidden state changed this player's decision token"
            }
            val hiddenFailure = expectNoMutation(game, PlayerSessionFailure.DECISION_UNAVAILABLE) {
                sessionA.chooseFreeGreatPerson(
                    choiceRequest("hidden-handle", initialA, validOption)
                        .copy(decisionHandle = "unknown-${UUID.randomUUID()}"),
                )
            }
            check(hiddenName !in hiddenFailure.message.orEmpty())
            check(opponent.greatPeople.longCountGPPool.none { it in hiddenFailure.message.orEmpty() })
            check(raw(game) == hiddenWorld)

            expectNoMutation(game, PlayerSessionFailure.STALE_EPOCH) {
                sessionA.chooseFreeGreatPerson(
                    choiceRequest("stale-epoch", initialA, validOption).copy(epoch = "old-epoch"),
                )
            }
            for (invalidId in listOf("", "control\ncharacter", "x".repeat(129))) {
                expectNoMutation(game, PlayerSessionFailure.INVALID_ARGUMENT) {
                    sessionA.chooseFreeGreatPerson(
                        choiceRequest(invalidId, initialA, validOption),
                    )
                }
            }
            expectNoMutation(game, PlayerSessionFailure.DECISION_UNAVAILABLE) {
                sessionA.chooseFreeGreatPerson(
                    choiceRequest("other-session", initialA, validOption)
                        .copy(decisionHandle = requireDecision(initialB).handle),
                )
            }
            for (invalidOption in listOf("", "Warrior", "not a unit")) {
                expectNoMutation(game, PlayerSessionFailure.OPTION_UNAVAILABLE) {
                    sessionA.chooseFreeGreatPerson(
                        choiceRequest("invalid-option-$invalidOption", initialA, invalidOption),
                    )
                }
            }

            setCurrentPlayer(game, opponent)
            expectNoMutation(game, PlayerSessionFailure.NOT_YOUR_TURN) {
                sessionA.chooseFreeGreatPerson(
                    choiceRequest("out-of-turn", initialA, validOption),
                )
            }
            val outOfTurn = sessionA.freeGreatPersonChoice()
            check(requireDecision(outOfTurn).handle != initialDecision.handle)
            check(!requireDecision(outOfTurn).mayaLimited)
            setCurrentPlayer(game, human)

            val restored = sessionA.freeGreatPersonChoice()
            game.currentPlayerCiv = opponent
            expectNoMutation(game, PlayerSessionFailure.NOT_YOUR_TURN) {
                sessionA.chooseFreeGreatPerson(
                    choiceRequest("mismatched-current-player", restored, validOption),
                )
            }
            game.currentPlayerCiv = human
            human.playerType = PlayerType.AI
            expectNoMutation(game, PlayerSessionFailure.PLAYER_UNAVAILABLE) {
                sessionA.freeGreatPersonChoice()
            }
            expectNoMutation(game, PlayerSessionFailure.PLAYER_UNAVAILABLE) {
                sessionA.chooseFreeGreatPerson(
                    choiceRequest("revoked-player", restored, validOption),
                )
            }
            human.playerType = PlayerType.Human

            val handles = mutableListOf(requireDecision(sessionA.freeGreatPersonChoice()).handle)
            manager.freeGreatPeople++
            val freeCountChanged = sessionA.freeGreatPersonChoice()
            handles += requireDecision(freeCountChanged).handle
            expectNoMutation(game, PlayerSessionFailure.DECISION_UNAVAILABLE) {
                sessionB.chooseFreeGreatPerson(
                    choiceRequest("shared-generation", initialB, validOption),
                )
            }
            check(requireDecision(sessionB.freeGreatPersonChoice()).handle != requireDecision(initialB).handle)

            manager.mayaLimitedFreeGP = 1
            manager.longCountGPPool.clear()
            val mayaCounterChanged = sessionA.freeGreatPersonChoice()
            handles += requireDecision(mayaCounterChanged).handle
            check(requireDecision(mayaCounterChanged).options.isEmpty())

            manager.longCountGPPool.add(validOption)
            val optionsChanged = sessionA.freeGreatPersonChoice()
            handles += requireDecision(optionsChanged).handle
            check(requireDecision(optionsChanged).options == listOf(validOption))
            manager.longCountGPPool.remove(validOption)
            expectNoMutation(game, PlayerSessionFailure.DECISION_UNAVAILABLE) {
                sessionA.chooseFreeGreatPerson(
                    choiceRequest("options-changed-before-resolution", optionsChanged, validOption),
                )
            }
            manager.longCountGPPool.add(validOption)
            handles += requireDecision(sessionA.freeGreatPersonChoice()).handle

            game.turns++
            val turnChanged = sessionA.freeGreatPersonChoice()
            handles += requireDecision(turnChanged).handle
            setCurrentPlayer(game, opponent)
            val authorityChanged = sessionA.freeGreatPersonChoice()
            handles += requireDecision(authorityChanged).handle
            check(handles.distinct().size == handles.size) {
                "A changed observed decision fingerprint reused a session handle"
            }

            setCurrentPlayer(game, human)
            manager.freeGreatPeople = 1
            manager.mayaLimitedFreeGP = 0
            manager.longCountGPPool.clear()
            val reusableSnapshot = sessionA.freeGreatPersonChoice()
            val reusableOption = ordinaryLandOption(reusableSnapshot)
            val rejected = choiceRequest("reusable-after-rejection", reusableSnapshot, "Warrior")
            expectNoMutation(game, PlayerSessionFailure.OPTION_UNAVAILABLE) {
                sessionA.chooseFreeGreatPerson(rejected)
            }
            val accepted = rejected.copy(unitName = reusableOption)
            check(sessionA.chooseFreeGreatPerson(accepted).unitName == reusableOption) {
                "A rejected request ID remained reserved"
            }

            manager.freeGreatPeople = 0
            val exhausted = sessionA.freeGreatPersonChoice()
            check(exhausted.decision == null)
            check(PlayerCommandJson.encode(exhausted).endsWith("\"decision\":null}"))
            expectNoMutation(game, PlayerSessionFailure.DECISION_UNAVAILABLE) {
                sessionA.chooseFreeGreatPerson(
                    choiceRequest("exhausted", reusableSnapshot, reusableOption),
                )
            }
            manager.freeGreatPeople = -1
            check(sessionA.freeGreatPersonChoice().decision == null)
        }
    }

    private fun verifyCommandNamespace(save: Path, data: Path) {
        HeadlessRuntime.load(save, data).use { runtime ->
            val game = runtime.game
            val human = game.getCivilization("The Maya")
            human.greatPeople.freeGreatPeople = 2
            val sessionA = PlayerHost(runtime).bindPlayer(human.civID)
            val sessionB = PlayerHost(runtime).bindPlayer(human.civID)
            val decisionA = sessionA.freeGreatPersonChoice()
            val decisionB = sessionB.freeGreatPersonChoice()
            val cityA = sessionA.cities().cities.single()
            val cityB = sessionB.cities().cities.single()
            val sharedId = "one-command-namespace"
            val renameA = RenameCityRequest(
                requestId = sharedId,
                epoch = decisionA.epoch,
                cityHandle = cityA.handle,
                name = "Renamed Before Choice",
            )
            val renameReceipt = sessionA.renameCity(renameA)

            expectNoMutation(game, PlayerSessionFailure.REQUEST_ID_CONFLICT) {
                sessionA.chooseFreeGreatPerson(
                    choiceRequest(sharedId, decisionA, ordinaryLandOption(decisionA)),
                )
            }
            val choiceB = choiceRequest(
                sharedId,
                decisionB,
                ordinaryLandOption(decisionB),
            )
            val choiceReceipt = sessionB.chooseFreeGreatPerson(choiceB)
            check(choiceReceipt.requestId == renameReceipt.requestId) {
                "Separate sessions did not have independent request-ID namespaces"
            }
            expectNoMutation(game, PlayerSessionFailure.REQUEST_ID_CONFLICT) {
                sessionB.renameCity(
                    RenameCityRequest(
                        requestId = sharedId,
                        epoch = decisionB.epoch,
                        cityHandle = cityB.handle,
                        name = "Must Not Rename",
                    ),
                )
            }
            check(sessionA.renameCity(renameA) == renameReceipt)
            check(human.cities.single().name == renameReceipt.name)
        }
    }

    private fun verifyEmptyDecisions(noCitySave: Path, emptyMayaSave: Path, data: Path) {
        HeadlessRuntime.load(noCitySave, data.resolve("no-city")).use { runtime ->
            val game = runtime.game
            val human = game.getCivilization("The Maya")
            val session = PlayerHost(runtime).bindPlayer(human.civID)
            val snapshot = session.freeGreatPersonChoice()
            val decision = requireDecision(snapshot)
            check(human.cities.isEmpty())
            check(decision.remainingChoices == 1)
            check(!decision.mayaLimited)
            check(decision.options.isEmpty())
            expectNoMutation(game, PlayerSessionFailure.OPTION_UNAVAILABLE) {
                session.chooseFreeGreatPerson(
                    choiceRequest("cityless", snapshot, "Great Scientist"),
                )
            }
            check(human.greatPeople.freeGreatPeople == 1)
            check(session.freeGreatPersonChoice() == snapshot)
        }

        HeadlessRuntime.load(emptyMayaSave, data.resolve("empty-maya-pool")).use { runtime ->
            val game = runtime.game
            val human = game.getCivilization("The Maya")
            val session = PlayerHost(runtime).bindPlayer(human.civID)
            val snapshot = session.freeGreatPersonChoice()
            val decision = requireDecision(snapshot)
            check(decision.remainingChoices == 1)
            check(decision.mayaLimited)
            check(decision.options.isEmpty())
            expectNoMutation(game, PlayerSessionFailure.OPTION_UNAVAILABLE) {
                session.chooseFreeGreatPerson(
                    choiceRequest("empty-maya-pool", snapshot, "Great Scientist"),
                )
            }
            check(human.greatPeople.freeGreatPeople == 1)
            check(human.greatPeople.mayaLimitedFreeGP == 1)
            check(human.greatPeople.longCountGPPool.isEmpty())
        }
    }

    private fun verifyPlacementFailureAndRetry(save: Path, data: Path) {
        HeadlessRuntime.load(save, data).use { runtime ->
            val game = runtime.game
            val human = game.getCivilization("The Maya")
            val manager = human.greatPeople
            val city = human.cities.single()
            val worker = requireNotNull(city.getCenterTile().civilianUnit)
            check(worker.name == "Worker")
            val session = PlayerHost(runtime).bindPlayer(human.civID)
            val snapshot = session.freeGreatPersonChoice()
            val decision = requireDecision(snapshot)
            check(decision.options == listOf("Great Scientist"))
            val request = choiceRequest("blocked-then-retry", snapshot, "Great Scientist")
            val unitIds = human.units.getCivUnits().map { it.id }.toSet()
            val mapUnits = game.tileMap.tileList.flatMap { it.getUnits().toList() }
            val notifications = human.notifications.toList()
            val pool = manager.longCountGPPool.toSet()

            expectFailure(PlayerSessionFailure.PLACEMENT_FAILED) {
                session.chooseFreeGreatPerson(request)
            }
            check(manager.freeGreatPeople == 1 && manager.mayaLimitedFreeGP == 1)
            check(manager.longCountGPPool == pool)
            check(human.units.getCivUnits().map { it.id }.toSet() == unitIds)
            check(game.tileMap.tileList.flatMap { it.getUnits().toList() } == mapUnits)
            check(city.getCenterTile().civilianUnit === worker)
            check(human.notifications == notifications)
            check(session.freeGreatPersonChoice() == snapshot) {
                "Failed placement consumed or replaced the decision"
            }

            worker.destroy()
            val unitsAfterUnblocking = human.units.getCivUnits().map { it.id }.toSet()
            val receipt = session.chooseFreeGreatPerson(request)
            check(receipt.remainingChoices == 0)
            check(manager.freeGreatPeople == 0 && manager.mayaLimitedFreeGP == 0)
            check("Great Scientist" !in manager.longCountGPPool)
            checkAddedUnit(human, unitsAfterUnblocking, "Great Scientist")
        }
    }

    private fun verifyConcurrentDecisions(save: Path, data: Path) {
        HeadlessRuntime.load(save, data.resolve("duplicate")).use { runtime ->
            val game = runtime.game
            val human = game.getCivilization("The Maya")
            val chooser = BlockingChooser()
            val session = PlayerHost(
                runtime,
                { city, name -> city.name = name },
                chooser::choose,
            ).bindPlayer(human.civID)
            val snapshot = session.freeGreatPersonChoice()
            val request = choiceRequest(
                "concurrent-duplicate",
                snapshot,
                ordinaryLandOption(snapshot),
            )
            val gate = chooser.arm()
            val executor = Executors.newFixedThreadPool(3)
            try {
                val first = executor.submit<ChooseFreeGreatPersonReceipt> {
                    session.chooseFreeGreatPerson(request)
                }
                check(gate.entered.await(5, TimeUnit.SECONDS))
                val duplicate = executor.submit<ChooseFreeGreatPersonReceipt> {
                    session.chooseFreeGreatPerson(request)
                }
                val discovery = executor.submit<FreeGreatPersonSnapshot> {
                    session.freeGreatPersonChoice()
                }
                expectStillBlocked(duplicate, "Duplicate request interleaved with its mutation")
                expectStillBlocked(discovery, "Decision discovery interleaved with its mutation")
                gate.release.countDown()
                val firstReceipt = first.get(5, TimeUnit.SECONDS)
                check(duplicate.get(5, TimeUnit.SECONDS) == firstReceipt)
                check(discovery.get(5, TimeUnit.SECONDS).decision == null)
                check(chooser.calls.get() == 1) { "Concurrent duplicate invoked the chooser twice" }
                check(human.greatPeople.freeGreatPeople == 0)
            } finally {
                chooser.releaseAnyWaiter()
                shutdown(executor)
            }
        }

        HeadlessRuntime.load(save, data.resolve("competing-sessions")).use { runtime ->
            val game = runtime.game
            val human = game.getCivilization("The Maya")
            human.greatPeople.freeGreatPeople = 2
            val sessionA = PlayerHost(runtime).bindPlayer(human.civID)
            val sessionB = PlayerHost(runtime).bindPlayer(human.civID)
            val snapshotA = sessionA.freeGreatPersonChoice()
            val snapshotB = sessionB.freeGreatPersonChoice()
            val option = ordinaryLandOption(snapshotA)
            check(requireDecision(snapshotA).handle != requireDecision(snapshotB).handle)
            val unitIds = human.units.getCivUnits().map { it.id }.toSet()
            val executor = Executors.newFixedThreadPool(2)
            try {
                val outcomes = runTogether(
                    executor,
                    listOf(
                        {
                            sessionA.chooseFreeGreatPerson(
                                choiceRequest("competing-a", snapshotA, option),
                            )
                        },
                        {
                            sessionB.chooseFreeGreatPerson(
                                choiceRequest("competing-b", snapshotB, option),
                            )
                        },
                    ),
                )
                val successes = outcomes.mapNotNull { it.receipt }
                val failures = outcomes.mapNotNull { it.failure }
                check(successes.size == 1 && failures.size == 1)
                check(failures.single().reason == PlayerSessionFailure.DECISION_UNAVAILABLE)
                check(human.greatPeople.freeGreatPeople == 1) {
                    "A stale competing handle spent the second entitlement"
                }
                checkAddedUnit(human, unitIds, option)
            } finally {
                shutdown(executor)
            }
        }
    }

    private fun verifyReloadExpiry(save: Path, data: Path) {
        lateinit var oldHost: PlayerHost
        lateinit var oldSession: PlayerSession
        lateinit var oldSnapshot: FreeGreatPersonSnapshot
        lateinit var oldRequest: ChooseFreeGreatPersonRequest
        var oldJson = ""

        HeadlessRuntime.load(save, data.resolve("first")).use { runtime ->
            oldHost = PlayerHost(runtime)
            oldSession = oldHost.bindPlayer("The Maya")
            oldSnapshot = oldSession.freeGreatPersonChoice()
            oldJson = PlayerCommandJson.encode(oldSnapshot)
            oldRequest = choiceRequest(
                "request-from-closed-runtime",
                oldSnapshot,
                ordinaryLandOption(oldSnapshot),
            )
        }

        expectFailure(PlayerSessionFailure.SESSION_CLOSED) { oldSession.summary() }
        expectFailure(PlayerSessionFailure.SESSION_CLOSED) { oldSession.cities() }
        expectFailure(PlayerSessionFailure.SESSION_CLOSED) { oldSession.freeGreatPersonChoice() }
        expectFailure(PlayerSessionFailure.SESSION_CLOSED) {
            oldSession.chooseFreeGreatPerson(oldRequest)
        }
        expectFailure(PlayerSessionFailure.SESSION_CLOSED) { oldHost.bindPlayer("The Maya") }
        check(PlayerCommandJson.encode(oldSnapshot) == oldJson)

        HeadlessRuntime.load(save, data.resolve("second")).use { runtime ->
            val game = runtime.game
            val session = PlayerHost(runtime).bindPlayer("The Maya")
            val snapshot = session.freeGreatPersonChoice()
            check(snapshot.epoch != oldSnapshot.epoch)
            check(requireDecision(snapshot).handle != requireDecision(oldSnapshot).handle)
            expectNoMutation(game, PlayerSessionFailure.STALE_EPOCH) {
                session.chooseFreeGreatPerson(oldRequest)
            }
        }
    }

    private fun verifyMutationFailureHaltsGame(save: Path, data: Path) {
        HeadlessRuntime.load(save, data).use { runtime ->
            val game = runtime.game
            val human = game.getCivilization("The Maya")
            val injected = IllegalStateException("private post-placement diagnostic")
            val host = PlayerHost(
                runtime,
                { city, name -> city.name = name },
                { civilization, unitName ->
                    checkNotNull(civilization.greatPeople.chooseFreeGreatPerson(unitName))
                    throw injected
                },
            )
            val session = host.bindPlayer(human.civID)
            val otherSession = PlayerHost(runtime).bindPlayer(human.civID)
            val snapshot = session.freeGreatPersonChoice()
            val request = choiceRequest(
                "post-placement-failure",
                snapshot,
                ordinaryLandOption(snapshot),
            )
            val unitIds = human.units.getCivUnits().map { it.id }.toSet()

            val failure = expectFailure(PlayerSessionFailure.GAME_HALTED) {
                session.chooseFreeGreatPerson(request)
            }
            check(injected.message.orEmpty() !in failure.message.orEmpty())
            check(runtime.mutationFailure === injected)
            check(human.greatPeople.freeGreatPeople == 0)
            check(human.units.getCivUnits().any { it.id !in unitIds }) {
                "Failure seam did not fail after engine placement"
            }
            expectFailure(PlayerSessionFailure.GAME_HALTED) { session.summary() }
            expectFailure(PlayerSessionFailure.GAME_HALTED) { session.cities() }
            expectFailure(PlayerSessionFailure.GAME_HALTED) { session.freeGreatPersonChoice() }
            expectFailure(PlayerSessionFailure.GAME_HALTED) {
                session.chooseFreeGreatPerson(request.copy(requestId = "after-halt"))
            }
            expectFailure(PlayerSessionFailure.GAME_HALTED) { otherSession.summary() }
            expectFailure(PlayerSessionFailure.GAME_HALTED) { host.bindPlayer(human.civID) }
        }
    }

    private fun prepareCoastalSave(
        source: Path,
        target: Path,
        data: Path,
        humanName: String,
    ): Path {
        HeadlessRuntime.load(source, data).use { runtime ->
            val civilization = runtime.game.getCivilization(humanName)
            val center = checkNotNull(civilization.getCapital()).getCenterTile()
            val coast = center.neighbors.first { tile ->
                !tile.isCityCenter() && tile.getUnits().none()
            }
            coast.setTileResource(null)
            coast.improvement = null
            coast.setTerrainFeatures(emptyList())
            coast.baseTerrain = Constants.coast
            coast.setTerrainTransients()
            Files.createDirectories(target.parent)
            Files.writeString(target, raw(runtime.game))
        }
        return target
    }

    private fun checkAddedUnit(
        civilization: Civilization,
        previousIds: Set<Int>,
        expectedName: String,
    ): MapUnit {
        val added = civilization.units.getCivUnits().filter { it.id !in previousIds }.toList()
        check(added.size == 1) { "$expectedName did not add exactly one unit" }
        return added.single().also { unit ->
            check(unit.name == expectedName)
            check(unit.civ === civilization && unit.owner == civilization.civID)
            check(unit.getTile().getUnits().any { it === unit })
        }
    }

    private fun requireDecision(snapshot: FreeGreatPersonSnapshot): FreeGreatPersonDecision = checkNotNull(snapshot.decision) { "Expected a pending great-person decision" }

    private fun ordinaryLandOption(snapshot: FreeGreatPersonSnapshot): String = "Great Scientist".also { option ->
        check(option in requireDecision(snapshot).options) {
            "Expected $option among ordinary options"
        }
    }

    private fun choiceRequest(
        requestId: String,
        snapshot: FreeGreatPersonSnapshot,
        unitName: String,
    ) = ChooseFreeGreatPersonRequest(
        requestId = requestId,
        epoch = snapshot.epoch,
        decisionHandle = requireDecision(snapshot).handle,
        unitName = unitName,
    )

    private fun setCurrentPlayer(game: GameInfo, civilization: Civilization) {
        game.currentPlayer = civilization.civID
        game.currentPlayerCiv = civilization
    }

    private fun raw(game: GameInfo): String = json().toJson(game)

    private fun expectNoMutation(
        game: GameInfo,
        reason: PlayerSessionFailure,
        operation: () -> Any?,
    ): PlayerSessionException {
        val before = raw(game)
        val failure = expectFailure(reason, operation)
        check(raw(game) == before) { "$reason changed serialized engine state" }
        return failure
    }

    private fun expectFailure(
        reason: PlayerSessionFailure,
        operation: () -> Any?,
    ): PlayerSessionException {
        val failure = runCatching(operation).exceptionOrNull()
        check(failure is PlayerSessionException) {
            "Expected PlayerSessionException($reason), got ${failure?.javaClass?.name ?: "success"}"
        }
        check(failure.reason == reason) { "Expected $reason, got ${failure.reason}" }
        check(failure.message == expectedMessages.getValue(reason)) {
            "Unexpected public error text for $reason: ${failure.message}"
        }
        check(failure.cause == null) { "Player-facing error exposed its engine cause" }
        return failure
    }

    private fun runTogether(
        executor: ExecutorService,
        operations: List<() -> ChooseFreeGreatPersonReceipt>,
    ): List<CommandOutcome> {
        val barrier = CyclicBarrier(operations.size + 1)
        val futures = operations.map { operation ->
            executor.submit<CommandOutcome> {
                barrier.await(5, TimeUnit.SECONDS)
                try {
                    CommandOutcome(receipt = operation(), failure = null)
                } catch (failure: PlayerSessionException) {
                    CommandOutcome(receipt = null, failure = failure)
                }
            }
        }
        barrier.await(5, TimeUnit.SECONDS)
        return futures.map { it.get(5, TimeUnit.SECONDS) }
    }

    private fun expectStillBlocked(future: Future<*>, message: String) {
        val result = runCatching { future.get(150, TimeUnit.MILLISECONDS) }
        check(result.exceptionOrNull() is TimeoutException) { message }
    }

    private fun shutdown(executor: ExecutorService) {
        executor.shutdownNow()
        check(executor.awaitTermination(5, TimeUnit.SECONDS)) { "Probe executor did not stop" }
    }

    private data class CommandOutcome(
        val receipt: ChooseFreeGreatPersonReceipt?,
        val failure: PlayerSessionException?,
    )

    private data class ChoiceGate(
        val entered: CountDownLatch = CountDownLatch(1),
        val release: CountDownLatch = CountDownLatch(1),
    )

    private class BlockingChooser {
        val calls = AtomicInteger()
        private val nextGate = AtomicReference<ChoiceGate?>()

        fun arm(): ChoiceGate = ChoiceGate().also { gate ->
            check(nextGate.compareAndSet(null, gate)) { "A choice gate is already armed" }
        }

        fun choose(civilization: Civilization, unitName: String): MapUnit? {
            calls.incrementAndGet()
            nextGate.get()?.let { gate ->
                gate.entered.countDown()
                try {
                    check(gate.release.await(5, TimeUnit.SECONDS)) { "Timed out releasing choice" }
                } finally {
                    nextGate.compareAndSet(gate, null)
                }
            }
            return civilization.greatPeople.chooseFreeGreatPerson(unitName)
        }

        fun releaseAnyWaiter() {
            nextGate.getAndSet(null)?.release?.countDown()
        }
    }

    private val expectedMessages = mapOf(
        PlayerSessionFailure.SESSION_CLOSED to "Player session is closed",
        PlayerSessionFailure.PLAYER_UNAVAILABLE to "Player is unavailable",
        PlayerSessionFailure.QUERY_FAILED to "Player query failed",
        PlayerSessionFailure.NOT_YOUR_TURN to "It is not your turn",
        PlayerSessionFailure.STALE_EPOCH to "Game epoch is stale",
        PlayerSessionFailure.CITY_UNAVAILABLE to "City is unavailable",
        PlayerSessionFailure.INVALID_ARGUMENT to "Invalid argument",
        PlayerSessionFailure.REQUEST_ID_CONFLICT to "Request ID conflicts with an earlier request",
        PlayerSessionFailure.GAME_HALTED to "Game is halted",
        PlayerSessionFailure.DECISION_UNAVAILABLE to "Decision is unavailable",
        PlayerSessionFailure.OPTION_UNAVAILABLE to "Option is unavailable",
        PlayerSessionFailure.PLACEMENT_FAILED to "Unit could not be placed",
    )
}
