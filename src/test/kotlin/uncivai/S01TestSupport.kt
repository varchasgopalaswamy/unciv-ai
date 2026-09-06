package uncivai

import com.badlogic.gdx.Files as GdxFiles
import com.badlogic.gdx.files.FileHandle
import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.PlayerType
import java.nio.charset.StandardCharsets
import java.nio.file.FileVisitResult
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.SimpleFileVisitor
import java.nio.file.attribute.BasicFileAttributes
import java.security.MessageDigest

internal fun deleteTreeWithoutFollowingLinks(root: Path) {
    if (!Files.exists(root)) return
    Files.walkFileTree(root, object : SimpleFileVisitor<Path>() {
        override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
            Files.delete(file)
            return FileVisitResult.CONTINUE
        }

        override fun postVisitDirectory(directory: Path, failure: java.io.IOException?): FileVisitResult {
            if (failure != null) throw failure
            Files.delete(directory)
            return FileVisitResult.CONTINUE
        }
    })
}

internal class S01Files(
    private val assetsDirectory: Path,
    private val dataDirectory: Path,
) : GdxFiles {
    private fun handle(path: Path) = FileHandle(path.toFile())

    override fun getFileHandle(path: String, type: GdxFiles.FileType): FileHandle = when (type) {
        GdxFiles.FileType.Classpath -> classpath(path)
        GdxFiles.FileType.Internal -> internal(path)
        GdxFiles.FileType.External -> external(path)
        GdxFiles.FileType.Absolute -> absolute(path)
        GdxFiles.FileType.Local -> local(path)
    }

    override fun classpath(path: String) = FileHandle(path)
    override fun internal(path: String) = handle(assetsDirectory.resolve(path))
    override fun external(path: String) = handle(dataDirectory.resolve(path))
    override fun absolute(path: String) = handle(Path.of(path))
    override fun local(path: String) = handle(dataDirectory.resolve(path))
    override fun getExternalStoragePath() = dataDirectory.toString()
    override fun isExternalStorageAvailable() = false
    override fun getLocalStoragePath() = dataDirectory.toString()
    override fun isLocalStorageAvailable() = true
}

internal object S01ScenarioState {
    private val pendingChoices = mapOf(
        "Rome" to Triple(2, 1, 1),
        "Greece" to Triple(1, 2, 2),
    )

    fun addPendingChoices(game: GameInfo) {
        for ((civName, choices) in pendingChoices) {
            val civ = game.getCivilization(civName)
            civ.policies.freePolicies = choices.first
            civ.policies.shouldOpenPolicyPicker = true
            civ.tech.freeTechs = choices.second
            civ.greatPeople.freeGreatPeople = choices.third
        }
    }

    fun verify(game: GameInfo) {
        check(game.turns == 17) { "Scenario turn changed: ${game.turns}" }
        check(game.currentPlayer == "Rome") { "Current player changed: ${game.currentPlayer}" }
        check(!game.gameParameters.isOnlineMultiplayer) { "Scenario must be an ordinary hotseat game" }
        check(game.gameParameters.mods.isEmpty()) { "Scenario unexpectedly uses mods" }

        val humans = game.civilizations.filter {
            it.playerType == PlayerType.Human && it.isMajorCiv()
        }
        check(humans.map { it.civID }.toSet() == pendingChoices.keys) {
            "Expected Human Rome and Greece, got ${humans.map { it.civID }}"
        }

        for (civ in humans) {
            check(civ.cities.size == 1) { "${civ.civID} must have one city" }
            val city = civ.cities.single()
            check(!city.isPuppet) { "${civ.civID}'s city must be an ordinary Human city" }
            check(city.population.population > 0) { "${city.name} must be populated" }
            check(city.cityConstructions.constructionQueue.isEmpty()) {
                "${city.name}'s open production choice was filled during load"
            }

            val units = civ.units.getCivUnits().toList()
            check(units.isNotEmpty()) { "${civ.civID} must retain a playable unit" }
            check(units.any { it.isMilitary() }) { "${civ.civID} must retain a military unit" }

            val choices = pendingChoices.getValue(civ.civID)
            check(civ.policies.freePolicies == choices.first) {
                "${civ.civID}'s free policy choices changed"
            }
            check(civ.policies.shouldOpenPolicyPicker) {
                "${civ.civID}'s policy picker flag changed"
            }
            check(civ.tech.freeTechs == choices.second) {
                "${civ.civID}'s free technology choices changed"
            }
            check(civ.greatPeople.freeGreatPeople == choices.third) {
                "${civ.civID}'s free great-person choices changed"
            }
        }
    }

    fun digest(game: GameInfo): String {
        val canonical = buildString {
            append("ruleset=").append(game.gameParameters.baseRuleset).append('\n')
            append("turn=").append(game.turns).append('\n')
            append("current=").append(game.currentPlayer).append('\n')
            append("turnStart=").append(game.currentTurnStartTime).append('\n')
            for (civ in game.civilizations.sortedBy { it.civID }) {
                append("civ=").append(civ.civID)
                    .append('|').append(civ.playerType)
                    .append('|').append(civ.gold)
                    .append('|').append(civ.policies.freePolicies)
                    .append('|').append(civ.policies.shouldOpenPolicyPicker)
                    .append('|').append(civ.tech.freeTechs)
                    .append('|').append(civ.tech.techsToResearch.joinToString(","))
                    .append('|').append(civ.greatPeople.freeGreatPeople)
                    .append('\n')
                for (city in civ.cities.sortedBy { it.id }) {
                    append("city=").append(city.id)
                        .append('|').append(city.name)
                        .append('|').append(city.location)
                        .append('|').append(city.population.population)
                        .append('|').append(city.isPuppet)
                        .append('|').append(city.cityConstructions.currentConstructionIsUserSet)
                        .append('|').append(city.cityConstructions.constructionQueue.joinToString(","))
                        .append('\n')
                }
                for (unit in civ.units.getCivUnits().sortedBy { it.id }) {
                    append("unit=").append(unit.id)
                        .append('|').append(unit.name)
                        .append('|').append(unit.getTile().position)
                        .append('|').append(unit.health)
                        .append('|').append(unit.currentMovement)
                        .append('|').append(unit.action)
                        .append('|').append(unit.automated)
                        .append('|').append(unit.due)
                        .append('\n')
                }
                for (alert in civ.popupAlerts) {
                    append("alert=").append(alert.type).append('|').append(alert.value).append('\n')
                }
            }
        }
        return MessageDigest.getInstance("SHA-256")
            .digest(canonical.toByteArray(StandardCharsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
    }
}
