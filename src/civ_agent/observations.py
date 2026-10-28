"""Model-facing projections of the host's already permission-filtered snapshot.

Detail reads reuse that snapshot. They never query hidden engine state or count as
reading any information outside the returned projection.
"""

import copy
import difflib
import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from civ_agent.action_templates import (
    add_city_commands,
    add_construction_commands,
    add_overview_commands,
    add_unit_commands,
)
from civ_agent.construction_estimates import (
    ESTIMATE_BASIS,
    completion_summary,
    construction_estimate,
    investment_comparison,
    overview_estimates,
    queue_progress,
)
from civ_agent.delivery_options import DeliveryOptions
from civ_agent.entity_sections import (
    CityDetails,
    EntityDetails,
    UnitDetails,
    project_entity,
)
from civ_agent.game_commands import Position
from civ_agent.map_format import compact_tiles
from civ_agent.mechanics_overview import (
    citizens_summary,
    exploration_summary,
    happiness_buildings,
    region_distance,
    research_summary,
    resource_summary,
    settlement_summary,
)
from civ_agent.protocol import JsonObject
from civ_agent.review_context import review_position
from civ_agent.schemas import BoundaryModel


class ConstructionDetails(EntityDetails, DeliveryOptions):
    model_config = {
        "json_schema_extra": {
            "allOf": [
                {
                    "if": {"required": ["horizonTurns"], "properties": {"horizonTurns": {"type": "integer"}}},
                    "then": {"required": ["names"], "properties": {"names": {"minItems": 1}}},
                }
            ]
        }
    }
    names: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)
    horizonTurns: (
        Annotated[int, Field(ge=1, le=100, description="Comparison horizon; requires a nonempty names list.")] | None
    ) = None

    @model_validator(mode="after")
    def comparison_requires_names(self):
        if self.horizonTurns is not None and not self.names:
            raise ValueError("Supply names when comparing investments over horizonTurns")
        return self


class MapRegion(DeliveryOptions):
    center: Position
    radius: Annotated[int, Field(ge=0, le=4)] = 2
    format: Literal["compact", "full"] = "compact"


class PlayerDetails(DeliveryOptions):
    section: Literal["economy", "diplomacy", "research", "policies", "turn_history", "map_index", "settlement"]


class DetailQuery(BoundaryModel):
    tool: Literal[
        "city_details",
        "unit_details",
        "construction_details",
        "map_region",
        "player_details",
        "movement_preview",
        "mission_preview",
        "player_inspect",
        "research_details",
        "policy_details",
        "victory_progress",
        "player_result",
        "trade_options",
        "trade_read",
        "agreement_list",
        "agreement_read",
        "chat_read",
    ]
    arguments: JsonObject


class DetailResult(BoundaryModel):
    evidence: str
    turn: int
    query: DetailQuery
    available: bool
    data: JsonObject | None = None
    detailUpdate: JsonObject | None = None
    constructionExplanations: dict[str, str] | None = None
    stateUpdate: JsonObject | None = None
    memoryUpdates: list[JsonObject] | None = None
    economyCoordination: JsonObject | None = None
    coordinationUpdate: JsonObject | None = None


DETAIL_MODELS: dict[str, type[BoundaryModel]] = {
    "city_details": CityDetails,
    "unit_details": UnitDetails,
    "construction_details": ConstructionDetails,
    "map_region": MapRegion,
    "player_details": PlayerDetails,
}

# Native/communication reads cannot be regenerated from the cached turn snapshot.
# Conservatively expire them when an observed native or social state changes.
EXTERNAL_DETAIL_TOOLS = {
    "research_details",
    "policy_details",
    "victory_progress",
    "player_result",
    "trade_options",
    "trade_read",
    "agreement_list",
    "agreement_read",
    "chat_read",
}


