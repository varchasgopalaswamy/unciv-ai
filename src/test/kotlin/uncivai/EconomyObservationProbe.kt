package uncivai

import com.unciv.json.json
import com.unciv.logic.city.CityFlags
import com.unciv.logic.civilization.diplomacy.DiplomaticStatus
import com.unciv.logic.trade.Trade
import com.unciv.logic.trade.TradeOffer
import com.unciv.logic.trade.TradeOfferType
import com.unciv.logic.trade.TradeRequest
import com.unciv.models.stats.Stats
import uncivai.player.EconomyAmount
import uncivai.player.EconomyObservation
import uncivai.player.PlayerHost
import uncivai.player.PlayerTurnJson
import uncivai.player.observeEconomy
import java.nio.file.Path
import kotlin.math.roundToInt

/** Trusted paired-world fixtures; observation is exclusively through the player boundary. */
object EconomyObservationProbe {
    @JvmStatic
    fun main(args: Array<String>) {
        val root = Path.of(args.single())
        hiddenOpponent(root)
        overview(root)
    }

    private fun hiddenOpponent(root: Path) {
        HeadlessRuntime.createDuel(root.resolve("unknown"), 412L).use { runtime ->
            val player = runtime.game.getCivilization("Rome")
            val other = runtime.game.getCivilization("Greece")
            val session = PlayerHost(runtime).bindPlayer("Rome")
            check(!player.knows(other))
            val before = session.turn()
            other.addGold(999)
            other.tech.addTechnology("Animal Husbandry", false)
            other.units.getCivUnits().first().promotions.XP += 50
            other.units.getCivUnits().first().currentTile.tileResource = runtime.game.ruleset.tileResources.getValue("Horses")
            other.cache.updateCivResources()
            check(session.turn() == before) { "Unmet opponent's economy changed the player's snapshot or revision" }

            player.diplomacyFunctions.makeCivilizationsMeet(other)
            val met = session.turn()
            other.addGold(999)
            other.units.getCivUnits().first().currentTile.tileResource = runtime.game.ruleset.tileResources.getValue("Iron")
            other.updateStatsForNextTurn()
            check(session.turn() == met) { "Hidden met-opponent economy changed the player's snapshot or revision" }
            check(met.economy == before.economy) { "Contact alone exposed an opponent's economic information" }
        }
    }

