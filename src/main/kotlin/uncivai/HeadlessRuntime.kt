package uncivai

import com.badlogic.gdx.Files.FileType
import com.badlogic.gdx.Gdx
import com.badlogic.gdx.files.FileHandle
import com.unciv.UncivGame
import com.unciv.logic.GameInfo
import com.unciv.logic.files.UncivFiles
import com.unciv.models.metadata.BaseRuleset
import com.unciv.models.metadata.GameSettings
import com.unciv.models.ruleset.RulesetCache
import com.unciv.utils.Concurrency
import com.unciv.utils.DebugUtils
import java.io.File
import java.nio.file.Files
import java.nio.file.Path

/**
 * Owns one display-free Unciv game in this JVM.
 *
 * This is a trusted-host API: [game] is the raw engine state and must not be exposed as a
 * player-facing information boundary.
 */
class HeadlessRuntime private constructor(
    private val uncivGame: UncivGame,
    private val runtimeFiles: RuntimeFiles,
    private val previousGdxFiles: com.badlogic.gdx.Files?,
    val game: GameInfo,
) : AutoCloseable {
    private var closed = false
    internal val executor = GameExecutor(game, lifecycleLock) {
        !closed && activeRuntime === this && UncivGame.isCurrentInitialized() &&
            UncivGame.Current === uncivGame && uncivGame.gameInfo === game
    }
    internal val epoch get() = executor.epoch
    internal val mutationFailure get() = executor.mutationFailure

    /** Serializes player reads and commands with runtime shutdown. */
    internal fun <T> accessGame(block: (GameInfo) -> T): T = executor.accessGame(block)

    /** Stops further player operations; the caller must not assume the mutation was rolled back. */
    internal fun haltAfterMutation(failure: Throwable): Nothing = executor.haltAfterMutation(failure)

    override fun close() {
        synchronized(lifecycleLock) {
            if (closed) return
            executor.close()
            closed = true

            if (UncivGame.isCurrentInitialized() && UncivGame.Current === uncivGame) {
                uncivGame.gameInfo = null
            }
            RulesetCache.clear()
            Concurrency.stopThreadPools()
            if (Gdx.files === runtimeFiles) Gdx.files = previousGdxFiles
            if (activeRuntime === this) activeRuntime = null
        }
    }

    companion object {
        private const val ASSETS_DIRECTORY_PROPERTY = "uncivai.assetsDir"
        private val lifecycleLock = Any()
        private var activeRuntime: HeadlessRuntime? = null

        /** Restores both the engine save and command receipts from one trusted checkpoint. */
        fun loadCheckpoint(checkpointPath: Path, dataDirectory: Path): HeadlessRuntime = loadCheckpoint(CheckpointStore(checkpointPath), dataDirectory)

        internal fun loadCheckpoint(store: CheckpointStore, dataDirectory: Path): HeadlessRuntime {
            val temporary = Files.createTempFile("unciv-checkpoint-load-", ".json")
            try {
                val checkpoint = store.read()
                Files.writeString(temporary, checkpoint.gameJson)
                val runtime = load(temporary, dataDirectory)
                try {
                    runtime.executor.restore(checkpoint, store)
                    return runtime
                } catch (failure: Throwable) {
                    runtime.close()
                    throw failure
                }
            } catch (failure: Throwable) {
                store.close()
                throw failure
            } finally {
                Files.deleteIfExists(temporary)
            }
        }

        /**
         * Loads [saveFile] without creating a GDX application, graphics context, or world screen.
         * Both paths must be absolute. [dataDirectory] is created when needed and must be writable.
         */
        fun load(saveFile: Path, dataDirectory: Path): HeadlessRuntime = synchronized(lifecycleLock) {
            check(activeRuntime == null) {
                "A HeadlessRuntime is already active in this JVM"
            }
            check(Gdx.app == null && Gdx.graphics == null && Gdx.gl == null) {
                "HeadlessRuntime requires a JVM without a GDX application or graphics context"
            }

            val paths = resolvePaths(saveFile, dataDirectory)
            val previousFiles = Gdx.files
            val runtimeFiles = RuntimeFiles(paths.assetsDirectory, paths.dataDirectory)
            val uncivGame = UncivGame(isConsoleMode = true)

            try {
                Gdx.files = runtimeFiles
                UncivGame.Current = uncivGame
                uncivGame.customDataDirectory = paths.dataDirectory.toString()
                uncivGame.settings = controlledSettings()
                uncivGame.files = UncivFiles(runtimeFiles, paths.dataDirectory.toString())

                // Establish the no-simulation and no-automation configuration before
                // loadGameFromFile restores the GameInfo transients.
                DebugUtils.SIMULATE_UNTIL_TURN = 0
                DebugUtils.VISIBLE_MAP = false

                val saveHandle = FileHandle(paths.saveFile.toFile())
                validateBundledSave(uncivGame.files, saveHandle)
                loadBundledRulesets()

                val gameInfo = uncivGame.files.loadGameFromFile(saveHandle)
                gameInfo.simulateUntilWin = false
                gameInfo.simulateMaxTurns = 0
                check(!gameInfo.isSimulation()) { "Loaded game unexpectedly entered simulation mode" }
                check(gameInfo.gameParameters.mods.isEmpty()) {
                    "Loaded game uses mods; only bundled rulesets are supported"
                }

                uncivGame.gameInfo = gameInfo
                HeadlessRuntime(uncivGame, runtimeFiles, previousFiles, gameInfo).also {
                    activeRuntime = it
                }
            } catch (failure: Throwable) {
                uncivGame.gameInfo = null
                RulesetCache.clear()
                Concurrency.stopThreadPools()
                if (Gdx.files === runtimeFiles) Gdx.files = previousFiles
                throw failure
            }
        }

        private fun controlledSettings() = GameSettings().apply {
            showTutorials = false
            autoAssignCityProduction = false
            autoBuildingRoads = false
            automatedWorkersReplaceImprovements = false
            stopAutomatedWorkersRemoveVegetation = true
            automatedUnitsMoveOnTurnStart = false
            automatedUnitsCanUpgrade = false
            automatedUnitsChoosePromotions = false
            citiesAutoBombardAtEndOfTurn = false
            turnsBetweenAutosaves = Int.MAX_VALUE
            autoPlay.autoPlayUntilEnd = false
            autoPlay.fullAutoPlayAI = false
            autoPlay.autoPlayMilitary = false
            autoPlay.autoPlayCivilian = false
            autoPlay.autoPlayEconomy = false
            autoPlay.autoPlayTechnology = false
            autoPlay.autoPlayPolicies = false
            autoPlay.autoPlayReligion = false
            autoPlay.autoPlayDiplomacy = false
            updateLocaleFromLanguage()
        }

        private fun validateBundledSave(files: UncivFiles, saveHandle: FileHandle) {
            val preview = files.loadGamePreviewFromFile(saveHandle)
            require(preview.gameParameters.mods.isEmpty()) {
                "The save uses mods; only bundled Vanilla and Gods & Kings saves are supported"
            }
            require(preview.gameParameters.baseRuleset in bundledRulesetNames) {
                "Unsupported base ruleset '${preview.gameParameters.baseRuleset}'; " +
                    "only bundled Vanilla and Gods & Kings saves are supported"
            }
        }

        private fun loadBundledRulesets() {
            val errors = RulesetCache.loadRulesets(consoleMode = false, noMods = true)
            check(errors.isEmpty()) {
                "Bundled rulesets failed to load: ${errors.joinToString("; ")}"
            }
            check(RulesetCache.keys == bundledRulesetNames) {
                "Expected bundled rulesets ${bundledRulesetNames.joinToString()}, " +
                    "but loaded ${RulesetCache.keys.joinToString()}"
            }
        }

        private val bundledRulesetNames = BaseRuleset.entries.mapTo(linkedSetOf()) { it.fullName }

        private fun resolvePaths(saveFile: Path, dataDirectory: Path): RuntimePaths {
            require(saveFile.isAbsolute) { "Save path must be absolute: $saveFile" }
            require(dataDirectory.isAbsolute) { "Data directory path must be absolute: $dataDirectory" }

            val realSave = saveFile.toRealPath()
            require(Files.isRegularFile(realSave)) { "Save path is not a regular file: $saveFile" }
            require(Files.isReadable(realSave)) { "Save file is not readable: $saveFile" }

            val assetsProperty = System.getProperty(ASSETS_DIRECTORY_PROPERTY)
                ?.takeIf { it.isNotBlank() }
                ?: throw IllegalStateException(
                    "Missing -D$ASSETS_DIRECTORY_PROPERTY=<absolute Unciv assets directory>",
                )
            val configuredAssets = Path.of(assetsProperty)
            require(configuredAssets.isAbsolute) {
                "$ASSETS_DIRECTORY_PROPERTY must be an absolute path: $configuredAssets"
            }
            val realAssetsDirectory = configuredAssets.toRealPath()
            require(Files.isDirectory(realAssetsDirectory)) {
                "Unciv assets path is not a directory: $configuredAssets"
            }
            for (baseRuleset in BaseRuleset.entries) {
                val rulesetDirectory = realAssetsDirectory.resolve("jsons").resolve(baseRuleset.fullName)
                require(Files.isDirectory(rulesetDirectory)) {
                    "Bundled ruleset assets are missing: $rulesetDirectory"
                }
            }

            val prospectiveDataDirectory = resolveThroughExistingAncestor(dataDirectory)
            requireSeparateDataDirectory(prospectiveDataDirectory, realAssetsDirectory)

            Files.createDirectories(dataDirectory.normalize())
            val realDataDirectory = dataDirectory.toRealPath()
            require(Files.isDirectory(realDataDirectory)) {
                "Data directory path is not a directory: $dataDirectory"
            }
            require(Files.isWritable(realDataDirectory)) {
                "Data directory is not writable: $dataDirectory"
            }
            requireSeparateDataDirectory(realDataDirectory, realAssetsDirectory)

            require(!realSave.startsWith(realAssetsDirectory)) {
                "Save file must be outside the Unciv assets directory"
            }

            return RuntimePaths(realSave, realDataDirectory, realAssetsDirectory)
        }

        private fun requireSeparateDataDirectory(dataDirectory: Path, assetsDirectory: Path) {
            require(
                !dataDirectory.startsWith(assetsDirectory) &&
                    !assetsDirectory.startsWith(dataDirectory),
            ) {
                "Data directory must be separate from the Unciv assets directory"
            }
        }

        /** Resolves symlinks in the nearest existing ancestor without creating [path]. */
        private fun resolveThroughExistingAncestor(path: Path): Path {
            val missingSegments = ArrayDeque<Path>()
            var existingPath = path.normalize()
            while (!Files.exists(existingPath)) {
                val segment = existingPath.fileName
                    ?: throw IllegalArgumentException("Path has no existing ancestor: $path")
                missingSegments.addFirst(segment)
                existingPath = existingPath.parent
                    ?: throw IllegalArgumentException("Path has no existing ancestor: $path")
            }

            var resolved = existingPath.toRealPath()
            for (segment in missingSegments) resolved = resolved.resolve(segment)
            return resolved.normalize()
        }
    }
}