class CitySummary(BoundaryModel):
    borderExpansion: JsonObject = Field(default_factory=dict)
    resistanceTurns: int = 0
    beingRazed: bool = False
    demandedResource: str | None = None
    weLoveTheKingTurns: int = 0
    handle: str
    name: str
    position: Position | None = None
    population: int | None = None
    health: int | None = None
    maxHealth: int | None = None
    defensiveStrength: int | None = None
    foodPerTurn: int | None = None
    turnsToGrowth: int | None = None
    turnsToStarvation: int | None = None
    constructionQueue: list[str]
    constructionChoice: JsonObject | None = None
    constructionConversion: JsonObject | None = None
    constructionEstimates: JsonObject = Field(default_factory=dict)
    puppet: bool
    inResistance: bool
    attackOptionCount: int
    attacksRemaining: int
    yields: JsonObject = Field(default_factory=dict)
    citizens: JsonObject = Field(default_factory=dict)


class UnitSummary(BoundaryModel):
    statuses: list[JsonObject] = Field(default_factory=list)
    handle: str
    name: str
    position: Position
    health: int
    movement: str
    military: bool
    strength: int | None = None
    rangedStrength: int | None = None
    role: str | None = None
    idle: bool
    movementSupported: bool
    legalDestinations: int
    attackOptionCount: int
    attacksRemaining: int
    canFoundCity: bool = False
    settlementStatus: str | None = None
    order: str | None = None
    automated: bool = False
    workerJob: str | None = None
    workerJobTurnsRemaining: int | None = None
    promotions: JsonObject | None = None
    availableGreatPersonAbilityCount: int = 0
    canAssembleSpaceshipPart: bool = False
    canGiftUnit: bool = False
    movementOrder: JsonObject | None = None
    escort: JsonObject | None = None
    protection: JsonObject | None = None
    canCreateWaterImprovement: bool = False
    missions: list[str] = Field(default_factory=list)
    transported: bool = False
    carriedUnits: list[str] = Field(default_factory=list)
    interceptionReady: bool = False
    inspections: JsonObject | None = None
    upgrades: list[JsonObject] = Field(default_factory=list)


class PlayerNotification(BoundaryModel):
    handle: str
    text: str
    category: str
    icons: list[str]
    locations: list[Position]
    canDismiss: bool


class TurnOverview(BoundaryModel):
    constructionExplanations: dict[str, str] | None = None
    explanationRefs: dict[str, str] | None = None
    economyBrief: JsonObject | None = None
    cityScope: JsonObject | None = None
    economyCoordination: JsonObject | None = None
    coordinationUpdate: JsonObject | None = None
    turnControls: JsonObject = Field(default_factory=dict)
    stateId: str | None = None
    memoryUpdates: list[JsonObject] | None = None
    invalidatedDetails: list[DetailQuery] | None = None
    turn: int
    phase: str
    canEndTurn: bool
    endTurn: JsonObject | None = None
    tacticalReview: JsonObject = Field(default_factory=dict)
    strategyReview: JsonObject = Field(default_factory=dict)
    decisionFeedback: JsonObject | None = None
    recentProgress: JsonObject | None = None
    pending: list[JsonObject] = Field(default_factory=list)
    alerts: list[JsonObject] = Field(default_factory=list)
    notifications: list[PlayerNotification] = Field(default_factory=list)
    combatReports: list[JsonObject] = Field(default_factory=list)
    rules: JsonObject = Field(default_factory=dict)
    productionPolicy: JsonObject | None = None
    civilizationProfile: JsonObject | None = None
    eraProgress: JsonObject | None = None
    scenario: JsonObject | None = None
    result: JsonObject | None = None
    victory: JsonObject = Field(default_factory=dict)
    cities: list[CitySummary] = Field(default_factory=list)
    units: list[UnitSummary] = Field(default_factory=list)
    researchQueue: list[str] = Field(default_factory=list)
    research: JsonObject | None = None
    culture: JsonObject | None = None
    outputSummary: JsonObject = Field(default_factory=dict)
    greatPeople: list[JsonObject] = Field(default_factory=list)
    researchOptions: list[str] = Field(default_factory=list)
    policyOptions: list[str] = Field(default_factory=list)
    greatPersonOptions: list[str] = Field(default_factory=list)
    freeTechnologies: int = 0
    freePolicies: int = 0
    freeGreatPeople: int = 0
    mayaLimited: bool = False
    idleUnits: int = 0
    economy: JsonObject = Field(default_factory=dict)
    diplomacy: list[JsonObject] = Field(default_factory=list)
    diplomacyOpportunities: JsonObject | None = None
    cityStates: list[JsonObject] = Field(default_factory=list)
    socialAdvisories: list[JsonObject] = Field(default_factory=list)
    turnReview: JsonObject = Field(default_factory=dict)
    changesSincePreviousTurn: JsonObject | None = None
    mapSummary: JsonObject = Field(default_factory=dict)
    exploration: JsonObject = Field(default_factory=dict)
    settlement: JsonObject | None = None
    militaryReadiness: JsonObject | None = None
    wonderOpportunities: list[JsonObject] = Field(default_factory=list)
    constructionEstimateBasis: str | None = None
    evidence: str = "observation:overview"


