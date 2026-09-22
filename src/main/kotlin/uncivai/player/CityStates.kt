package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.PlayerCityStateOperations
import com.unciv.logic.civilization.diplomacy.CityStateFunctions
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.logic.civilization.diplomacy.RelationshipLevel
import com.unciv.logic.civilization.managers.quests.AssignedQuest
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.models.ruleset.QuestName
import com.unciv.models.ruleset.tile.ResourceType
import com.unciv.models.ruleset.unique.GameContext
import com.unciv.models.translations.fillPlaceholders
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.util.Collections

@Serializable
data class CityStateCivilization(val civilizationId: String?, val name: String, val unmet: Boolean)

@Serializable
data class CityStateResource(val name: String, val amount: Int)

@Serializable
data class CityStateBonus(val relationship: String, val description: String, val active: Boolean)

@Serializable
data class CityStateContest(val yourScore: Int, val leaderScore: Int, val leaders: List<CityStateCivilization>)

@Serializable
data class CityStateQuest(val name: String, val description: String, val influenceReward: Float, val remainingTurns: Int?, val global: Boolean, val targetCivilization: CityStateCivilization?, val location: TilePosition?, val contest: CityStateContest?, val objective: CityStateQuestObjective? = null)

/** Public parameters already displayed by the quest, separated from its narrative. */
@Serializable
data class CityStateQuestObjective(val kind: String, val issuer: String, val targetName: String? = null, val goldGiftInfluenceBonusPercent: Int? = null)

@Serializable
data class CityStateDefense(val enemy: CityStateCivilization, val unitsToKill: Int, val unitsKilled: Int)

@Serializable
data class CityStateAction(val name: String, val kind: String, val title: String, val available: Boolean, val unavailableReasons: List<String>, val goldChange: Int, val influenceChange: Float, val confirmation: List<String>, val position: TilePosition?, val improvement: String?)

@Serializable
data class CityStateDetails(
    val civilizationId: String,
    val name: String,
    val type: String,
    val personality: String,
    val atWar: Boolean,
    val defeated: Boolean,
    val relationship: String,
    val influence: Float,
    val restingPoint: Float,
    val decayPerTurn: Float,
    val turnsToRelationshipChange: Int,
    val peaceTreatyTurns: Int,
    val ally: CityStateCivilization?,
    val allyInfluence: Float?,
    val protectors: List<CityStateCivilization>,
    val protecting: Boolean,
    val protectionWithdrawalCooldown: Int,
    val protectionPledgeCooldown: Int,
    val resources: List<CityStateResource>,
    val bonuses: List<CityStateBonus>,
    val uniqueUnit: String?,
    val quests: List<CityStateQuest>,
    val defenseRequests: List<CityStateDefense>,
    val tributeGoldWillingness: Int,
    val tributeWorkerWillingness: Int,
    val tributeGoldModifiers: Map<String, Int>,
    val tributeWorkerModifiers: Map<String, Int>,
    val actions: List<CityStateAction>,
    val declareWar: DiplomaticAction,
    val declarationConfirmation: List<String>,
)

@Serializable
data class CityStateUnitGift(val civilizationId: String, val action: CityStateAction, val consumesUnit: Boolean)

@Serializable
data class CityStateOutcome(
    val civilizationId: String,
    val action: String,
    val goldChange: Int,
    val influenceBefore: Float,
    val influenceAfter: Float,
    val allyBefore: CityStateCivilization?,
    val allyAfter: CityStateCivilization?,
    val protectingBefore: Boolean,
    val protectingAfter: Boolean,
    val atWarBefore: Boolean,
    val atWarAfter: Boolean,
    val completedOrRemovedQuests: List<String>,
    val gainedUnits: List<String>,
    val unitRemoved: String?,
    val position: TilePosition? = null,
    val improvement: String? = null,
)

private fun civilization(player: Civilization, other: Civilization): CityStateCivilization = if (player === other || player.knows(other)) {
    CityStateCivilization(other.civID, other.civName, false)
} else {
    CityStateCivilization(null, "Unknown civilization", true)
}

