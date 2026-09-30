package uncivai.player

import com.unciv.logic.GameInfo
import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerOperations
import com.unciv.models.translations.tr
import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class CivilianCaptureOutcome(val recipient: String, val unitType: String?)

@Serializable
data class CaptureDiplomaticEffect(val civilizationId: String, val stat: String, val operation: String, val value: Float, val before: Float?, val after: Float)

@OptIn(ExperimentalSerializationApi::class)
@Serializable
data class CaptureOption(
    val name: String,
    val label: String,
    val paragraphs: List<String>,
    val available: Boolean,
    val unavailableReasons: List<String>,
    @EncodeDefault(EncodeDefault.Mode.NEVER) val civilianOutcome: CivilianCaptureOutcome? = null,
    @EncodeDefault(EncodeDefault.Mode.NEVER) val diplomaticEffect: CaptureDiplomaticEffect? = null,
)

@Serializable
data class CaptureDecision(val title: String, val paragraphs: List<String>, val options: List<CaptureOption>)

@Serializable
data class PlayerResult(
    val outcome: String,
    val winningCivilizationId: String?,
    val winningCivilizationName: String?,
    val victoryType: String?,
    val victoryTurn: Int?,
    val title: String,
    val paragraphs: List<String>,
)

@Serializable
data class PlayerFinalState(val epoch: String, val civilization: String, val result: PlayerResult?, val combatReports: List<CombatReport>, val messages: List<AlertContent> = emptyList(), val victory: VictoryDecisions = VictoryDecisions())

internal fun PlayerTurnSnapshot.finalState(civilizationId: String) = PlayerFinalState(epoch, civilizationId, result, combatReports, frozen(alerts.mapNotNull { it.content }), victory)

internal fun com.unciv.logic.civilization.CaptureOption.observation(active: Boolean) = CaptureOption(
    choice.name,
    label.tr(),
    frozen(paragraphs.map { it.tr() }),
    active && unavailableReasons.isEmpty(),
    frozen(unavailableReasons.map { it.tr() }),
    civilianOutcome?.let { CivilianCaptureOutcome(it.recipient, it.unitType) },
    diplomaticEffect?.let { CaptureDiplomaticEffect(it.civilizationId, it.stat, it.operation, it.value, it.before, it.after) },
)

internal fun com.unciv.logic.civilization.CaptureDecision.observation(active: Boolean) = CaptureDecision(
    title.tr(),
    frozen(paragraphs.map { it.tr() }),
    frozen(options.map { it.observation(active) }),
)

internal fun observeResult(player: Civilization): PlayerResult? = PlayerOperations(player).gameResult()?.let {
    PlayerResult(it.outcome, it.winningCivilizationId, it.winningCivilizationName, it.victoryType, it.victoryTurn, it.title.tr(), frozen(it.paragraphs.map { line -> line.tr() }))
}

internal fun gameHasResult(game: GameInfo): Boolean = !game.oneMoreTurnMode &&
    (game.victoryData != null || game.civilizations.any { it.victoryManager.hasWon() })

internal fun Civilization.hasCaptureDecision(): Boolean = popupAlerts.any { it.type in setOf(AlertType.CityConquered, AlertType.RecapturedCivilian, AlertType.CityTraded) }

/** A capture choice is still the current human player's responsibility even after a winning attack. */
internal fun gameFinished(game: GameInfo): Boolean = gameHasResult(game) && !game.currentPlayerCiv.hasCaptureDecision()

private fun <T> frozen(values: Collection<T>): List<T> = Collections.unmodifiableList(values.toList())