def wonder_summary(wonder: JsonObject, cities: list[JsonObject]) -> JsonObject:
    """Own all overview wonder estimates, sharing benefits and common blockers."""
    result = {
        key: copy.deepcopy(wonder[key])
        for key in ("name", "kind", "benefits", "greatPersonPoints", "specialistSlots")
        if wonder.get(key)
    }
    costs = {city["productionCost"] for city in wonder["cities"] if "productionCost" in city}
    if len(costs) == 1:
        result["productionCost"] = next(iter(costs))
    available, blocked = [], {}
    estimates = {city["handle"]: city.get("constructionEstimates", {}).get(wonder["name"]) for city in cities}
    for city in wonder["cities"]:
        if city.get("canQueue") or city.get("queued"):
            option = {
                "city": city["city"],
                "turns": city["turns"],
                **{key: city[key] for key in ("productionStored", "productionPerTurn") if key in city},
            }
            estimate = estimates.get(city["city"])
            reasons = (estimate or {}).get("queueUnavailableReasons") or city.get("unavailableReasons")
            if reasons:
                option["queueUnavailableReasons"] = copy.deepcopy(reasons)
            overflow = (estimate or {}).get("production", {}).get("overflow")
            if overflow:
                option["productionOverflow"] = overflow
            if estimate and estimate.get("completion") is not None:
                option["completion"] = {
                    key: value
                    for key, value in completion_summary(estimate["completion"]).items()
                    if key in {"yieldChange", "maintenanceChange", "growth"}
                }
            if estimate and estimate.get("whileBuilding", {}).get("change"):
                option["whileBuildingChange"] = copy.deepcopy(estimate["whileBuilding"]["change"])
            if city.get("queued"):
                option["queued"] = True
            if len(costs) > 1:
                option["productionCost"] = city["productionCost"]
            available.append(option)
        else:
            reasons = tuple(city.get("unavailableReasons", []))
            blocked.setdefault(reasons, []).append(city["city"])
    result["cities"] = available
    if blocked:
        result["unavailableCities"] = [
            {"cities": cities, "reasons": list(reasons)} for reasons, cities in blocked.items()
        ]
    return result


