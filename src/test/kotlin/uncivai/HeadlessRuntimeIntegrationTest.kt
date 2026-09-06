package uncivai

import com.badlogic.gdx.utils.JsonReader
import com.badlogic.gdx.utils.JsonValue
import com.badlogic.gdx.utils.JsonWriter
import com.unciv.logic.files.FileConversions
import org.junit.AfterClass
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import kotlin.io.path.name

class HeadlessRuntimeIntegrationTest {
    @Test
    fun `Vanilla and Gods and Kings saves load and close without gameplay or graphics`() {
        assertEquals(assetFingerprintBeforeGeneration, fingerprintTree(assetsDirectory))

        for ((fileName, expectedDigest) in generatedDigests) {
            val save = scenarioDirectory.resolve(fileName)
            val dataDirectory = Files.createDirectories(testRoot.resolve("runtime-${save.name}"))
            writePoisonousSettings(dataDirectory)

            val result = runChild(
                mainClass = "uncivai.S01RuntimeProbe",
                arguments = listOf(save.toString(), dataDirectory.toString()),
                timeoutSeconds = 30,
            )

            assertEquals(result.output, 0, result.exitCode)
            assertTrue(
                result.output,
                result.output.lineSequence().any { it == "S01-LOADED:$fileName:$expectedDigest" },
            )
        }

        assertEquals(assetFingerprintBeforeGeneration, fingerprintTree(assetsDirectory))
    }

    @Test
    fun `CLI loads plain and compressed persisted scenarios and exits within its timeout`() {
        val compressedSave = testRoot.resolve("gods-and-kings-compressed.json")
        Files.writeString(
            compressedSave,
            FileConversions.zip(Files.readString(scenarioDirectory.resolve("gods-and-kings.json"))),
            StandardCharsets.UTF_8,
        )

        val saves = listOf(scenarioDirectory.resolve("vanilla.json"), compressedSave)
        for (save in saves) {
            val dataDirectory = Files.createDirectories(testRoot.resolve("cli-valid-${save.name}"))
            writePoisonousSettings(dataDirectory)
            val saveBefore = Files.readAllBytes(save)

            val result = runChild(
                mainClass = "uncivai.MainKt",
                arguments = listOf("--save", save.toString(), "--data-dir", dataDirectory.toString()),
                timeoutSeconds = 30,
            )

            assertEquals(result.output, 0, result.exitCode)
            assertTrue(
                "CLI must print the loaded turn and current player:\n${result.output}",
                result.output.lineSequence().any {
                    it.startsWith("Loaded game ") &&
                        it.contains("turn 17") &&
                        it.contains("current player Rome")
                },
            )
            assertFalse(
                "CLI must not print an uncaught exception:\n${result.output}",
                result.output.contains("Exception in thread"),
            )
            assertTrue("CLI rewrote $save", saveBefore.contentEquals(Files.readAllBytes(save)))
        }
        assertEquals(assetFingerprintBeforeGeneration, fingerprintTree(assetsDirectory))
    }

    @Test
    fun `missing corrupt and unsupported-mod saves fail cleanly`() {
        val corruptSave = testRoot.resolve("corrupt.json")
        Files.writeString(corruptSave, "this is not a saved game", StandardCharsets.UTF_8)

        val unsupportedModSave = testRoot.resolve("unsupported-mod.json")
        val validJson = Files.readString(scenarioDirectory.resolve("vanilla.json"))
        val jsonRoot = JsonReader().parse(validJson)
        val gameParameters = requireNotNull(jsonRoot.get("gameParameters"))
        gameParameters.remove("mods")
        val unsupportedMods = JsonValue(JsonValue.ValueType.array)
        unsupportedMods.addChild(JsonValue("S01 Missing Mod"))
        gameParameters.addChild("mods", unsupportedMods)
        Files.writeString(
            unsupportedModSave,
            jsonRoot.toJson(JsonWriter.OutputType.json),
            StandardCharsets.UTF_8,
        )

        val cases = listOf(
            "missing" to testRoot.resolve("does-not-exist.json"),
            "corrupt" to corruptSave,
            "unsupported mod" to unsupportedModSave,
        )
        for ((label, save) in cases) {
            val dataDirectory = Files.createDirectories(testRoot.resolve("cli-failure-${label.replace(' ', '-')}"))
            writePoisonousSettings(dataDirectory)
            val result = runChild(
                mainClass = "uncivai.MainKt",
                arguments = listOf("--save", save.toString(), "--data-dir", dataDirectory.toString()),
                timeoutSeconds = 30,
            )

            assertTrue("$label save unexpectedly succeeded:\n${result.output}", result.exitCode != 0)
            assertFalse("$label save leaked an uncaught exception:\n${result.output}", result.output.contains("Exception in thread"))
        }
        assertEquals(assetFingerprintBeforeGeneration, fingerprintTree(assetsDirectory))
    }

