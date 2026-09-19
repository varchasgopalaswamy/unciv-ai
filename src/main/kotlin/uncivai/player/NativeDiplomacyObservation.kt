package uncivai.player

import com.unciv.logic.civilization.AlertType
import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerDiplomaticCommunicationOperations
import com.unciv.logic.civilization.PopupAlert
import com.unciv.logic.civilization.diplomacy.Demand
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.civilization.diplomacy.DiplomaticModifiers
import com.unciv.models.translations.fillPlaceholders
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import uncivai.GameExecutor
import uncivai.SocialPromiseAdvisory
import java.util.Collections

@Serializable
data class NativeDiplomaticChoice(val name: String, val text: String, val available: Boolean, val consequences: List<String>, val socialAdvisories: List<SocialPromiseAdvisory> = emptyList())

@Serializable
data class NativeDiplomaticDecision(val civilizationId: String, val content: AlertContent, val options: List<NativeDiplomaticChoice>)

@Serializable
data class NativeDemandOption(val name: String, val text: String, val action: DiplomaticAction)

@Serializable
data class NativePromise(val kind: String, val promiser: String, val promisee: String, val turnsRemaining: Int, val text: String)

@Serializable
data class NativeOpinionModifier(val name: String, val text: String, val amount: Float)

@Serializable
data class NativeDiplomacyDetails(
    val builtInAI: Boolean,
    val supportsChat: Boolean,
    val denounce: DiplomaticAction,
    val demands: List<NativeDemandOption>,
    val ourDenunciationTurns: Int,
    val theirDenunciationTurns: Int,
    val relationship: String?,
    val opinionModifiers: List<NativeOpinionModifier>,
    val promises: List<NativePromise>,
)

internal fun nativeDecision(player: Civilization, alert: PopupAlert, active: Boolean, runtime: GameExecutor): NativeDiplomaticDecision? = PlayerDiplomaticCommunicationOperations(player).decision(alert)?.let { decision ->
    NativeDiplomaticDecision(
        decision.civilizationId,
        decision.content.toPlayerContent(),
        immutable(
            decision.options.map {
                val war = it.name == "DeclareWar" || (alert.type == AlertType.DemandToNotAttackUs && it.name == "Refuse")
                NativeDiplomaticChoice(
                    it.name,
                    it.text.tr(),
                    active && it.available,
                    immutable(it.consequences.map(String::tr)),
                    if (war) immutable(runtime.communication.peaceAdvisories(player.civID, decision.civilizationId)) else emptyList(),
                )
            },
        ),
    )
}

internal fun nativeDiplomacy(player: Civilization, other: Civilization, active: Boolean): NativeDiplomacyDetails {
    val operations = PlayerDiplomaticCommunicationOperations(player)
    val own = player.getDiplomacyManager(other)!!
    val theirs = other.getDiplomacyManager(player)!!
    fun action(value: com.unciv.logic.civilization.PlayerDiplomaticAction) = DiplomaticAction(
        active && value.available,
        immutable((value.unavailableReasons + if (active) emptyList() else listOf("Gameplay is not available.")).distinct()),
    )
    return NativeDiplomacyDetails(
        other.isAI(), other.isHuman(), action(checkNotNull(operations.denounceOption(other))),
        immutable(operations.demands(other).map { NativeDemandOption(it.name, it.text.tr(), action(it.action)) }),
        own.getFlag(DiplomacyFlags.Denunciation), theirs.getFlag(DiplomacyFlags.Denunciation),
        if (other.isAI()) theirs.relationshipLevel().name else null,
        immutable(
            if (other.isAI()) {
                theirs.diplomaticModifiers.entries.mapNotNull { (name, value) ->
                    val modifier = DiplomaticModifiers.safeValueOf(name) ?: return@mapNotNull null
                    if (modifier == DiplomaticModifiers.AttackedProtectedMinor && theirs.hasModifier(DiplomaticModifiers.DestroyedProtectedMinor)) return@mapNotNull null
                    NativeOpinionModifier(name, modifier.text.tr(), value)
                }.sortedBy { it.name }
            } else {
                emptyList()
            },
        ),
        immutable(
            Demand.entries.flatMap { demand ->
                listOfNotNull(
                    NativePromise(demand.name, player.civID, other.civID, theirs.getFlag(demand.agreedToDemand), demand.wePromisedText.fillPlaceholders(theirs.getFlag(demand.agreedToDemand).toString()).tr()).takeIf { theirs.hasFlag(demand.agreedToDemand) },
                    NativePromise(demand.name, other.civID, player.civID, own.getFlag(demand.agreedToDemand), demand.theyPromisedText.fillPlaceholders(own.getFlag(demand.agreedToDemand).toString()).tr()).takeIf { own.hasFlag(demand.agreedToDemand) },
                )
            },
        ),
    )
}

private fun <T> immutable(values: List<T>): List<T> = Collections.unmodifiableList(values.toList())