def military_readiness_summary(snapshot: JsonObject) -> JsonObject | None:
    """Include peaceful opportunities from permitted tiles, with explicit bounds."""
    readiness = copy.deepcopy(snapshot.get("militaryReadiness") or {})
    civilization = (snapshot.get("civilizationProfile") or {}).get("name") or snapshot.get("civilization")
    if not civilization:
        return readiness or None
    cities, units = [], []
    for tile in snapshot.get("map", {}).get("tiles", []):
        if not tile.get("visible"):
            continue
        position = tile["position"]
        if tile.get("cityName") and tile.get("owner") not in (None, civilization):
            cities.append(
                {
                    "name": tile["cityName"],
                    "civilization": tile["owner"],
                    "position": copy.deepcopy(position),
                    **{
                        key: tile[source]
                        for key, source in (
                            ("health", "cityHealth"),
                            ("maxHealth", "cityMaxHealth"),
                            ("defensiveStrength", "cityDefensiveStrength"),
                            ("population", "cityPopulation"),
                            ("originalMajorCapital", "cityIsOriginalMajorCapital"),
                        )
                        if tile.get(source) is not None
                    },
                    "inspection": {"tool": "map_region", "arguments": {"center": copy.deepcopy(position), "radius": 3}},
                }
            )
        for unit in tile.get("units", []):
            if unit.get("civilization") not in (None, civilization):
                units.append({**copy.deepcopy(unit), "position": copy.deepcopy(position)})
    cities.sort(key=lambda c: (c["civilization"], c["position"]["x"], c["position"]["y"]))
    units.sort(key=lambda u: (u["civilization"], u["position"]["x"], u["position"]["y"], u.get("name", "")))
    readiness.update(
        visibleForeignCities=cities[:24],
        visibleForeignUnits=units[:48],
        omittedVisibleForeignCities=max(0, len(cities) - 24),
        omittedVisibleForeignUnits=max(0, len(units) - 48),
        foreignObservationLimits="Currently visible foreign cities and units, including peaceful neighbors. Presence does not establish intent or reveal unseen forces. Use map_region for terrain, staging and additional visible details; hex positions are not travel times.",
    )
    return readiness


