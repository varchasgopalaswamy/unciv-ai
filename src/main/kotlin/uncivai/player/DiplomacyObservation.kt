package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerOperations
import com.unciv.logic.civilization.PopupAlert
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class FirstContactDetails(
    val civilizationId: String,
    val civilizationName: String,
    val leaderName: String,
    val message: String,
    val acknowledgement: String,
)

@Serializable
data class KnownCivilization(
    val civilizationId: String,
    val civilizationName: String,
    val leaderName: String,
    val atWar: Boolean,
)

@Serializable
data class DiplomacyObservation(val knownCivilizations: List<KnownCivilization> = emptyList())

/** Copy the introduction of an actual player-owned alert, never a caller-supplied civilization. */
internal fun observeFirstContact(operations: PlayerOperations, alert: PopupAlert): FirstContactDetails? = operations.firstContactIntroduction(alert)?.let {
    FirstContactDetails(it.civilizationId, it.civilizationName, it.leaderName, it.message.tr(), it.acknowledgement.tr())
}

/** The desktop diplomacy list filters to met, surviving civilizations before showing their identity. */
internal fun observeDiplomacy(player: Civilization): DiplomacyObservation = DiplomacyObservation(
    knownCivilizations = Collections.unmodifiableList(
        player.diplomacyFunctions.getKnownCivsSorted(includeCityStates = false)
            .filter { it.isMajorCiv() }
            .map {
                KnownCivilization(
                    civilizationId = it.civID,
                    civilizationName = it.civName,
                    leaderName = it.getLeaderDisplayName(),
                    atWar = player.isAtWarWith(it),
                )
            }.toList(),
    ),
)
