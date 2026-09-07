package uncivai

import com.unciv.json.json
import com.unciv.logic.GameInfo
import com.unciv.logic.city.City
import com.unciv.logic.city.CityFlags
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerType
import uncivai.player.OwnedCitiesSnapshot
import uncivai.player.PlayerCommandJson
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.RenameCityReceipt
import uncivai.player.RenameCityRequest
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

/** Exercises S03 inside the display-less JVM that owns the real loaded engine game. */
object S03CityRenameProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        require(args.size == 2) {
            "Usage: S03CityRenameProbe <absolute-save> <absolute-data-directory>"
        }
        val save = Path.of(args[0])
        val dataDirectory = Path.of(args[1])
        require(save.isAbsolute && dataDirectory.isAbsolute) { "Probe paths must be absolute" }

        verifyObservationsAndRetries(save, dataDirectory.resolve("observations-and-retries"))
        verifyValidationAndAuthority(save, dataDirectory.resolve("validation-and-authority"))
        verifyOwnershipLoss(save, dataDirectory.resolve("ownership-loss"))
        verifyRemovedCity(save, dataDirectory.resolve("removed-city"))
        verifyReloadExpiry(save, dataDirectory.resolve("reload-expiry"))
        verifyConcurrentIdempotency(save, dataDirectory.resolve("concurrent-idempotency"))
        verifySerializationWithSnapshotsAndClose(save, dataDirectory.resolve("serialization"))
        verifyMutationFailureHaltsGame(save, dataDirectory.resolve("mutation-failure"))

        println("S03-VERIFIED:${save.fileName}")
    }

    private fun verifyObservationsAndRetries(save: Path, dataDirectory: Path) {
        HeadlessRuntime.load(save, dataDirectory).use { runtime ->
            val game = runtime.game
            S01ScenarioState.verify(game)
            val rome = game.getCivilization("Rome")
            val greece = game.getCivilization("Greece")
            val romeCity = rome.cities.single()
            val greekCity = greece.cities.single()
            val host = PlayerHost(runtime)
            val sessionA = host.bindPlayer("Rome")
            val sessionB = host.bindPlayer("Rome")
            val greekSession = host.bindPlayer("Greece")

            val rawBeforeObservations = raw(game)
            val snapshotA = sessionA.cities()
            val repeatedA = sessionA.cities()
            val snapshotB = sessionB.cities()
            val greekSnapshot = greekSession.cities()
            check(snapshotA == repeatedA) { "A session changed its city handle between reads" }
            check(snapshotA.epoch == snapshotB.epoch && snapshotA.epoch == greekSnapshot.epoch) {
                "Sessions in one runtime observed different epochs"
            }
            check(snapshotA.cities.single().name == romeCity.name)
            check(greekSnapshot.cities.single().name == greekCity.name)
            check(snapshotA.cities.single().handle != snapshotB.cities.single().handle) {
                "Separate sessions reused a city handle"
            }
            check(snapshotA.cities.single().handle != romeCity.id) { "A raw city ID was exposed" }
            check(raw(game) == rawBeforeObservations) { "City observations changed engine state" }

            check(!rome.knows(greece)) { "Hidden-name check requires an unmet opponent" }
            val originalGreekName = greekCity.name
            val hiddenGreekName = "Unseen opponent city ${UUID.randomUUID()}"
            greekCity.name = hiddenGreekName
            val rawHiddenWorld = raw(game)
            check(sessionA.cities() == snapshotA) {
                "An unmet opponent's private city name changed Rome's city snapshot"
            }
            val hiddenFailure = expectNoMutation(game, PlayerSessionFailure.CITY_UNAVAILABLE) {
                sessionA.renameCity(
                    request(
                        "foreign-hidden-name",
                        snapshotA,
                        greekSnapshot.cities.single().handle,
                        "Roma",
                    ),
                )
            }
            check(hiddenGreekName !in hiddenFailure.message.orEmpty())
            check(greekSnapshot.cities.single().handle !in hiddenFailure.message.orEmpty())
            check(raw(game) == rawHiddenWorld)
            greekCity.name = originalGreekName

            verifyDetachedSnapshot(sessionA, romeCity)

            val summaryBeforeRename = sessionA.summary()
            val oldSnapshot = sessionA.cities()
            val oldSnapshotJson = PlayerCommandJson.encode(oldSnapshot)
            val requestA = request("rename-a", oldSnapshot, oldSnapshot.cities.single().handle, "Nova Roma")
            val expectedAfterA = expectedRawAfterNameChange(game, romeCity, requestA.name)
            val receiptA = sessionA.renameCity(requestA)
            check(raw(game) == expectedAfterA) { "Rename A changed more than the city name" }
            check(romeCity.name == "Nova Roma")
            check(
                receiptA == RenameCityReceipt(
                    requestId = requestA.requestId,
                    epoch = requestA.epoch,
                    cityHandle = requestA.cityHandle,
                    previousName = oldSnapshot.cities.single().name,
                    name = requestA.name,
                ),
            )
            check(sessionA.summary() == summaryBeforeRename) {
                "Rename changed the existing summary or a pending free choice"
            }
            check(PlayerCommandJson.encode(oldSnapshot) == oldSnapshotJson) {
                "A previously returned city snapshot remained attached to the engine"
            }
            check(sessionA.cities().cities.single().name == "Nova Roma")

            val requestB = request(
                "rename-b",
                snapshotB,
                snapshotB.cities.single().handle,
                "New Rome",
            )
            val receiptB = sessionB.renameCity(requestB)
            check(receiptB.previousName == "Nova Roma")
            check(romeCity.name == "New Rome")
            check(sessionA.renameCity(requestA) == receiptA)
            check(romeCity.name == "New Rome") { "Retry A reapplied over rename B" }

            setCurrentPlayer(game, greece)
            val rawBeforeOutOfTurnRetry = raw(game)
            check(sessionA.renameCity(requestA) == receiptA) {
                "An exact historical retry was not available after the turn changed"
            }
            check(raw(game) == rawBeforeOutOfTurnRetry) { "Historical retry mutated the game" }
            expectNoMutation(game, PlayerSessionFailure.REQUEST_ID_CONFLICT) {
                sessionA.renameCity(requestA.copy(name = "Conflicting Rome"))
            }
            setCurrentPlayer(game, rome)

            val detachedReceipt = receiptA.copy(name = "Tampered outside the host")
            check(detachedReceipt != receiptA)
            check(sessionA.renameCity(requestA) == receiptA) { "A copied receipt changed the cache" }
            check(romeCity.name == "New Rome")

            val sharedRequestA = request(
                "same-id-in-two-sessions",
                sessionA.cities(),
                snapshotA.cities.single().handle,
                "Roma Tertia",
            )
            val sharedRequestB = request(
                "same-id-in-two-sessions",
                sessionB.cities(),
                snapshotB.cities.single().handle,
                "Roma Quarta",
            )
            val sharedReceiptA = sessionA.renameCity(sharedRequestA)
            val sharedReceiptB = sessionB.renameCity(sharedRequestB)
            check(sharedReceiptA.requestId == sharedReceiptB.requestId)
            check(sharedReceiptA.name == "Roma Tertia" && sharedReceiptB.name == "Roma Quarta")
            check(romeCity.name == "Roma Quarta") {
                "Separate sessions did not have independent request-ID namespaces"
            }
            check(sessionA.renameCity(sharedRequestA) == sharedReceiptA)
            check(romeCity.name == "Roma Quarta")

            rome.playerType = PlayerType.AI
            expectNoMutation(game, PlayerSessionFailure.PLAYER_UNAVAILABLE) {
                sessionA.renameCity(requestA)
            }
            rome.playerType = PlayerType.Human
            val rawBeforeRestoredReplay = raw(game)
            check(sessionA.renameCity(requestA) == receiptA)
            check(raw(game) == rawBeforeRestoredReplay)

            val romeIndex = game.civilizations.indexOf(rome)
            check(romeIndex >= 0 && game.civilizations.remove(rome))
            expectNoMutation(game, PlayerSessionFailure.PLAYER_UNAVAILABLE) {
                sessionA.renameCity(requestA)
            }
            game.civilizations.add(romeIndex, rome)
        }
    }

    private fun verifyValidationAndAuthority(save: Path, dataDirectory: Path) {
        HeadlessRuntime.load(save, dataDirectory).use { runtime ->
            val game = runtime.game
            S01ScenarioState.verify(game)
            val rome = game.getCivilization("Rome")
            val greece = game.getCivilization("Greece")
            val city = rome.cities.single()
            val host = PlayerHost(runtime)
            val session = host.bindPlayer("Rome")
            val snapshot = session.cities()
            val handle = snapshot.cities.single().handle
            val greekSnapshot = host.bindPlayer("Greece").cities()

            expectNoMutation(game, PlayerSessionFailure.STALE_EPOCH) {
                session.renameCity(
                    request("stale", snapshot, handle, "Stale").copy(epoch = "not-this-runtime"),
                )
            }

            val unknownOne = expectNoMutation(game, PlayerSessionFailure.CITY_UNAVAILABLE) {
                session.renameCity(request("unknown-1", snapshot, "unknown-one", "Unknown"))
            }
            val unknownTwo = expectNoMutation(game, PlayerSessionFailure.CITY_UNAVAILABLE) {
                session.renameCity(request("unknown-2", snapshot, "unknown-two", "Unknown"))
            }
            check(unknownOne.message == unknownTwo.message) { "Unknown handles produced distinct errors" }
            expectNoMutation(game, PlayerSessionFailure.CITY_UNAVAILABLE) {
                session.renameCity(
                    request(
                        "foreign",
                        snapshot,
                        greekSnapshot.cities.single().handle,
                        "Foreign",
                    ),
                )
            }

            setCurrentPlayer(game, greece)
            expectNoMutation(game, PlayerSessionFailure.NOT_YOUR_TURN) {
                session.renameCity(request("out-of-turn", snapshot, handle, "Out of turn"))
            }
            setCurrentPlayer(game, rome)

            rome.playerType = PlayerType.AI
            expectNoMutation(game, PlayerSessionFailure.PLAYER_UNAVAILABLE) {
                session.renameCity(request("became-ai", snapshot, handle, "Unavailable"))
            }
            rome.playerType = PlayerType.Human

            val romeIndex = game.civilizations.indexOf(rome)
            check(romeIndex >= 0 && game.civilizations.remove(rome))
            expectNoMutation(game, PlayerSessionFailure.PLAYER_UNAVAILABLE) {
                session.renameCity(request("removed-player", snapshot, handle, "Unavailable"))
            }
            game.civilizations.add(romeIndex, rome)

            val invalidNames = listOf(
                "",
                "a".repeat(33),
                "line\nfeed",
                "carriage\rreturn",
            ) + "[]{}\"\\<>".map { "bad${it}name" }
            for ((index, invalidName) in invalidNames.withIndex()) {
                expectNoMutation(game, PlayerSessionFailure.INVALID_ARGUMENT) {
                    session.renameCity(request("invalid-name-$index", snapshot, handle, invalidName))
                }
            }

            val invalidRequestIds = listOf("", "r".repeat(129), "line\nfeed", "non-ascii-π")
            for (invalidRequestId in invalidRequestIds) {
                expectNoMutation(game, PlayerSessionFailure.INVALID_ARGUMENT) {
                    session.renameCity(request("valid-placeholder", snapshot, handle, "Valid").copy(requestId = invalidRequestId))
                }
            }

            val reusableRequestId = request("reusable-after-rejection", snapshot, handle, "")
            expectNoMutation(game, PlayerSessionFailure.INVALID_ARGUMENT) {
                session.renameCity(reusableRequestId)
            }
            val acceptedAfterRejection = reusableRequestId.copy(name = "Accepted After Rejection")
            val expectedAfterRejection = expectedRawAfterNameChange(game, city, acceptedAfterRejection.name)
            check(session.renameCity(acceptedAfterRejection).name == acceptedAfterRejection.name)
            check(raw(game) == expectedAfterRejection) { "A rejected request ID was incorrectly reserved" }

            city.isPuppet = true
            city.setFlag(CityFlags.Resistance, 2)
            check(city.isInResistance())
            val duplicateName = greece.cities.single().name
            val validNames = listOf(
                " ",
                "a".repeat(32),
                "𐐀".repeat(16),
                duplicateName,
                city.name,
            )
            for ((index, validName) in validNames.withIndex()) {
                val renameRequest = request("valid-name-$index", snapshot, handle, validName)
                val expected = expectedRawAfterNameChange(game, city, validName)
                val receipt = session.renameCity(renameRequest)
                check(receipt.name == validName)
                check(city.name == validName)
                check(raw(game) == expected) { "Valid rename '$index' changed unrelated game state" }
            }
            check(city.isPuppet && city.isInResistance())
            check(rome.policies.freePolicies == 2 && rome.tech.freeTechs == 1)
            check(rome.greatPeople.freeGreatPeople == 1)
        }
    }

    private fun verifyOwnershipLoss(save: Path, dataDirectory: Path) {
        HeadlessRuntime.load(save, dataDirectory).use { runtime ->
            val game = runtime.game
            S01ScenarioState.verify(game)
            val rome = game.getCivilization("Rome")
            val greece = game.getCivilization("Greece")
            val city = rome.cities.single()
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val snapshot = session.cities()
            val accepted = request(
                "accepted-before-transfer",
                snapshot,
                snapshot.cities.single().handle,
                "Pre-Transfer",
            )
            val receipt = session.renameCity(accepted)
            city.moveToCiv(greece)
            check(city.civ === greece && city !in rome.cities && city in greece.cities)

            val rawAfterTransfer = raw(game)
            check(session.renameCity(accepted) == receipt) {
                "An accepted request lost its historical receipt after ownership changed"
            }
            check(raw(game) == rawAfterTransfer)
            expectNoMutation(game, PlayerSessionFailure.CITY_UNAVAILABLE) {
                session.renameCity(
                    request(
                        "new-after-transfer",
                        snapshot,
                        snapshot.cities.single().handle,
                        "Must Not Apply",
                    ),
                )
            }
        }
    }

    private fun verifyRemovedCity(save: Path, dataDirectory: Path) {
        HeadlessRuntime.load(save, dataDirectory).use { runtime ->
            val game = runtime.game
            S01ScenarioState.verify(game)
            val rome = game.getCivilization("Rome")
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val snapshot = session.cities()
            rome.cities = emptyList()
            expectNoMutation(game, PlayerSessionFailure.CITY_UNAVAILABLE) {
                session.renameCity(
                    request(
                        "removed-city",
                        snapshot,
                        snapshot.cities.single().handle,
                        "Must Not Apply",
                    ),
                )
            }
        }
    }

    private fun verifyReloadExpiry(save: Path, dataDirectory: Path) {
        lateinit var oldHost: PlayerHost
        lateinit var oldSession: PlayerSession
        lateinit var oldSnapshot: OwnedCitiesSnapshot
        lateinit var oldRequest: RenameCityRequest
        lateinit var oldReceipt: RenameCityReceipt
        var oldSnapshotJson = ""

        HeadlessRuntime.load(save, dataDirectory.resolve("first")).use { runtime ->
            oldHost = PlayerHost(runtime)
            oldSession = oldHost.bindPlayer("Rome")
            oldSnapshot = oldSession.cities()
            oldSnapshotJson = PlayerCommandJson.encode(oldSnapshot)
            oldRequest = request(
                "accepted-before-close",
                oldSnapshot,
                oldSnapshot.cities.single().handle,
                "Before Close",
            )
            oldReceipt = oldSession.renameCity(oldRequest)
        }

        expectFailure(PlayerSessionFailure.SESSION_CLOSED) { oldSession.summary() }
        expectFailure(PlayerSessionFailure.SESSION_CLOSED) { oldSession.cities() }
        expectFailure(PlayerSessionFailure.SESSION_CLOSED) { oldSession.renameCity(oldRequest) }
        expectFailure(PlayerSessionFailure.SESSION_CLOSED) { oldHost.bindPlayer("Rome") }
        check(PlayerCommandJson.encode(oldSnapshot) == oldSnapshotJson)
        check(oldReceipt.name == "Before Close")

        HeadlessRuntime.load(save, dataDirectory.resolve("second")).use { runtime ->
            val game = runtime.game
            val newSession = PlayerHost(runtime).bindPlayer("Rome")
            val newSnapshot = newSession.cities()
            check(newSnapshot.epoch != oldSnapshot.epoch) { "Reload reused a runtime epoch" }
            check(newSnapshot.cities.single().handle != oldSnapshot.cities.single().handle) {
                "Reload reused a session city handle"
            }
            expectNoMutation(game, PlayerSessionFailure.STALE_EPOCH) {
                newSession.renameCity(oldRequest)
            }
            val freshRequest = request(
                "accepted-before-close",
                newSnapshot,
                newSnapshot.cities.single().handle,
                "After Reload",
            )
            check(newSession.renameCity(freshRequest).name == "After Reload")
            check(game.getCivilization("Rome").cities.single().name == "After Reload")
            expectFailure(PlayerSessionFailure.SESSION_CLOSED) { oldSession.renameCity(oldRequest) }
        }
    }

    private fun verifyConcurrentIdempotency(save: Path, dataDirectory: Path) {
        HeadlessRuntime.load(save, dataDirectory).use { runtime ->
            val game = runtime.game
            S01ScenarioState.verify(game)
            val mutationCount = AtomicInteger()
            val host = PlayerHost(runtime) { city, name ->
                mutationCount.incrementAndGet()
                city.name = name
            }
            val session = host.bindPlayer("Rome")
            val snapshot = session.cities()
            val handle = snapshot.cities.single().handle
            val executor = Executors.newFixedThreadPool(2)
            try {
                val duplicate = request("concurrent-duplicate", snapshot, handle, "Concurrent Rome")
                val duplicateOutcomes = runTogether(executor, listOf({ session.renameCity(duplicate) }, { session.renameCity(duplicate) }))
                check(duplicateOutcomes.all { it.receipt != null && it.failure == null })
                check(duplicateOutcomes.map { it.receipt }.distinct() == listOf(duplicateOutcomes.first().receipt))
                check(mutationCount.get() == 1) { "Concurrent duplicate mutated more than once" }
                check(game.getCivilization("Rome").cities.single().name == "Concurrent Rome")

                val first = request("concurrent-conflict", snapshot, handle, "Concurrent Alpha")
                val second = first.copy(name = "Concurrent Beta")
                val conflictOutcomes = runTogether(executor, listOf({ session.renameCity(first) }, { session.renameCity(second) }))
                val successes = conflictOutcomes.mapNotNull { it.receipt }
                val failures = conflictOutcomes.mapNotNull { it.failure }
                check(successes.size == 1 && failures.size == 1)
                check(failures.single().reason == PlayerSessionFailure.REQUEST_ID_CONFLICT)
                check(mutationCount.get() == 2) { "Concurrent conflict was not atomic" }
                check(game.getCivilization("Rome").cities.single().name == successes.single().name)
            } finally {
                shutdown(executor)
            }
        }
    }

    private fun verifySerializationWithSnapshotsAndClose(save: Path, dataDirectory: Path) {
        val runtime = HeadlessRuntime.load(save, dataDirectory)
        val blockingRenamer = BlockingRenamer()
        val session = PlayerHost(runtime, blockingRenamer::rename).bindPlayer("Rome")
        val snapshot = session.cities()
        val handle = snapshot.cities.single().handle
        val executor = Executors.newFixedThreadPool(2)
        try {
            val snapshotGate = blockingRenamer.arm()
            val renameBeforeSnapshot = executor.submit<RenameCityReceipt> {
                session.renameCity(request("serialize-snapshot", snapshot, handle, "Snapshot Barrier"))
            }
            check(snapshotGate.entered.await(5, TimeUnit.SECONDS))
            val concurrentSnapshot = executor.submit<OwnedCitiesSnapshot> { session.cities() }
            expectStillBlocked(concurrentSnapshot, "City snapshot interleaved with rename")
            snapshotGate.release.countDown()
            check(renameBeforeSnapshot.get(5, TimeUnit.SECONDS).name == "Snapshot Barrier")
            check(concurrentSnapshot.get(5, TimeUnit.SECONDS).cities.single().name == "Snapshot Barrier")

            val closeGate = blockingRenamer.arm()
            val renameBeforeClose = executor.submit<RenameCityReceipt> {
                session.renameCity(request("serialize-close", snapshot, handle, "Close Barrier"))
            }
            check(closeGate.entered.await(5, TimeUnit.SECONDS))
            val close = executor.submit { runtime.close() }
            expectStillBlocked(close, "Runtime close interleaved with rename")
            closeGate.release.countDown()
            check(renameBeforeClose.get(5, TimeUnit.SECONDS).name == "Close Barrier")
            close.get(5, TimeUnit.SECONDS)
            expectFailure(PlayerSessionFailure.SESSION_CLOSED) { session.cities() }
        } finally {
            blockingRenamer.releaseAnyWaiter()
            runtime.close()
            shutdown(executor)
        }
    }

    private fun verifyMutationFailureHaltsGame(save: Path, dataDirectory: Path) {
        HeadlessRuntime.load(save, dataDirectory).use { runtime ->
            val game = runtime.game
            S01ScenarioState.verify(game)
            val injectedFailure = IllegalStateException("private post-mutation diagnostic")
            val session = PlayerHost(runtime) { city, name ->
                city.name = name
                throw injectedFailure
            }.bindPlayer("Rome")
            val snapshot = session.cities()
            val request = request(
                "mutate-then-fail",
                snapshot,
                snapshot.cities.single().handle,
                "Possibly Applied",
            )

            val firstFailure = expectFailure(PlayerSessionFailure.GAME_HALTED) {
                session.renameCity(request)
            }
            check(injectedFailure.message.orEmpty() !in firstFailure.message.orEmpty())
            check(runtime.mutationFailure === injectedFailure)
            check(game.getCivilization("Rome").cities.single().name == "Possibly Applied") {
                "Failure seam did not exercise a failure after mutation"
            }
            expectFailure(PlayerSessionFailure.GAME_HALTED) { session.summary() }
            expectFailure(PlayerSessionFailure.GAME_HALTED) { session.cities() }
            expectFailure(PlayerSessionFailure.GAME_HALTED) {
                session.renameCity(request.copy(requestId = "after-halt"))
            }
        }
    }

    private fun verifyDetachedSnapshot(session: PlayerSession, engineCity: City) {
        val detached = session.cities()
        val detachedJson = PlayerCommandJson.encode(detached)
        val attemptedMutation = runCatching {
            @Suppress("UNCHECKED_CAST")
            (detached.cities as MutableList<Any?>).clear()
        }
        if (attemptedMutation.isFailure) {
            check(PlayerCommandJson.encode(detached) == detachedJson)
        }
        val changedCopy = detached.cities.firstOrNull()?.copy(name = "Changed detached copy")
        check(changedCopy == null || changedCopy.name != engineCity.name)
        val fresh = session.cities()
        check(fresh.cities.single().name == engineCity.name) {
            "Mutating or copying a returned city list changed the engine"
        }
    }

    private fun expectedRawAfterNameChange(game: GameInfo, city: City, name: String): String {
        val previousName = city.name
        val baseline = raw(game)
        city.name = name
        val expected = raw(game)
        city.name = previousName
        check(raw(game) == baseline) { "Expected-state construction changed the baseline" }
        return expected
    }

    private fun request(
        requestId: String,
        snapshot: OwnedCitiesSnapshot,
        handle: String,
        name: String,
    ) = RenameCityRequest(requestId, snapshot.epoch, handle, name)

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
        operations: List<() -> RenameCityReceipt>,
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
        val receipt: RenameCityReceipt?,
        val failure: PlayerSessionException?,
    )

    private data class RenameGate(
        val entered: CountDownLatch = CountDownLatch(1),
        val release: CountDownLatch = CountDownLatch(1),
    )

    private class BlockingRenamer {
        private val nextGate = AtomicReference<RenameGate?>()

        fun arm(): RenameGate = RenameGate().also { gate ->
            check(nextGate.compareAndSet(null, gate)) { "A rename gate is already armed" }
        }

        fun rename(city: City, name: String) {
            nextGate.get()?.let { gate ->
                gate.entered.countDown()
                try {
                    check(gate.release.await(5, TimeUnit.SECONDS)) { "Timed out releasing rename" }
                } finally {
                    nextGate.compareAndSet(gate, null)
                }
            }
            city.name = name
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
    )
}