def overview(snapshot: JsonObject) -> JsonObject:
    """No legal-offer lists, map tiles, yield breakdowns or repeated receipt bodies."""
    value = {
        key: copy.deepcopy(snapshot[key])
        for key in TurnOverview.model_fields
        if key in snapshot and key not in {"cities", "settlement", "research"}
    }
    if snapshot.get("civilizationProfile") is not None:
        value["civilizationProfile"] = civilization_profile_summary(snapshot["civilizationProfile"])
    if readiness := military_readiness_summary(snapshot):
        value["militaryReadiness"] = readiness
    value["wonderOpportunities"] = [
        wonder_summary(wonder, snapshot.get("cities", [])) for wonder in snapshot.get("wonderOpportunities", [])
    ]
    if any(city.get("constructionEstimates") for city in snapshot.get("cities", [])):
        value["constructionEstimateBasis"] = ESTIMATE_BASIS
    value["cities"] = []
    wonders = {wonder["name"] for wonder in snapshot.get("wonderOpportunities", [])}
    for city in snapshot.get("cities", []):
        development = city.get("development") or {}
        value["cities"].append(
            {
                **{key: city[key] for key in ("handle", "name", "constructionQueue", "puppet")},
                **{key: city[key] for key in ("health", "maxHealth", "defensiveStrength") if key in city},
                **{
                    key: development.get(key)
                    for key in ("position", "population", "foodPerTurn", "turnsToGrowth", "turnsToStarvation")
                },
                "constructionChoice": city.get("constructionChoice"),
                "constructionEstimates": overview_estimates(city, wonders),
                "constructionConversion": (city.get("productionConversions") or {}).get(
                    city["constructionQueue"][0] if city["constructionQueue"] else ""
                ),
                "inResistance": development.get("inResistance", False),
                **{
                    key: copy.deepcopy(development[key])
                    for key in (
                        "borderExpansion",
                        "resistanceTurns",
                        "beingRazed",
                        "demandedResource",
                        "weLoveTheKingTurns",
                    )
                    if key in development
                },
                "attackOptionCount": len(city.get("attacks", [])),
                "attacksRemaining": city["attacksRemaining"],
                "yields": {y["stat"]: y["amount"] for y in development.get("yields", [])},
                "citizens": citizens_summary(city),
            }
        )
    value["units"] = []
    for unit in snapshot.get("map", {}).get("units", []):
        combat = unit.get("combat") or {}
        value["units"].append(
            {
                **{
                    key: unit[key]
                    for key in (
                        "handle",
                        "name",
                        "position",
                        "health",
                        "movement",
                        "military",
                        "idle",
                        "movementSupported",
                        "canFoundCity",
                    )
                },
                "movementOrder": unit.get("movementOrder"),
                "escort": unit.get("escort"),
                "protection": copy.deepcopy(unit.get("protection")),
                "statuses": copy.deepcopy(unit.get("statuses", [])),
                **{key: combat.get(key) for key in ("strength", "rangedStrength", "role") if unit["military"]},
                "settlementStatus": "CAN_FOUND_NOW"
                if unit["canFoundCity"]
                else (unit.get("settlement") or {}).get("siteStatus"),
                "missions": [m["mission"] for m in (unit.get("air") or {}).get("missions", []) if m.get("available")],
                "transported": (unit.get("air") or {}).get("transported", False),
                "carriedUnits": (unit.get("air") or {}).get("carriedUnits", []),
                "interceptionReady": ((unit.get("air") or {}).get("interception") or {}).get("ready", False),
                "canCreateWaterImprovement": (unit.get("waterImprovement") or {}).get("available", False),
                "legalDestinations": len(unit.get("moves", [])),
                "attackOptionCount": len(combat.get("attacks", [])),
                "attacksRemaining": combat["attacksRemaining"],
                "order": (combat.get("order") or {}).get("action"),
                "automated": (combat.get("order") or {}).get("automated", False),
                "workerJob": (unit.get("worker") or {}).get("currentJob"),
                "workerJobTurnsRemaining": (unit.get("worker") or {}).get("remainingTurns"),
                "promotions": promotion_summary(combat.get("promotions")),
                "upgrades": [
                    {
                        key: option[key]
                        for key in ("targetName", "goldCost", "additionalResourceRequirements", "available", "reasons")
                    }
                    for option in (unit.get("economy") or {}).get("upgrades", [])
                ],
                "availableGreatPersonAbilityCount": sum(
                    bool(a.get("available")) for a in (unit.get("greatPerson") or {}).get("abilities", [])
                ),
                "canGiftUnit": ((unit.get("gift") or {}).get("action") or {}).get("available", False),
                "canAssembleSpaceshipPart": (unit.get("spaceshipAssembly") or {}).get("available", False),
            }
        )
    value["economy"] = {
        key: item
        for key, item in (snapshot.get("economy") or {}).items()
        if key
        in {
            "treasury",
            "committedGold",
            "availableGold",
            "goldPerTurn",
            "happiness",
            "unitMaintenance",
            "unitSupply",
            "goldenAge",
            "development",
        }
    }
    value["economy"]["resources"] = resource_summary(snapshot.get("economy") or {})
    value["economy"]["happinessBudget"] = {
        "sources": copy.deepcopy((snapshot.get("economy") or {}).get("happinessBreakdown", [])),
        "existingCityBuildings": happiness_buildings(snapshot),
        "limits": "Building gains are separate current-population comparisons, not an additive budget. New luxuries require acquisition/connection; duplicate luxuries do not ordinarily add happiness. See settlement for new-city costs and potential resources.",
    }
    value["research"] = research_summary(snapshot)
    value["outputSummary"] = {
        "asOfTurn": snapshot["turn"],
        "totals": review_position(snapshot)["totals"],
        "basis": "Owned current rates, not forecasts. Production includes current construction modifiers. Science from production is shown separately; the remainder can still include temporary bonuses. Unknown rates are null.",
    }
    value["exploration"] = exploration_summary(snapshot)
    value["diplomacy"] = [
        {
            key: item
            for key, item in relation.items()
            if key in {"civilizationId", "civilizationName", "status", "atWar", "peaceTreatyTurns", "native"}
        }
        for relation in snapshot.get("diplomacy", {}).get("knownCivilizations", [])
    ]
    value["cityStates"] = [
        {
            key: item
            for key, item in state.items()
            if key
            in {
                "civilizationId",
                "name",
                "type",
                "atWar",
                "defeated",
                "relationship",
                "influence",
                "decayPerTurn",
                "turnsToRelationshipChange",
                "ally",
                "protecting",
                "quests",
                "bonuses",
                "resources",
                "allyInfluence",
                "uniqueUnit",
            }
        }
        for state in snapshot.get("diplomacy", {}).get("cityStates", [])
    ]
    review = snapshot.get("turnReview") or {}
    value["turnReview"] = {"confirmedActions": len(review.get("actions", []))}
    value["mapSummary"] = {
        "exploredTiles": len(snapshot.get("map", {}).get("tiles", [])),
        "frontierTiles": len(snapshot.get("map", {}).get("frontier", [])),
    }
    if snapshot.get("settlement") is not None:
        value["settlement"] = settlement_summary(snapshot["settlement"])
    add_overview_commands(value)
    result = TurnOverview.model_validate(value).model_dump(exclude_none=True)
    # Roster deltas replace entire rows. Omitted optional capabilities therefore
    # disappear on update; health/movement/idle and meaningful zero counts stay.
    optional = {
        "canFoundCity",
        "automated",
        "availableGreatPersonAbilityCount",
        "canAssembleSpaceshipPart",
        "canGiftUnit",
        "canCreateWaterImprovement",
        "missions",
        "transported",
        "carriedUnits",
        "interceptionReady",
        "upgrades",
        "statuses",
    }
    for unit in result["units"]:
        for key in optional:
            if key == "canFoundCity" and "settlementStatus" in unit:
                continue
            if not unit.get(key):
                unit.pop(key, None)
    return result


