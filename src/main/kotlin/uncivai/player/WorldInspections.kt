package uncivai.player

import com.unciv.logic.civilization.Civilization
import com.unciv.logic.civilization.diplomacy.DiplomacyFlags
import com.unciv.models.ruleset.Policy.PolicyBranchType
import com.unciv.models.translations.tr
import com.unciv.ui.screens.overviewscreen.WonderInfo
import kotlinx.serialization.json.JsonObject
import uncivai.obj

internal fun inspectPolitics(player: Civilization, request: PlayerInspectionRequest): JsonObject {
    val known = player.gameInfo.civilizations.filter { !it.isBarbarian && !it.isSpectator() && (it === player || player.knows(it)) }
    return inspectionPage(
        known.sortedBy { it.civID }.map { civ ->
            obj(
                "civilizationId" to civ.civID,
                "name" to civ.civName,
                "cityState" to civ.isCityState,
                "defeated" to civ.isDefeated(),
                "era" to civ.tech.era.name,
                "policyBranches" to civ.policies.branches.filter { civ.policies.isAdopted(it.name) }.map { branch ->
                    obj("name" to branch.name, "count" to 1 + branch.policies.count { it.policyBranchType != PolicyBranchType.BranchComplete && civ.policies.isAdopted(it.name) })
                },
                "relations" to known.filter { it !== civ }.flatMap { other ->
                    val diplomacy = civ.getDiplomacyManager(other)
                    buildList {
                        if (civ.isAtWarWith(other)) add(obj("with" to other.civID, "kind" to "War"))
                        for (flag in listOf(DiplomacyFlags.DefensivePact, DiplomacyFlags.DeclarationOfFriendship, DiplomacyFlags.Denunciation)) {
                            if (diplomacy?.hasFlag(flag) == true) add(obj("with" to other.civID, "kind" to flag.name, "turnsRemaining" to diplomacy.getFlag(flag)))
                        }
                        if (civ.isCityState && civ.allyCiv === other) add(obj("with" to other.civID, "kind" to "Ally"))
                    }
                },
            )
        },
        request,
    )
}

internal fun inspectWonders(player: Civilization, request: PlayerInspectionRequest): JsonObject = inspectionPage(
    WonderInfo().collectInfo(player).filter { it.status != WonderInfo.WonderStatus.Hidden }.map { wonder ->
        val located = wonder.status in setOf(WonderInfo.WonderStatus.Known, WonderInfo.WonderStatus.Owned)
        val location = wonder.location?.takeIf { located && it.isExplored(player) }
        val owner = wonder.civ?.takeIf { located && (it === player || player.knows(it)) }
        obj(
            "name" to wonder.name.takeUnless { wonder.status == WonderInfo.WonderStatus.Unknown },
            "kind" to wonder.category.name,
            "status" to wonder.status.name,
            "civilizationId" to owner?.civID,
            "city" to wonder.city?.takeIf { located && it.getCenterTile().isExplored(player) }?.name,
            "position" to location?.position?.inspectionPosition(),
        )
    }.sortedBy { it.toString() },
    request,
)

internal fun inspectNotifications(player: Civilization, request: PlayerInspectionRequest): JsonObject = inspectionPage(
    player.notificationsLog.sortedByDescending { it.turn }.filter { request.turn == null || it.turn == request.turn }.flatMap { log ->
        log.notifications.map { notification ->
            obj(
                "turn" to log.turn,
                "text" to visibleNotificationText(player, notificationGameplayText(notification.text)).tr(),
                "category" to notification.category.name,
                "icons" to notification.icons.filter { icon -> player.gameInfo.civilizations.none { it !== player && !player.knows(it) && (it.civName == icon || it.civID == icon) } },
                "locations" to notification.locations().map { obj("x" to it.x, "y" to it.y) },
            )
        }
    },
    request,
)

internal fun inspectReplay(player: Civilization, request: PlayerInspectionRequest): JsonObject {
    val game = player.gameInfo
    if (game.turns < 5 && game.victoryData == null && player.isAlive()) return obj("available" to false, "reason" to "Replay is available from turn 5")
    val turn = request.turn ?: game.turns
    val tiles = game.tileMap.tileList.filter { it.isExplored(player) }.sortedWith(compareBy({ it.position.x }, { it.position.y }))
    val rows = tiles.map { tile ->
        val past = tile.history.getState(turn)
        val owner = past.owningCivName?.let { id -> game.civilizations.singleOrNull { it.civID == id } }
        obj(
            "position" to tile.position.inspectionPosition(),
            "occupied" to (past.owningCivName != null),
            "civilizationId" to owner?.takeIf { it === player || player.knows(it) }?.civID,
            "cityCenter" to past.cityCenterType.name,
        )
    }
    return JsonObject(
        inspectionPage(rows, request) + obj(
            "available" to true,
            "replayTurn" to turn,
            "basis" to "Native territorial history within your currently explored map; undisclosed owners are unnamed.",
        ),
    )
}
