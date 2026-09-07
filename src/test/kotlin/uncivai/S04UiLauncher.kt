package uncivai

import com.badlogic.gdx.Gdx
import com.badlogic.gdx.files.FileHandle
import com.unciv.UncivGame
import com.unciv.json.json
import com.unciv.logic.files.SETTINGS_FILE_NAME
import com.unciv.logic.files.UncivFiles
import com.unciv.models.metadata.GameSettings
import com.unciv.ui.screens.mainmenuscreen.MainMenuScreen
import com.unciv.ui.screens.pickerscreens.GreatPersonPickerScreen
import com.unciv.utils.Concurrency
import java.lang.reflect.InvocationTargetException
import java.nio.charset.StandardCharsets
import java.nio.file.FileAlreadyExistsException
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardOpenOption
import java.util.concurrent.TimeUnit

/** Trusted development launcher that opens a supplied save through Unciv's normal desktop UI. */
object S04UiLauncher {
    private const val DESKTOP_LAUNCHER = "com.unciv.app.desktop.DesktopLauncher"
    private const val STARTUP_TIMEOUT_MINUTES = 5L
    private const val SMOKE_TEST_PROPERTY = "uncivai.uiSmokeTest"

    @JvmStatic
    fun main(args: Array<String>) {
        try {
            require(args.size == 2) {
                "Usage: S04UiLauncher <absolute save> <absolute data directory>"
            }
            val saveFile = validateSave(Path.of(args[0]))
            val dataDirectory = prepareDataDirectory(Path.of(args[1]))
            ensureSafeSettings(dataDirectory)
            armAutoLoad(saveFile, java.lang.Boolean.getBoolean(SMOKE_TEST_PROPERTY))

            println("S04 UI autoload armed for $saveFile")
            invokeDesktopLauncher(dataDirectory)
        } catch (failure: Throwable) {
            val cause = unwrapInvocationFailure(failure)
            System.err.println(
                "S04 UI autoload failed before launch: " +
                    (cause.message ?: cause::class.java.simpleName),
            )
            throw cause
        }
    }

    private fun validateSave(path: Path): Path {
        require(path.isAbsolute) { "Save path must be absolute: $path" }
        val realPath = path.toRealPath()
        require(Files.isRegularFile(realPath)) { "Save path is not a regular file: $path" }
        require(Files.isReadable(realPath)) { "Save file is not readable: $path" }
        return realPath
    }

    private fun prepareDataDirectory(path: Path): Path {
        require(path.isAbsolute) { "Data directory path must be absolute: $path" }
        val assetsDirectory = findAssetsDirectory()
        val prospectivePath = resolveThroughExistingAncestor(path)
        requireSeparateDataDirectory(prospectivePath, assetsDirectory)

        Files.createDirectories(path.normalize())
        val realPath = path.toRealPath()
        require(Files.isDirectory(realPath)) { "Data directory path is not a directory: $path" }
        require(Files.isWritable(realPath)) { "Data directory is not writable: $path" }
        requireSeparateDataDirectory(realPath, assetsDirectory)
        return realPath
    }

    private fun ensureSafeSettings(dataDirectory: Path) {
        val settingsFile = dataDirectory.resolve(SETTINGS_FILE_NAME)
        if (Files.exists(settingsFile)) {
            validateExistingSettings(dataDirectory, settingsFile)
            return
        }

        val settings = GameSettings().apply {
            isFreshlyCreated = false
            screenSize = GameSettings.ScreenSize.Medium
            screenMode = 0
            windowState = GameSettings.WindowState(width = 1100, height = 760)
            showTutorials = false
            enableEasterEggs = false
            soundEffectsVolume = 0f
            citySoundsVolume = 0f
            musicVolume = 0f
            voicesVolume = 0f
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
        }
        val encoded = json().toJson(settings)
        try {
            Files.writeString(
                settingsFile,
                encoded,
                StandardCharsets.UTF_8,
                StandardOpenOption.CREATE_NEW,
                StandardOpenOption.WRITE,
            )
        } catch (_: FileAlreadyExistsException) {
            validateExistingSettings(dataDirectory, settingsFile)
        }
    }

