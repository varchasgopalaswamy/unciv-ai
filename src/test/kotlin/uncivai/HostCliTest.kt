package uncivai

import com.github.ajalt.clikt.core.PrintHelpMessage
import com.github.ajalt.clikt.core.UsageError
import com.github.ajalt.clikt.core.parse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

class HostCliTest {
    @Test fun `manager scenarios validate without models and retain identity on resume`() {
        val directory = Files.createTempDirectory("host-cli-managers-")
        try {
            for (variant in ManagerScenarioSetup.variants) {
                lateinit var parsed: CliOptions
                HostCli { parsed = it }.parse(listOf("--validate-setup", "--data-dir", directory.toString(), "--scenario", variant))
                assertEquals(variant, parsed.scenario)
                assertTrue(parsed.validateSetup)
                val checkpoint = directory.resolve("$variant.json")
                Files.writeString(checkpoint, "{}")
                HostCli { parsed = it }.parse(listOf("--resume-checkpoint", checkpoint.toString(), "--data-dir", directory.toString(), "--scenario", variant))
                assertEquals(variant, parsed.scenario)
            }
        } finally {
            deleteTreeWithoutFollowingLinks(directory)
        }
    }

    @Test
    fun `starting position seed is optional and independent of map seed`() {
        val directory = Files.createTempDirectory("host-cli-starting-seed-")
        try {
            val base = listOf("--validate-setup", "--data-dir", directory.toString())
            lateinit var parsed: CliOptions
            HostCli { parsed = it }.parse(base)
            assertEquals(null, parsed.vanilla.startingPositionSeed)
            HostCli { parsed = it }.parse(base + listOf("--seed", "42", "--starting-position-seed", "0"))
            assertEquals(42L, parsed.seed)
            assertEquals(0L, parsed.vanilla.startingPositionSeed)
            val save = Files.createFile(directory.resolve("game.json"))
            assertThrows(UsageError::class.java) {
                HostCli { error("Seed accepted on resume") }.parse(listOf("--resume-checkpoint", save.toString(), "--data-dir", directory.toString(), "--starting-position-seed", "1"))
            }
        } finally {
            deleteTreeWithoutFollowingLinks(directory)
        }
    }

    @Test
    fun `custom size options are exclusive and only apply to creation`() {
        val directory = Files.createTempDirectory("host-cli-map-")
        try {
            val checkpoint = directory.resolve("game.json")
            val base = listOf("--new-game", "--checkpoint", checkpoint.toString(), "--data-dir", directory.toString())
            lateinit var parsed: CliOptions
            HostCli { parsed = it }.parse(base + listOf("--map-size", "Large", "--agent-player", "Germany"))
            assertEquals("Large", parsed.mapSize)
            assertEquals(listOf("Germany"), parsed.agentPlayers)
            for (extra in listOf(listOf("--map-radius", "1"), listOf("--map-radius", "26", "--map-size", "Large"), listOf("--map-size", "Large", "--scenario", "trade-cycle"))) {
                assertThrows(UsageError::class.java) { HostCli { error("Invalid map reached startup") }.parse(base + extra) }
            }
            Files.createFile(checkpoint)
            assertThrows(UsageError::class.java) {
                HostCli { error("Resume map reached startup") }.parse(listOf("--resume-checkpoint", checkpoint.toString(), "--data-dir", directory.toString(), "--map-size", "Large"))
            }
        } finally {
            deleteTreeWithoutFollowingLinks(directory)
        }
    }

    @Test
    fun `city state count applies only to new ordinary games`() {
        val directory = Files.createTempDirectory("host-cli-city-states-")
        try {
            val checkpoint = directory.resolve("game.json")
            val base = listOf("--new-game", "--checkpoint", checkpoint.toString(), "--data-dir", directory.toString())
            lateinit var parsed: CliOptions
            HostCli { parsed = it }.parse(base)
            assertEquals(null, parsed.cityStates)
            HostCli { parsed = it }.parse(base + listOf("--city-states", "2"))
            assertEquals(2, parsed.cityStates)
            for (extra in listOf(listOf("--city-states", "-1"), listOf("--city-states", "42"), listOf("--city-states", "2", "--scenario", "war-paid"))) {
                assertThrows(UsageError::class.java) { HostCli { error("Invalid count reached startup") }.parse(base + extra) }
            }
            Files.createFile(checkpoint)
            assertThrows(UsageError::class.java) {
                HostCli { error("Resume count reached startup") }.parse(listOf("--resume-checkpoint", checkpoint.toString(), "--data-dir", directory.toString(), "--city-states", "2"))
            }
        } finally {
            deleteTreeWithoutFollowingLinks(directory)
        }
    }