def civilization_profile_summary(profile: JsonObject) -> JsonObject:
    """Keep mechanical facts; omit empty optional fields without dropping false conditions or zero comparisons."""

    def compact(value):
        if isinstance(value, dict):
            if value.keys() == {"ordinary", "unique"}:
                return copy.deepcopy(value)
            return {
                key: compact(item)
                for key, item in value.items()
                if key == "missingTechnologies" or item is not None and item != [] and item != {}
            }
        if isinstance(value, list):
            return [compact(item) for item in value]
        return value

    return compact(profile)


def promotion_summary(status: JsonObject | None) -> JsonObject | None:
    if not status:
        return None
    acquired = [choice["name"] for choice in status.get("choices", []) if choice.get("acquired")]
    choices = [choice for choice in status.get("choices", []) if choice.get("available")]
    if not (acquired or choices or status.get("experience") or status.get("earnedPromotions")):
        return None
    result = {
        key: status[key]
        for key in (
            "experience",
            "experienceForNextPromotion",
            "earnedPromotions",
            "totalExperience",
            "barbarianExperienceLimit",
            "canEarnExperienceFromBarbarians",
        )
        if key in status
    }
    result["acquired"] = acquired
    if choices:
        result["choices"] = [
            {
                key: copy.deepcopy(value)
                for key, value in choice.items()
                if key not in {"acquired", "unavailableReasons"}
            }
            for choice in choices
        ]
        result["recovery"] = copy.deepcopy(status.get("recovery"))
    return result


def native_observation(value: Any) -> Any:
    """Keep social reminders out of native-state evidence and freshness checks.

    The displayed reminders repeat the player's own terms. The communication
    ledger remains the citeable source for those social statements.
    """
    if isinstance(value, dict):
        return {key: native_observation(part) for key, part in value.items() if key != "socialAdvisories"}
    if isinstance(value, list):
        return [native_observation(part) for part in value]
    return value


def detail_scope(query: DetailQuery) -> str:
    canonical = json.dumps(query.model_dump(), sort_keys=True, separators=(",", ":"))
    return "detail/" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def map_region_inputs(snapshot: JsonObject, args: JsonObject):
    view = snapshot.get("map", {})
    center, radius, wrap = args["center"], args.get("radius", 2), view.get("wrapOffset")
    tiles, frontier = view.get("tiles", []), view.get("frontier", [])
    available = any(tile["position"] == center for tile in tiles) or center in frontier
    return {
        "available": available,
        "tiles": [tile for tile in tiles if region_distance(tile["position"], center, wrap) <= radius]
        if available
        else [],
        "frontier": [p for p in frontier if region_distance(p, center, wrap) <= radius] if available else [],
    }