    private fun validateExistingSettings(dataDirectory: Path, settingsFile: Path) {
        require(Files.isRegularFile(settingsFile) && Files.isReadable(settingsFile)) {
            "Existing settings path is not a readable regular file: $settingsFile"
        }
        val settings = UncivFiles.getSettingsForPlatformLaunchers(dataDirectory.toString())
        require(!settings.isFreshlyCreated) {
            "Existing settings are invalid or marked as newly created; refusing to overwrite them"
        }
    }

    private fun armAutoLoad(saveFile: Path, smokeTest: Boolean) {
        val deadline = System.nanoTime() + TimeUnit.MINUTES.toNanos(STARTUP_TIMEOUT_MINUTES)
        Thread({
            while (Gdx.app == null) {
                if (System.nanoTime() >= deadline) {
                    System.err.println("S04 UI autoload failed: desktop application did not initialize")
                    return@Thread
                }
                Thread.sleep(25)
            }
            Gdx.app.postRunnable(MainMenuAutoLoader(saveFile, deadline, smokeTest))
        }, "S04 UI autoload waiter").apply {
            isDaemon = true
            start()
        }
    }

    private class MainMenuAutoLoader(
        private val saveFile: Path,
        private val deadline: Long,
        private val smokeTest: Boolean,
    ) : Runnable {
        override fun run() {
            val game = runCatching { UncivGame.Current }.getOrNull()
            if (game?.getScreen() !is MainMenuScreen) {
                if (System.nanoTime() >= deadline) {
                    System.err.println("S04 UI autoload failed: main menu did not become ready")
                    if (smokeTest) Gdx.app.exit()
                    return
                }
                Gdx.app.postRunnable(this)
                return
            }

            Concurrency.run("S04 UI autoload") {
                try {
                    val gameInfo = game.files.loadGameFromFile(FileHandle(saveFile.toFile()))
                    game.loadGame(gameInfo, callFromLoadScreen = true)
                    println("S04 UI autoload succeeded: $saveFile")
                    if (smokeTest) {
                        val pickerDeadline =
                            System.nanoTime() + TimeUnit.MINUTES.toNanos(STARTUP_TIMEOUT_MINUTES)
                        Gdx.app.postRunnable(GreatPersonPickerWaiter(saveFile.fileName.toString(), pickerDeadline))
                    }
                } catch (failure: Throwable) {
                    System.err.println(
                        "S04 UI autoload failed: " +
                            (failure.message ?: failure::class.java.simpleName),
                    )
                    if (smokeTest) Gdx.app.postRunnable { Gdx.app.exit() }
                }
            }
        }
    }

    private class GreatPersonPickerWaiter(
        private val saveFileName: String,
        private val deadline: Long,
    ) : Runnable {
        override fun run() {
            val game = runCatching { UncivGame.Current }.getOrNull()
            if (game?.getScreen() is GreatPersonPickerScreen) {
                println("S04-UI-READY:$saveFileName")
                System.out.flush()
                Gdx.app.postRunnable { Gdx.app.exit() }
                return
            }
            if (System.nanoTime() >= deadline) {
                System.err.println("S04 UI autoload failed: Great Person picker did not appear")
                Gdx.app.exit()
                return
            }
            Gdx.app.postRunnable(this)
        }
    }

    private fun invokeDesktopLauncher(dataDirectory: Path) {
        val launcher = Class.forName(DESKTOP_LAUNCHER)
        val main = launcher.getMethod("main", Array<String>::class.java)
        main.invoke(null, arrayOf("--data-dir=$dataDirectory") as Any)
    }

    private fun findAssetsDirectory(): Path {
        val configured = System.getProperty("uncivai.assetsDir")
            ?.takeIf { it.isNotBlank() }
            ?.let { Path.of(it) }
        val path = configured ?: Path.of("").toAbsolutePath()
        val realPath = path.toRealPath()
        require(Files.isDirectory(realPath.resolve("jsons"))) {
            "Unciv assets directory is missing its jsons directory: $realPath"
        }
        return realPath
    }

    private fun requireSeparateDataDirectory(dataDirectory: Path, assetsDirectory: Path) {
        require(
            !dataDirectory.startsWith(assetsDirectory) &&
                !assetsDirectory.startsWith(dataDirectory),
        ) {
            "Data directory must be separate from the Unciv assets directory"
        }
    }

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

    private fun unwrapInvocationFailure(failure: Throwable): Throwable = if (failure is InvocationTargetException && failure.cause != null) {
        failure.cause!!
    } else {
        failure
    }
}