    @Test
    fun `ordinary game does not require a scenario or its counteroffer limit`() {
        val directory = Files.createTempDirectory("host-cli-ordinary-")
        try {
            lateinit var parsed: CliOptions
            HostCli { parsed = it }.parse(listOf("--new-game", "--checkpoint", directory.resolve("game.json").toString(), "--data-dir", directory.toString(), "--trade-counteroffers", "5"))
            assertTrue(parsed.newGame)
            assertEquals(null, parsed.scenario)
            assertEquals(null, parsed.seed)
            assertEquals(5, parsed.tradeCounteroffers)
            for (variant in listOf("war-paid", "war-joint")) {
                HostCli { parsed = it }.parse(listOf("--new-game", "--checkpoint", directory.resolve("game.json").toString(), "--data-dir", directory.toString(), "--scenario", variant, "--trade-counteroffers", "5"))
                assertEquals(variant, parsed.scenario)
            }
            assertThrows(UsageError::class.java) {
                HostCli { error("Invalid scenario reached startup") }.parse(listOf("--new-game", "--checkpoint", directory.resolve("game.json").toString(), "--data-dir", directory.toString(), "--scenario", "war-paid", "--trade-counteroffers", "0"))
            }
            assertThrows(UsageError::class.java) {
                HostCli { error("Invalid scenario reached startup") }.parse(listOf("--new-game", "--checkpoint", directory.resolve("game.json").toString(), "--data-dir", directory.toString(), "--scenario", "trade-cycle", "--trade-counteroffers", "5"))
            }
        } finally {
            deleteTreeWithoutFollowingLinks(directory)
        }
    }

    @Test
    fun `parser supplies typed options without starting a runtime`() {
        val directory = Files.createTempDirectory("host-cli-")
        try {
            lateinit var parsed: CliOptions
            HostCli { parsed = it }.parse(listOf("--new-game", "--checkpoint", directory.resolve("game.json").toString(), "--data-dir", directory.resolve("data").toString(), "--scenario", "trade-cycle", "--seed", "-42", "--trade-counteroffers", "3", "--trade-timeout", "86400"))
            assertTrue(parsed.newGame)
            assertEquals(-42L, parsed.seed)
            assertEquals(3, parsed.tradeCounteroffers)
            assertEquals(86400, parsed.tradeTimeoutSeconds)
            assertEquals(8765, parsed.port)
            assertEquals("trade-cycle", parsed.scenario)
        } finally {
            deleteTreeWithoutFollowingLinks(directory)
        }
    }

    @Test
    fun `help and invalid argument combinations cannot invoke the host`() {
        val cli = { HostCli { error("Invalid input reached host startup") } }
        assertThrows(PrintHelpMessage::class.java) { cli().parse(listOf("--help")) }
        val directory = Files.createTempDirectory("host-cli-")
        try {
            val base = listOf("--new-game", "--checkpoint", directory.resolve("game.json").toString(), "--data-dir", directory.toString())
            for (extra in listOf(listOf("--port", "0"), listOf("--trade-counteroffers", "-1"), listOf("--trade-timeout", "0"), listOf("--trade-timeout", "86401"), listOf("--scenario", "missing"), listOf("--seed", "oops"), listOf("--unknown"))) {
                assertThrows(UsageError::class.java) { cli().parse(base + extra) }
            }
            for (removed in listOf("city-management", "great-people", "async-chat", "verbal-betrayal", "async-trade", "async-peace", "bilateral-trade")) {
                assertThrows(UsageError::class.java) { cli().parse(base + listOf("--scenario", removed)) }
            }
            assertThrows(UsageError::class.java) { cli().parse(listOf("--new-game", "--data-dir", directory.toString())) }
            val save = Files.createFile(directory.resolve("save.json"))
            assertThrows(UsageError::class.java) { cli().parse(base + listOf("--save", save.toString())) }
        } finally {
            deleteTreeWithoutFollowingLinks(directory)
        }
    }
}