private fun PlayerCityStateOperations.Option.observation(active: Boolean) = CityStateAction(
    name, kind.name, title.tr(), active && available,
    frozen((reasons + if (active) emptyList() else listOf("Gameplay is not available.")).distinct().map(String::tr)),
    gold, influence, frozen(confirmation.map(String::tr)), position?.let { TilePosition(it.x, it.y) }, improvement,
)

internal fun observeCityStateGift(player: Civilization, unit: MapUnit, active: Boolean): CityStateUnitGift? = PlayerCityStateOperations(player).giftOption(unit)?.let {
    CityStateUnitGift(unit.currentTile.getOwner()!!.civID, it.observation(active), unit.isGreatPerson())
}

internal fun observeCityStates(player: Civilization, active: Boolean): List<CityStateDetails> {
    val operations = PlayerCityStateOperations(player)
    return frozen(
        player.getKnownCivs().filter { it.isCityState }.sortedBy { it.civID }.map { other ->
            val theirs = other.getDiplomacyManager(player)!!
            val influence = checkNotNull(operations.influence(other))
            val functions = other.cityStateFunctions
            val options = operations.options(other)
            val war = options.single { it.kind == PlayerCityStateOperations.Kind.DeclareWar }.observation(active)
            val relationship = theirs.relationshipIgnoreAfraid()
            CityStateDetails(
                other.civID, other.civName, other.cityStateType.name, other.cityStatePersonality.name,
                player.isAtWarWith(other), other.isDefeated(), theirs.relationshipLevel().name,
                influence.current, influence.restingPoint, influence.decayPerTurn, influence.turnsToRelationshipChange,
                player.getDiplomacyManager(other)!!.turnsToPeaceTreaty(),
                other.allyCiv?.let { civilization(player, it) }, other.allyCiv?.let { other.getDiplomacyManager(it)!!.getInfluence() },
                frozen(functions.getProtectorCivs().map { civilization(player, it) }),
                player in functions.getProtectorCivs(), theirs.getFlag(DiplomacyFlags.RecentlyPledgedProtection), theirs.getFlag(DiplomacyFlags.RecentlyWithdrewProtection),
                frozen(functions.getCityStateResourcesForAlly().filter { it.resource.resourceType != ResourceType.Bonus && player.canSeeResource(it.resource) }.map { CityStateResource(it.resource.name, it.amount) }.sortedBy { it.name }),
                frozen(
                    listOf(RelationshipLevel.Friend, RelationshipLevel.Ally).flatMap { level ->
                        CityStateFunctions.getCityStateBonuses(other.cityStateType, level).filterNot { it.isHiddenToUsers() }.map {
                            CityStateBonus(level.name, it.getDisplayText().tr(), relationship == level && it.conditionalsApply(GameContext(player)))
                        }.toList()
                    },
                ),
                other.cityStateUniqueUnit,
                frozen(other.questManager.getAssignedQuestsFor(player).map { quest(player, it) }.toList()),
                frozen(other.getKnownCivs().filter { it !== player && other.questManager.isWarWithMajorActive(it) }.map { CityStateDefense(civilization(player, it), other.questManager.unitsToKill(it), other.questManager.unitsKilledSoFar(it, player)) }.toList()),
                functions.getTributeWillingness(player), functions.getTributeWillingness(player, true),
                Collections.unmodifiableMap(functions.getTributeModifiers(player, requireWholeList = true).toMap()),
                Collections.unmodifiableMap(functions.getTributeModifiers(player, demandingWorker = true, requireWholeList = true).toMap()),
                frozen(
                    options.filter { option ->
                        option.kind != PlayerCityStateOperations.Kind.DeclareWar && (
                            option.position == null ||
                                player.gameInfo.tileMap[option.position!!].let { tile -> tile.isExplored(player) && tile.tileResource?.let(player::canSeeResource) == true }
                            )
                    }.map { it.observation(active) },
                ),
                DiplomaticAction(war.available, war.unavailableReasons), war.confirmation,
            )
        }.toList(),
    )
}