internal class RuntimeClosedException : IllegalStateException("Runtime is closed")

internal class RuntimeHaltedException : IllegalStateException("Runtime is halted")

private data class RuntimePaths(
    val saveFile: Path,
    val dataDirectory: Path,
    val assetsDirectory: Path,
)

/** Minimal core-only GDX file service with explicit asset and writable-data roots. */
private class RuntimeFiles(
    private val assetsDirectory: Path,
    private val dataDirectory: Path,
) : com.badlogic.gdx.Files {
    override fun getFileHandle(path: String, type: FileType): FileHandle = when (type) {
        FileType.Classpath, FileType.Internal ->
            rootedHandle(assetsDirectory, path, "asset", FileType.Internal)

        FileType.Local, FileType.External ->
            rootedHandle(dataDirectory, path, "data", FileType.Absolute)

        FileType.Absolute -> FileHandle(Path.of(path).toFile())
    }

    override fun classpath(path: String) = getFileHandle(path, FileType.Classpath)
    override fun internal(path: String) = getFileHandle(path, FileType.Internal)
    override fun external(path: String) = getFileHandle(path, FileType.External)
    override fun absolute(path: String) = getFileHandle(path, FileType.Absolute)
    override fun local(path: String) = getFileHandle(path, FileType.Local)

    override fun getExternalStoragePath() = dataDirectory.toString() + File.separator
    override fun isExternalStorageAvailable() = false
    override fun getLocalStoragePath() = dataDirectory.toString() + File.separator
    override fun isLocalStorageAvailable() = true

    private fun rootedHandle(
        root: Path,
        path: String,
        kind: String,
        handleType: FileType,
    ): FileHandle {
        val resolved = root.resolve(path).normalize()
        require(resolved.startsWith(root)) { "$kind path escapes its configured root: $path" }
        return TypedFileHandle(resolved.toFile(), handleType)
    }
}

private class TypedFileHandle(file: File, type: FileType) : FileHandle(file, type)