    private fun overview(root: Path) {
        HeadlessRuntime.createDuel(root.resolve("overview"), 412L, combatScenario = true).use { runtime ->
            val game = runtime.game
            val player = game.getCivilization("Rome")
            val other = game.getCivilization("Greece")
            val capital = player.getCapital()!!
            for (civilization in listOf(player, other)) {
                civilization.getDiplomacyManager(if (civilization == player) other else player)!!.diplomaticStatus = DiplomaticStatus.Peace
                for (technology in listOf("Animal Husbandry", "Bronze Working", "Iron Working")) {
                    civilization.tech.addTechnology(technology, false)
                }
            }
            val ownTiles = capital.getTiles().filter { !it.isCityCenter() }.toList()
            check(ownTiles.size >= 4)
            ownTiles[0].tileResource = game.ruleset.tileResources.getValue("Iron")
            ownTiles[0].resourceAmount = 6
            ownTiles[0].improvement = "Mine"
            ownTiles[1].tileResource = game.ruleset.tileResources.getValue("Gold Ore")
            ownTiles[1].improvement = "Mine"
            ownTiles[2].tileResource = game.ruleset.tileResources.getValue("Horses")
            ownTiles[2].resourceAmount = 4
            ownTiles[2].improvement = null
            // A resource under our own borders is still invisible before its revealing technology.
            ownTiles[3].tileResource = game.ruleset.tileResources.getValue("Uranium")
            ownTiles[3].resourceAmount = 7
            ownTiles[3].improvement = null
            checkNotNull(player.units.placeUnitNearTile(capital.location, "Legion"))
            repeat(4) { checkNotNull(player.units.placeUnitNearTile(capital.location, "Warrior")) }
            capital.demandedResource = "Marble"
            val outpost = player.cities.single { !it.isCapital() }
            outpost.demandedResource = "Gold Ore"
            outpost.setFlag(CityFlags.WeLoveTheKing, 5)

            // Distinct active agreements permit reading incoming and outgoing resources even
            // when their overview Trade row nets those directions into one number.
            val outgoing = Trade().apply {
                ourOffers.add(offer("Iron", TradeOfferType.Strategic_Resource, 2))
                theirOffers.add(offer("Gold", TradeOfferType.Gold_Per_Turn, 4))
            }
            val incoming = Trade().apply {
                theirOffers.add(offer("Iron", TradeOfferType.Strategic_Resource, 3))
                ourOffers.add(offer("Gold Ore", TradeOfferType.Luxury_Resource, 1))
            }
            for (trade in listOf(outgoing, incoming)) {
                player.getDiplomacyManager(other)!!.trades.add(trade)
                other.getDiplomacyManager(player)!!.trades.add(trade.reverse())
            }
            val proposal = Trade().apply {
                theirOffers.add(offer("Iron", TradeOfferType.Strategic_Resource, 1))
            }
            other.tradeRequests.add(TradeRequest(player.civID, proposal))
            // Incoming requests are not resources this player has offered.
            player.tradeRequests.add(
                TradeRequest(
                    other.civID,
                    Trade().apply {
                        theirOffers.add(offer("Horses", TradeOfferType.Strategic_Resource, 2))
                    },
                ),
            )
            player.cache.updateCivResources()
            player.cities.forEach { it.cityStats.update() }
            player.updateStatsForNextTurn()
            val session = PlayerHost(runtime).bindPlayer("Rome")
            val initial = checkNotNull(session.turn().economy)
            check(initial.treasury == player.gold && initial.unitMaintenance > 0)
            check(initial.goldBreakdown.any { it.source == "Trade" && it.amount == 4 })
            val iron = initial.resources.single { it.name == "Iron" }
            check(iron.available == 6 && iron.used == 1 && iron.imported == 3 && iron.exported == 2 && iron.offered == 1) { "Wrong iron accounting: $iron" }
            check(iron.breakdown.single { it.source == "Trade" }.amount == 1)
            check(initial.resources.single { it.name == "Gold Ore" }.run { available == 0 && exported == 1 && citiesCelebrating == 1 })
            check(initial.resources.single { it.name == "Horses" }.run { available == 0 && offered == 0 && unimprovedOwnedTiles == 1 && unimprovedAlliedCityStateTiles == 0 })
            check(initial.resources.single { it.name == "Marble" }.run { available == 0 && citiesDemanding == 1 })
            check(initial.resources.none { it.name == "Uranium" || it.type == "Bonus" })
            check(initial.cityConnections.size == 2 && initial.cityConnections.none { it.name in setOf("Athens", "Sparta") })
            for (connection in initial.cityConnections) {
                val city = player.cities.single { it.name == connection.name }
                check(connection.capital == city.isCapital() && connection.connectedToCapital == city.isConnectedToCapital())
            }
            check(initial.unitSupply.run { inUse == player.units.getCivUnitsSize() && total == base + fromCities + fromPopulation })

            // Exact fractional engine internals are not displayed in StatsOverviewTab.
            // Boundary rows round individually, while the total rounds their unrounded sum.
            capital.cityStats.finalStatList = linkedMapOf("Income" to Stats(gold = 1.4f), "Expenses" to Stats(gold = -0.6f), "Below display precision" to Stats(gold = 0.4f))
            capital.cityStats.happinessList = linkedMapOf("Fraction A" to 0.4f, "Fraction B" to 0.4f, "Unhappiness" to -1.6f)
            outpost.cityStats.finalStatList = linkedMapOf()
            outpost.cityStats.happinessList = linkedMapOf()
            val engineGold = player.stats.getStatMapForNextTurn()
            val engineHappiness = player.stats.getHappinessBreakdown()
            val persisted = json().toJson(game)
            val resourceCache = player.detailedCivResources
            val cityStatsCache = capital.cityStats.finalStatList
            val observed = checkNotNull(session.turn().economy)
            check(observed.goldBreakdown.containsAll(listOf(EconomyAmount("Income", 1), EconomyAmount("Expenses", -1))))
            check(observed.goldBreakdown.none { it.source == "Below display precision" })
            check(observed.happinessBreakdown.contains(EconomyAmount("Unhappiness", -2)))
            check(observed.happinessBreakdown.none { it.source.startsWith("Fraction") })
            check(observed.goldPerTurn == engineGold.values.map { it.gold }.sum().roundToInt())
            check(observed.happiness == engineHappiness.values.sum().roundToInt())
            repeat(3) { check(session.turn().economy == observed) }
            check(json().toJson(game) == persisted) { "Reading economy changed persisted game state" }
            check(player.detailedCivResources === resourceCache && capital.cityStats.finalStatList === cityStatsCache) { "Reading economy refreshed mutable engine caches" }
            check(observed == PlayerTurnJson.json.decodeFromString<EconomyObservation>(PlayerTurnJson.json.encodeToString(EconomyObservation.serializer(), observed)))
            immutable(observed.resources)
            immutable(observed.goldBreakdown)
            immutable(observed.happinessBreakdown)
            immutable(observed.cityConnections)
            immutable(observed.resources.first().breakdown)

            val encoded = PlayerTurnJson.json.encodeToString(EconomyObservation.serializer(), observed)
            player.addGold(17)
            capital.cityStats.finalStatList.getValue("Income").gold = 19f
            player.detailedCivResources.first { it.resource.name == "Iron" && it.origin == "Trade" }.amount = 2
            proposal.theirOffers.single().amount = 2
            check(PlayerTurnJson.json.encodeToString(EconomyObservation.serializer(), observed) == encoded) { "Snapshot retained mutable engine references" }
            check(runtime.accessGame { observeEconomy(player) } != observed)
        }
    }

    private fun offer(name: String, type: TradeOfferType, amount: Int) = TradeOffer(name, type, amount, duration = 20)

    private fun immutable(values: List<*>) {
        check(runCatching { (values as MutableList).clear() }.exceptionOrNull() is UnsupportedOperationException)
    }
}