    @Test
    fun `failed full load cleans up and a later runtime can load`() {
        val malformedSave = testRoot.resolve("valid-preview-malformed-game.json")
        Files.writeString(
            malformedSave,
            """{"gameParameters":{"baseRuleset":"Civ V - Vanilla","mods":[]}}""",
            StandardCharsets.UTF_8,
        )
        val dataDirectory = Files.createDirectories(testRoot.resolve("failure-recovery"))
        val result = runChild(
            mainClass = "uncivai.S01FailureRecoveryProbe",
            arguments = listOf(
                malformedSave.toString(),
                scenarioDirectory.resolve("vanilla.json").toString(),
                dataDirectory.toString(),
            ),
            timeoutSeconds = 30,
        )

        assertEquals(result.output, 0, result.exitCode)
        assertTrue(result.output, result.output.lineSequence().any { it == "S01-RECOVERED" })
        assertEquals(assetFingerprintBeforeGeneration, fingerprintTree(assetsDirectory))
    }

    @Test
    fun `data directory beneath real or symlinked assets is rejected without creation`() {
        val directChild = assetsDirectory.resolve("s01-must-not-be-created")
        assertFalse("Test precondition failed: $directChild exists", Files.exists(directChild))

        val assetAlias = testRoot.resolve("asset-alias")
        Files.createSymbolicLink(assetAlias, assetsDirectory)
        val aliasChild = assetAlias.resolve("s01-must-not-be-created-through-alias")
        val save = scenarioDirectory.resolve("vanilla.json")

        for (dataDirectory in listOf(directChild, aliasChild)) {
            val result = runChild(
                mainClass = "uncivai.MainKt",
                arguments = listOf("--save", save.toString(), "--data-dir", dataDirectory.toString()),
                timeoutSeconds = 15,
            )
            assertTrue(result.output, result.exitCode != 0)
            assertFalse("Rejected data directory was created: $dataDirectory", Files.exists(dataDirectory))
        }
        assertEquals(assetFingerprintBeforeGeneration, fingerprintTree(assetsDirectory))
    }

    private data class ChildResult(val exitCode: Int, val output: String)