def detail_inputs(snapshot: JsonObject, query: DetailQuery):
    """Conservative dependencies for refreshing a cached projection, excluding clocks."""
    name, args = query.tool, query.arguments
    if name in {"unit_details", "city_details"}:
        entities = snapshot.get("map", {}).get("units", []) if name == "unit_details" else snapshot.get("cities", [])
        entity = next((e for e in entities if e["handle"] == args["target"]), None)
        return project_entity(entity, name, args.get("sections", ["all"])) if entity is not None else None
    if name == "construction_details":
        return project_detail(snapshot, query)["data"]
    if name in {"movement_preview", "mission_preview", "player_inspect"}:
        return snapshot.get("revision")
    if name == "map_region":
        return map_region_inputs(snapshot, args)
    if args.get("section") == "map_index":
        return {key: snapshot.get("map", {}).get(key) for key in ("tiles", "frontier", "wrapOffset")}
    section = args["section"]
    fields = {
        "turn_history": ("turnReview", "previousTurnReview"),
        "research": ("research", "researchQueue", "researchOptions", "freeTechnologies"),
        "policies": ("culture", "policyOptions", "freePolicies"),
    }.get(section, (section,))
    return {key: snapshot.get(key) for key in fields}


def detail_fingerprint(snapshot: JsonObject, query: DetailQuery) -> str:
    inputs = json.dumps(detail_inputs(snapshot, query), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(inputs.encode()).hexdigest()


def project_detail(snapshot: JsonObject, query: DetailQuery) -> JsonObject:
    args, name = query.arguments, query.tool
    data: JsonObject = {}
    available = True
    if name in {"unit_details", "city_details", "construction_details"}:
        entities = snapshot.get("map", {}).get("units", []) if name == "unit_details" else snapshot.get("cities", [])
        entity = next((e for e in entities if e["handle"] == args["target"]), None)
        available = entity is not None
        if entity is not None:
            if name != "construction_details":
                data = {"entity": project_entity(entity, name, args.get("sections", ["all"]))}
                if name == "unit_details":
                    add_unit_commands(data["entity"])
                    if data["entity"].get("worker") is not None or data["entity"].get("waterImprovement") is not None:
                        data["workScope"] = {
                            "kind": "CURRENT_TILE",
                            "position": entity["position"],
                            "description": "Work options apply only at this unit's current tile; they do not enumerate jobs elsewhere.",
                            "surroundings": {
                                "tool": "map_region",
                                "arguments": {"center": entity["position"], "radius": 3},
                            },
                        }
                else:
                    add_city_commands(data["entity"])
            else:
                purchases = {p["name"]: p for p in (entity.get("purchases") or {}).get("constructions", [])}
                progress = (entity.get("development") or {}).get("constructions", [])
                options = entity.get("constructionOptions", [])
                names = args.get("names", [])
                estimates = entity.get("constructionEstimates", {})
                catalogue = sorted(set(options) | purchases.keys() | {p["name"] for p in progress})
                known_names = snapshot["constructionCatalogue"]
                unknown = [n for n in names if n not in known_names]
                available = not unknown

                def queue_reasons(n):
                    policy = snapshot.get("productionPolicy") or {}
                    if n in policy.get("blockedConversions", []):
                        return ["Production conversion is disabled until " + policy["conversionUnlockEra"]]
                    if n in options:
                        return []
                    reasons = list(estimates.get(n, {}).get("queueUnavailableReasons", []))
                    if entity["puppet"] and "Puppet cities control their own production" not in reasons:
                        reasons.append("Puppet cities control their own production")
                    return reasons or ["Not in this city's current production menu; native requirements may be unmet."]

                data = {
                    "city": entity["handle"],
                    "name": entity["name"],
                    "queue": entity["constructionQueue"],
                    "constructions": [
                        {
                            "name": n,
                            "canQueue": n in options,
                            "purchase": (
                                {
                                    key: value
                                    for key, value in purchases[n].items()
                                    if key not in {"requiredTechnologies", "resourceRequirements"}
                                    or key not in estimates.get(n, {})
                                }
                                if n in purchases
                                else None
                            )
                            if names
                            else (
                                {k: v for k, v in purchases[n].items() if k in {"goldCost", "available"}}
                                if n in purchases
                                else None
                            ),
                            "progress": [
                                queue_progress(p, estimates.get(n, {}).get("production"))
                                for p in progress
                                if p["name"] == n
                            ],
                            **(construction_estimate(estimates[n], detailed=bool(names)) if n in estimates else {}),
                            **(
                                {
                                    "investmentComparison": investment_comparison(
                                        {**estimates[n], "queueUnavailableReasons": queue_reasons(n)},
                                        purchases.get(n),
                                        args["horizonTurns"],
                                        progress=[p for p in progress if p["name"] == n],
                                        can_queue=n in options,
                                    )
                                }
                                if n in estimates and args.get("horizonTurns") is not None
                                else {}
                            ),
                            "queueUnavailableReasons": queue_reasons(n),
                            **(
                                {"conversion": entity["productionConversions"][n]}
                                if n in entity.get("productionConversions", {})
                                else {}
                            ),
                        }
                        for n in (names or catalogue)
                        if n in known_names
                    ],
                    "detailLevel": "named" if names else "catalogue",
                }
                if unknown:
                    data["unknownNames"] = [
                        {
                            "name": n,
                            "code": "UNKNOWN_CONSTRUCTION",
                            "suggestions": difflib.get_close_matches(n, known_names, n=3),
                        }
                        for n in unknown
                    ]
                    data["discovery"] = {"tool": "construction_details", "arguments": {"target": args["target"]}}
                if not names:
                    data["knownNames"] = known_names
                if estimates:
                    data["estimateBasis"] = ESTIMATE_BASIS
                data = copy.deepcopy(data)
                add_construction_commands(data)
    elif name in {"movement_preview", "mission_preview", "player_inspect"}:
        # Native pathfinding cannot be reconstructed from a cached turn projection.
        available = False
        data = {"requiresRefresh": True}
    elif name == "map_region":
        center, radius = args["center"], args.get("radius", 2)
        # Use an observed position: arbitrary coordinates must not imply discovered terrain.
        region = map_region_inputs(snapshot, args)
        available = region["available"]
        if available:
            tiles = region["tiles"]
            data = {
                "center": center,
                "radius": radius,
                **(compact_tiles(tiles) if args.get("format", "compact") == "compact" else {"tiles": tiles}),
                "frontier": region["frontier"],
            }
    elif name == "player_details":
        section = args["section"]
        if section == "map_index":
            data = {
                "tiles": [{k: t[k] for k in ("position", "visible")} for t in snapshot.get("map", {}).get("tiles", [])],
                "frontier": snapshot.get("map", {}).get("frontier", []),
                "wrapOffset": snapshot.get("map", {}).get("wrapOffset"),
            }
        else:
            fields = {
                "turn_history": ("turnReview", "previousTurnReview"),
                "research": ("research", "researchQueue", "researchOptions", "freeTechnologies"),
                "policies": ("culture", "policyOptions", "freePolicies"),
            }.get(section, (section,))
            data = {key: snapshot.get(key) for key in fields}
    return DetailResult(
        evidence="observation:" + detail_scope(query),
        turn=snapshot["turn"],
        query=query,
        available=available,
        data=copy.deepcopy(data),
    ).model_dump(exclude_unset=True)


def required_detail(command: JsonObject) -> DetailQuery | None:
    """Exact native previews remain required; routine detail reads are optional."""
    kind, target = command["kind"], command.get("target")
    if kind in {"REBASE_UNIT", "PARADROP", "AIR_SWEEP", "NUCLEAR_STRIKE"}:
        return DetailQuery(
            tool="mission_preview",
            arguments={
                "target": target,
                "mission": "REBASE" if kind == "REBASE_UNIT" else kind,
                "destination": command["destination"],
            },
        )
    if kind == "SET_UNIT_DESTINATION":
        return DetailQuery(tool="movement_preview", arguments={"target": target, "destination": command["destination"]})
    if kind == "CONNECT_ROAD":
        return DetailQuery(
            tool="player_inspect",
            arguments={"subject": "ROAD", "target": target, "destination": command["destination"]},
        )
    return None
