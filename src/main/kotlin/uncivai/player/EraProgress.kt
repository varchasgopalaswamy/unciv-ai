package uncivai.player

import com.unciv.logic.civilization.Civilization
import kotlinx.serialization.Serializable

@Serializable
data class EraProgress(val current: String, val highestKnownMajor: String, val modernOrLater: Boolean)

/** The same known civilizations' eras exposed by the politics overview. */
internal fun observeEraProgress(player: Civilization): EraProgress {
    val highest = player.gameInfo.civilizations.asSequence()
        .filter { it.isMajorCiv() && (it === player || player.knows(it)) }
        .map { it.tech.era }
        .maxByOrNull { it.eraNumber } ?: player.tech.era
    val modern = player.gameInfo.ruleset.eras["Modern era"]
    return EraProgress(player.tech.era.name, highest.name, modern != null && highest.eraNumber >= modern.eraNumber)
}