    companion object {
        private lateinit var testRoot: Path
        private lateinit var scenarioDirectory: Path
        private lateinit var assetsDirectory: Path
        private lateinit var testClasspath: String
        private lateinit var assetFingerprintBeforeGeneration: String
        private lateinit var generatedDigests: Map<String, String>

        @BeforeClass
        @JvmStatic
        fun generateScenarios() {
            assetsDirectory = Path.of(requireNotNull(System.getProperty("uncivai.assetsDir")) {
                "Gradle must provide uncivai.assetsDir"
            }).toAbsolutePath().normalize()
            testClasspath = requireNotNull(System.getProperty("uncivai.testClasspath")) {
                "Gradle must provide uncivai.testClasspath"
            }
            testRoot = Files.createTempDirectory("uncivai-s01-").toAbsolutePath()
            scenarioDirectory = Files.createDirectories(testRoot.resolve("scenarios"))
            writePoisonousSettings(testRoot)
            assetFingerprintBeforeGeneration = fingerprintTree(assetsDirectory)

            val result = runChild(
                mainClass = "uncivai.S01ScenarioGenerator",
                arguments = listOf(scenarioDirectory.toString()),
                timeoutSeconds = 60,
            )
            assertEquals(result.output, 0, result.exitCode)
            generatedDigests = result.output.lineSequence()
                .filter { it.startsWith("S01-GENERATED:") }
                .associate { line ->
                    val fields = line.split(':', limit = 3)
                    fields[1] to fields[2]
                }
            assertEquals(result.output, setOf("vanilla.json", "gods-and-kings.json"), generatedDigests.keys)
            for (fileName in generatedDigests.keys) {
                val save = scenarioDirectory.resolve(fileName)
                assertTrue("Missing generated scenario $save", Files.isRegularFile(save))
                assertTrue("Scenario $save must be uncompressed JSON", Files.readString(save).startsWith("{"))
            }
            assertEquals(assetFingerprintBeforeGeneration, fingerprintTree(assetsDirectory))
        }

        @AfterClass
        @JvmStatic
        fun removeScenarios() {
            if (::assetsDirectory.isInitialized && ::assetFingerprintBeforeGeneration.isInitialized) {
                assertEquals(assetFingerprintBeforeGeneration, fingerprintTree(assetsDirectory))
            }
            if (::testRoot.isInitialized) deleteTreeWithoutFollowingLinks(testRoot)
        }

        private fun writePoisonousSettings(dataDirectory: Path) {
            Files.writeString(
                dataDirectory.resolve("GameSettings.json"),
                """{
                    "autoAssignCityProduction": true,
                    "automatedUnitsMoveOnTurnStart": true,
                    "automatedUnitsCanUpgrade": true,
                    "automatedUnitsChoosePromotions": true,
                    "citiesAutoBombardAtEndOfTurn": true,
                    "turnsBetweenAutosaves": 1
                }""".trimIndent(),
                StandardCharsets.UTF_8,
            )
        }

        private fun runChild(
            mainClass: String,
            arguments: List<String>,
            timeoutSeconds: Long,
        ): ChildResult {
            val outputFile = Files.createTempFile(testRoot, "child-", ".log")
            val java = Path.of(System.getProperty("java.home"), "bin", "java").toString()
            val command = buildList {
                add(java)
                add("-Xmx1g")
                add("-Djava.awt.headless=true")
                add("-Duncivai.assetsDir=$assetsDirectory")
                add("-cp")
                add(testClasspath)
                add(mainClass)
                addAll(arguments)
            }
            val process = ProcessBuilder(command)
                .directory(testRoot.toFile())
                .redirectErrorStream(true)
                .redirectOutput(outputFile.toFile())
                .apply {
                    environment().remove("DISPLAY")
                    environment().remove("WAYLAND_DISPLAY")
                }
                .start()

            if (!process.waitFor(timeoutSeconds, TimeUnit.SECONDS)) {
                process.destroyForcibly()
                process.waitFor(5, TimeUnit.SECONDS)
                val output = Files.readString(outputFile)
                throw AssertionError("$mainClass did not exit within ${timeoutSeconds}s:\n$output")
            }
            return ChildResult(process.exitValue(), Files.readString(outputFile))
        }

        private fun fingerprintTree(root: Path): String {
            val digest = MessageDigest.getInstance("SHA-256")
            Files.walk(root).use { paths ->
                paths.filter { Files.isRegularFile(it) }
                    .sorted()
                    .forEach { path ->
                        digest.update(root.relativize(path).toString().toByteArray(StandardCharsets.UTF_8))
                        digest.update(0.toByte())
                        Files.newInputStream(path).use { input ->
                            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                            while (true) {
                                val count = input.read(buffer)
                                if (count < 0) break
                                digest.update(buffer, 0, count)
                            }
                        }
                    }
            }
            return digest.digest().joinToString("") { "%02x".format(it) }
        }
    }
}
