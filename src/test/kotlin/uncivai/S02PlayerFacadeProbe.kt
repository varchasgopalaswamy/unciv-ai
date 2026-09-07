package uncivai

import com.unciv.json.json
import com.unciv.logic.GameInfo
import com.unciv.logic.city.City
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerType
import uncivai.player.PlayerHost
import uncivai.player.PlayerSession
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerSummary
import uncivai.player.PlayerSummaryJson
import java.nio.file.Path

/** Exercises the player facade in the display-less JVM that owns the engine runtime. */
object S02PlayerFacadeProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        require(args.size == 2) {
            "Usage: S02PlayerFacadeProbe <absolute-save> <absolute-data-directory>"
        }
        val save = Path.of(args[0])
        val dataDirectory = Path.of(args[1])
        require(save.isAbsolute && dataDirectory.isAbsolute) { "Probe paths must be absolute" }

        val baseline = verifyBoundClientsAndCaptureBaseline(save, dataDirectory.resolve("baseline"))
        val hiddenVariant = captureHiddenVariant(save, dataDirectory.resolve("hidden-variant"))
        check(baseline.playerOutput == hiddenVariant.playerOutput) {
            "An unmet player's private state changed Rome's serialized summary"
        }
        check(baseline.rawGame != hiddenVariant.rawGame) {
            "Hidden-state variant did not actually change the serialized engine state"
        }

        verifyAuthorityFailuresAndReload(save, dataDirectory.resolve("authority"))
        println("S02-VERIFIED:${save.fileName}")
    }

    private fun verifyBoundClientsAndCaptureBaseline(save: Path, dataDirectory: Path): CapturedWorld = HeadlessRuntime.load(save, dataDirectory).use { runtime ->
        val game = runtime.game
        S01ScenarioState.verify(game)
        setDistinctPlayerState(game)

        val host = PlayerHost(runtime)
        val romeClient = ScriptedClient(host.bindPlayer("Rome"))
        val greeceClient = ScriptedClient(host.bindPlayer("Greece"))
        val rawBeforeQueries = json().toJson(game)

        val rome = romeClient.summary()
        val greece = greeceClient.summary()
        verifySummary(rome, game.getCivilization("Rome"), game)
        verifySummary(greece, game.getCivilization("Greece"), game)
        check(rome.treasury == 321 && rome.currentResearch == "Pottery")
        check(greece.treasury == 654 && greece.currentResearch == "Mining")
        check(rome.freePolicies == 2 && greece.freePolicies == 1)
        check(rome.freeTechnologies == 1 && greece.freeTechnologies == 2)
        check(rome.freeGreatPeople == 1 && greece.freeGreatPeople == 2)
        check(rome != greece) { "Distinct player state produced identical summaries" }

        val repeatedOutput = listOf(
            PlayerSummaryJson.encode(rome),
            romeClient.serializedSummary(),
        )
        check(repeatedOutput.distinct().size == 1) { "Repeated summary queries were unstable" }
        greeceClient.summary()
        check(json().toJson(game) == rawBeforeQueries) {
            "A summary query changed serialized engine state"
        }

        verifyFixedIdentity(game, romeClient, greeceClient)
        verifyDetachedSummary(game, romeClient)
        CapturedWorld(repeatedOutput, rawBeforeQueries)
    }

    private fun captureHiddenVariant(save: Path, dataDirectory: Path): CapturedWorld = HeadlessRuntime.load(save, dataDirectory).use { runtime ->
        val game = runtime.game
        S01ScenarioState.verify(game)
        setDistinctPlayerState(game)

        val rome = game.getCivilization("Rome")
        val greece = game.getCivilization("Greece")
        check(!rome.knows(greece)) { "Paired-world test requires Greece to be unmet by Rome" }
        greece.addGold(9_000)
        greece.tech.techsToResearch.clear()
        greece.tech.techsToResearch.add("Writing")
        val hiddenUnit = greece.units.getCivUnits().firstOrNull {
            it.getTile() !in rome.viewableTiles
        }
        checkNotNull(hiddenUnit) { "Paired-world test requires an unseen Greek unit" }
        hiddenUnit.apply {
            health = 7
            currentMovement = 0f
        }

        val rawBeforeQueries = json().toJson(game)
        val client = ScriptedClient(PlayerHost(runtime).bindPlayer("Rome"))
        val output = listOf(client.serializedSummary(), client.serializedSummary())
        check(output.distinct().size == 1) { "Repeated hidden-variant queries were unstable" }
        check(json().toJson(game) == rawBeforeQueries) {
            "A hidden-variant summary query changed serialized engine state"
        }
        CapturedWorld(output, rawBeforeQueries)
    }

    private fun verifyFixedIdentity(
        game: GameInfo,
        romeClient: ScriptedClient,
        greeceClient: ScriptedClient,
    ) {
        val rome = game.getCivilization("Rome")
        val greece = game.getCivilization("Greece")
        game.currentPlayer = "Greece"
        game.currentPlayerCiv = greece

        val romeAfterSelectionChange = romeClient.summary()
        val greeceAfterSelectionChange = greeceClient.summary()
        check(romeAfterSelectionChange.civilization == "Rome")
        check(romeAfterSelectionChange.treasury == 321)
        check(romeAfterSelectionChange.currentResearch == "Pottery")
        check(!romeAfterSelectionChange.isYourTurn)
        check(greeceAfterSelectionChange.civilization == "Greece")
        check(greeceAfterSelectionChange.treasury == 654)
        check(greeceAfterSelectionChange.currentResearch == "Mining")
        check(greeceAfterSelectionChange.isYourTurn)

        game.currentPlayer = "Rome"
        game.currentPlayerCiv = rome
    }

    private fun verifyDetachedSummary(game: GameInfo, romeClient: ScriptedClient) {
        val rome = game.getCivilization("Rome")
        val detached = romeClient.summary()
        val detachedJson = PlayerSummaryJson.encode(detached)
        rome.addGold(79)
        rome.tech.techsToResearch.clear()
        rome.tech.techsToResearch.add("Writing")
        game.turns += 1

        check(detached.treasury == 321)
        check(detached.currentResearch == "Pottery")
        check(detached.turn == 17)
        check(PlayerSummaryJson.encode(detached) == detachedJson) {
            "A previously returned summary remained attached to mutable engine state"
        }

        val updated = romeClient.summary()
        check(updated.treasury == 400)
        check(updated.currentResearch == "Writing")
        check(updated.turn == 18)
    }

    private fun verifyAuthorityFailuresAndReload(save: Path, dataDirectory: Path) {
        lateinit var expiredSession: PlayerSession
        lateinit var expiredHost: PlayerHost
        HeadlessRuntime.load(save, dataDirectory.resolve("first")).use { runtime ->
            val game = runtime.game
            S01ScenarioState.verify(game)
            expiredHost = PlayerHost(runtime)

            expectFailure(PlayerSessionFailure.PLAYER_UNAVAILABLE) {
                expiredHost.bindPlayer("Unknown private identifier one")
            }
            expectFailure(PlayerSessionFailure.PLAYER_UNAVAILABLE) {
                expiredHost.bindPlayer("Unknown private identifier two")
            }

            val greece = game.getCivilization("Greece")
            greece.playerType = PlayerType.AI
            expectFailure(PlayerSessionFailure.PLAYER_UNAVAILABLE) {
                expiredHost.bindPlayer("Greece")
            }
            greece.playerType = PlayerType.Human

            val livingCities = greece.cities
            greece.cities = emptyList()
            check(greece.isDefeated())
            check(expiredHost.bindPlayer("Greece").summary().cityCount == 0) {
                "A defeated Human major player lost read access"
            }
            greece.cities = livingCities

            val spectator = game.getSpectator("s02-spectator")
            check(spectator.isSpectator() && spectator.isHuman())
            expectFailure(PlayerSessionFailure.PLAYER_UNAVAILABLE) {
                expiredHost.bindPlayer(spectator.civID)
            }
            check(game.civilizations.remove(spectator))

            val greeceSession = expiredHost.bindPlayer("Greece")
            greece.playerType = PlayerType.AI
            expectFailure(PlayerSessionFailure.PLAYER_UNAVAILABLE) { greeceSession.summary() }
            greece.playerType = PlayerType.Human
            check(greeceSession.summary().civilization == "Greece")

            val greeceIndex = game.civilizations.indexOf(greece)
            check(greeceIndex >= 0 && game.civilizations.remove(greece))
            expectFailure(PlayerSessionFailure.PLAYER_UNAVAILABLE) { greeceSession.summary() }
            game.civilizations.add(greeceIndex, greece)
            check(greeceSession.summary().civilization == "Greece")

            expiredSession = expiredHost.bindPlayer("Rome")
            val rome = game.getCivilization("Rome")
            val realCities = rome.cities
            rome.cities = object : AbstractList<City>() {
                override val size: Int
                    get() = error("sensitive City manager failure")

                override fun get(index: Int): City = error("sensitive City manager failure")
            }
            val queryFailure = expectFailure(PlayerSessionFailure.QUERY_FAILED) {
                expiredSession.summary()
            }
            check(!queryFailure.message.orEmpty().contains("City"))
            rome.cities = realCities
            check(expiredSession.summary().civilization == "Rome")
        }

        expectFailure(PlayerSessionFailure.SESSION_CLOSED) { expiredSession.summary() }
        expectFailure(PlayerSessionFailure.SESSION_CLOSED) { expiredHost.bindPlayer("Rome") }

        HeadlessRuntime.load(save, dataDirectory.resolve("reloaded")).use { runtime ->
            val reloadedRome = runtime.game.getCivilization("Rome")
            reloadedRome.addGold(777 - reloadedRome.gold)
            val newSession = PlayerHost(runtime).bindPlayer("Rome")
            check(newSession.summary().treasury == 777)
            expectFailure(PlayerSessionFailure.SESSION_CLOSED) { expiredSession.summary() }
        }
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

    private fun setDistinctPlayerState(game: GameInfo) {
        game.getCivilization("Rome").apply {
            addGold(321 - gold)
            tech.techsToResearch.clear()
            tech.techsToResearch.add("Pottery")
        }
        game.getCivilization("Greece").apply {
            addGold(654 - gold)
            tech.techsToResearch.clear()
            tech.techsToResearch.add("Mining")
        }
    }

    private fun verifySummary(summary: PlayerSummary, civilization: Civilization, game: GameInfo) {
        check(summary.civilization == civilization.civName)
        check(summary.ruleset == game.gameParameters.baseRuleset)
        check(summary.turn == game.turns)
        check(summary.isYourTurn == (game.currentPlayer == civilization.civID))
        check(summary.treasury == civilization.gold)
        check(summary.cityCount == civilization.cities.size)
        check(summary.unitCount == civilization.units.getCivUnitsSize())
        check(summary.currentResearch == civilization.tech.currentTechnologyName())
        check(summary.freePolicies == civilization.policies.freePolicies)
        check(summary.freeTechnologies == civilization.tech.freeTechs)
        check(summary.freeGreatPeople == civilization.greatPeople.freeGreatPeople)
    }

    private class ScriptedClient(private val session: PlayerSession) {
        fun summary(): PlayerSummary = session.summary()

        fun serializedSummary(): String = PlayerSummaryJson.encode(summary())
    }

    private data class CapturedWorld(
        val playerOutput: List<String>,
        val rawGame: String,
    )

    private val expectedMessages = mapOf(
        PlayerSessionFailure.SESSION_CLOSED to "Player session is closed",
        PlayerSessionFailure.PLAYER_UNAVAILABLE to "Player is unavailable",
        PlayerSessionFailure.QUERY_FAILED to "Player query failed",
    )
}