private fun quest(player: Civilization, assigned: AssignedQuest): CityStateQuest {
    val manager = assigned.assignerCiv.questManager
    val namesCivilization = assigned.questNameInstance in setOf(QuestName.ConquerCityState, QuestName.BullyCityState, QuestName.FindPlayer, QuestName.DenounceCiv, QuestName.GiveGold, QuestName.PledgeToProtect)
    val target = if (namesCivilization) player.gameInfo.civilizations.firstOrNull { it.civID == assigned.data1 }?.let { civilization(player, it) } else null
    val description = if (namesCivilization) assigned.quest.description.fillPlaceholders(target?.name ?: "Unknown civilization") else assigned.getDescription()
    val location = when (assigned.questNameInstance) {
        QuestName.ClearBarbarianCamp -> assigned.data1.toIntOrNull()?.let { x -> assigned.data2.toIntOrNull()?.let { y -> TilePosition(x, y) } }
        QuestName.Route -> assigned.assignerCiv.getCapital()?.getCenterTile()?.takeIf { it.isExplored(player) }?.position?.let { TilePosition(it.x, it.y) }
        else -> null
    }
    return CityStateQuest(
        assigned.questName,
        description.tr(),
        assigned.getInfluence(),
        assigned.getRemainingTurns().takeIf { assigned.doesExpire() },
        assigned.isGlobal(),
        target,
        location,
        manager.getGlobalQuestStanding(assigned)?.let { standing ->
            CityStateContest(standing.yourScore, standing.leaderScore, frozen(standing.leaders.map { civilization(player, player.gameInfo.getCivilization(it)) }))
        },
        if (assigned.questNameInstance == QuestName.None) {
            null
        } else {
            CityStateQuestObjective(
                assigned.questName,
                assigned.assignerCiv.civID,
                // Never copy arbitrary data1: it can be a hidden civilization ID,
                // location or private contest baseline. These names are UI targets.
                targetName = assigned.data1.takeIf {
                    assigned.questNameInstance in setOf(QuestName.ConstructWonder, QuestName.ConnectResource, QuestName.GreatPerson, QuestName.FindNaturalWonder, QuestName.SpreadReligion)
                },
                goldGiftInfluenceBonusPercent = assigned.data1.toIntOrNull().takeIf { assigned.questNameInstance == QuestName.Invest },
            )
        },
    )
}

internal fun cityStateOutcome(request: TurnCommandRequest, before: PlayerTurnSnapshot, after: PlayerTurnSnapshot, cityState: String): CityStateOutcome {
    val old = before.diplomacy.cityStates.single { it.civilizationId == cityState }
    val new = after.diplomacy.cityStates.single { it.civilizationId == cityState }
    val option = old.actions.firstOrNull { it.name == request.selection }.takeIf { request.kind == TurnCommandKind.CITY_STATE_ACTION }
    return CityStateOutcome(
        cityState, request.selection ?: request.kind.name, after.economy!!.treasury - before.economy!!.treasury,
        old.influence, new.influence, old.ally, new.ally, old.protecting, new.protecting, old.atWar, new.atWar,
        frozen(old.quests.filter { q -> new.quests.none { it.name == q.name } }.map { it.name }),
        frozen(after.map.units.filter { unit -> before.map.units.none { it.handle == unit.handle } }.map { it.handle }),
        request.target?.takeIf { target -> before.map.units.any { it.handle == target } && after.map.units.none { it.handle == target } },
        option?.position, option?.improvement,
    )
}

internal fun CityStateOutcome.detached() = copy(completedOrRemovedQuests = frozen(completedOrRemovedQuests), gainedUnits = frozen(gainedUnits))

private fun <T> frozen(values: List<T>): List<T> = Collections.unmodifiableList(values.toList())
