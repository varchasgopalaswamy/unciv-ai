package uncivai

import com.unciv.json.json
import com.unciv.logic.civilization.PlayerUnitOrders
import com.unciv.logic.map.HexCoord
import com.unciv.logic.map.mapunit.MapUnit
import com.unciv.logic.map.mapunit.UnitTurnManager
import com.unciv.models.ruleset.unit.Promotion
import uncivai.player.PlayerHost
import uncivai.player.PromotionExperience
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest
import uncivai.player.UnitRecovery
import uncivai.player.observeUnitCombat
import java.nio.file.Path

object PromotionObservationProbe {
    fun run(root: Path) = SettlementPreviewProbe.fixture(root).use { runtime ->
        val game = runtime.game
        val player = game.getCivilization("Rome")
        player.units.getCivUnits().toList().forEach { it.destroy() }
        val warrior = player.units.placeUnitNearTile(HexCoord(0, 0), "Warrior")!!
        warrior.health = 40
        warrior.promotions.XP = 13
        fun status(unit: MapUnit = warrior) = observeUnitCombat(player, unit, true).promotions!!
        val before = json().toJson(game)
        val initial = status()
        val heal = initial.choices.single { it.name == "Heal Instantly" }
        val shock = initial.choices.single { it.name == "Shock I" }
        check(heal.available && !heal.retainedAfterSelection && heal.prerequisiteUnlocks.isEmpty())
        check(heal.afterSelection == PromotionExperience(3, 20, 17))
        check(shock.available && shock.retainedAfterSelection && shock.afterSelection == heal.afterSelection)
        check(shock.prerequisiteUnlocks.map { it.name }.toSet() == setOf("Shock II", "Amphibious", "Medic"))
        check(shock.prerequisiteUnlocks.all { it.description.isNotBlank() })
        check(initial.experience == 13 && initial.totalExperience == 13 && initial.barbarianExperienceLimit == 30)
        check(initial.canEarnExperienceFromBarbarians)
        check(initial.recovery == UnitRecovery(25, 25, 3))
        check(PlayerHost(runtime).bindPlayer("Rome").turn().map.units.single().combat!!.promotions == initial)
        check(json().toJson(game) == before) { "Promotion comparison changed native state" }

        // Rest estimates count the missed healing at the current turn's end after moving or attacking.
        warrior.currentMovement -= 1f
        check(status().recovery == UnitRecovery(0, 25, 4))
        warrior.currentMovement = warrior.getMaxMovement().toFloat()
        warrior.attacksThisTurn = 1
        check(status().recovery == UnitRecovery(0, 25, 4))
        check(status().choices.none { it.available || it.afterSelection != null })
        warrior.attacksThisTurn = 0
        warrior.health = 95
        check(status().recovery == UnitRecovery(5, 5, 1))
        warrior.health = 100
        check(status().recovery == UnitRecovery(0, 0, 0))

        // Two instant heals spend the same experience thresholds as permanent promotions.
        warrior.health = 40
        val orders = PlayerUnitOrders(player)
        val session = PlayerHost(runtime).bindPlayer("Rome")
        val turn = session.turn()
        val receipt = session.execute(TurnCommandRequest("heal", turn.epoch, turn.revision, TurnCommandKind.PROMOTE_UNIT, turn.map.units.single().handle, "Heal Instantly"))
        check(receipt.actionSummary.contains("health 40 to 90"))
        check(receipt.actionSummary.contains("promotion not retained"))
        check(warrior.health == 90 && warrior.promotions.promotions.isEmpty())
        check(status().experience == heal.afterSelection.experienceRemaining)
        check(status().experienceForNextPromotion == heal.afterSelection.nextPromotionCost)
        warrior.promotions.XP = 21
        warrior.health = 32
        val second = status()
        check(second.totalExperience == 31 && !second.canEarnExperienceFromBarbarians)
        val secondHeal = second.choices.single { it.name == "Heal Instantly" }
        check(secondHeal.afterSelection == PromotionExperience(1, 30, 29))
        check(orders.tryPromote(warrior, "Heal Instantly"))
        check(warrior.health == 82 && warrior.promotions.promotions.isEmpty())
        val spent = status()
        check(spent.earnedPromotions == 2 && spent.totalExperience == 31 && spent.experience == 1)
        check(!spent.canEarnExperienceFromBarbarians && spent.choices.none { it.available })

        val other = player.units.placeUnitNearTile(HexCoord(1, 0), "Warrior")!!
        other.promotions.XP = 13
        check(orders.tryPromote(other, "Shock I"))
        check(status(other).choices.single { it.name == "Shock I" }.acquired)
        check(other.promotions.XP == 3 && other.promotions.xpForNextPromotion() == 20)
        other.promotions.XP = 19
        check(status(other).totalExperience == 29 && status(other).canEarnExperienceFromBarbarians)
        other.promotions.XP = 20
        check(status(other).totalExperience == 30 && !status(other).canEarnExperienceFromBarbarians)

        // Free choices leave the paid promotion threshold untouched.
        val free = Promotion().apply {
            name = "Free test promotion"
            unitTypes = listOf(other.type.name)
            uniques = arrayListOf("This Promotion is free")
        }
        game.ruleset.unitPromotions[free.name] = free
        val freeChoice = status(other).choices.single { it.name == free.name }
        check(freeChoice.experienceCost == 0 && freeChoice.afterSelection == PromotionExperience(20, 20, 0))
        check(orders.tryPromote(other, free.name))
        check(other.promotions.XP == 20 && other.promotions.numberOfPromotions == 1)

        // A damaged ship on neutral water cannot recover by resting there.
        val sea = game.tileMap[4, 0]
        sea.setBaseTerrain(game.ruleset.terrains.getValue("Coast"))
        sea.setTerrainTransients()
        val ship = player.units.placeUnitNearTile(sea.position, "Trireme")!!
        check(ship.currentTile === sea)
        ship.health = 50
        check(status(ship).recovery == UnitRecovery(0, 0, null))

        // Final healing bonuses, health caps and March agree with actual native turn-end healing.
        warrior.health = 40
        warrior.promotions.addPromotion("Rejuvenation", isFree = true)
        val beforeRecovery = json().toJson(game)
        check(status().recovery == UnitRecovery(50, 50, 2))
        check(observeUnitCombat(player, warrior, true).order!!.healingPerTurn == 50)
        check(json().toJson(game) == beforeRecovery) { "Healing estimate changed native state" }
        UnitTurnManager(warrior).endTurn()
        check(warrior.health == 90)
        check(status().recovery == UnitRecovery(10, 10, 1))
        warrior.health = 40
        warrior.currentMovement = 0f
        warrior.attacksThisTurn = 1
        warrior.promotions.addPromotion("March", isFree = true)
        check(status().recovery == UnitRecovery(50, 50, 2))
        UnitTurnManager(warrior).endTurn()
        check(warrior.health == 90)
    }
}
