"""Model-facing projections of the host's already permission-filtered snapshot.

Detail reads reuse that snapshot. They never query hidden engine state or count as
reading any information outside the returned projection.
"""

import copy
import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import Field

from civ_agent.action_templates import (
    add_city_commands,
    add_construction_commands,
    add_overview_commands,
    add_unit_commands,
)
from civ_agent.entity_sections import (
    CITY_COMMAND_SECTIONS,
    UNIT_COMMAND_SECTIONS,
    CityDetails,
    EntityDetails,
    UnitDetails,
    project_entity,
)
from civ_agent.game_commands import Position
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel


class ConstructionDetails(EntityDetails):
    names: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)


class MapRegion(BoundaryModel):
    center: Position
    radius: Annotated[int, Field(ge=0, le=4)] = 2


class PlayerDetails(BoundaryModel):
    section: Literal["economy", "diplomacy", "research", "policies", "turn_history", "map_index"]


class DetailQuery(BoundaryModel):
    tool: Literal[
        "city_details",
        "unit_details",
        "construction_details",
        "map_region",
        "player_details",
        "movement_preview",
        "mission_preview",
    ]
    arguments: JsonObject


class DetailResult(BoundaryModel):
    evidence: str
    turn: int
    query: DetailQuery
    available: bool
    data: JsonObject


DETAIL_MODELS: dict[str, type[BoundaryModel]] = {
    "city_details": CityDetails,
    "unit_details": UnitDetails,
    "construction_details": ConstructionDetails,
    "map_region": MapRegion,
    "player_details": PlayerDetails,
}


class CitySummary(BoundaryModel):
    handle: str
    name: str
    position: Position | None = None
    population: int | None = None
    foodPerTurn: int | None = None
    turnsToGrowth: int | None = None
    turnsToStarvation: int | None = None
    constructionQueue: list[str]
    constructionTurns: int | None = None
    puppet: bool
    inResistance: bool
    availableAttacks: int


class UnitSummary(BoundaryModel):
    handle: str
    name: str
    position: Position
    health: int
    movement: str
    military: bool
    idle: bool
    movementSupported: bool
    legalDestinations: int
    availableAttacks: int
    canFoundCity: bool
    order: str | None = None
    workerJob: str | None = None
    availablePromotions: int
    greatPersonAbilities: int
    canAssembleSpaceshipPart: bool = False
    canGiftToCityState: bool = False
    movementOrder: JsonObject | None = None
    canCreateWaterImprovement: bool = False
    missions: list[str] = Field(default_factory=list)
    transported: bool = False
    carriedUnits: list[str] = Field(default_factory=list)
    interceptionReady: bool = False
    inspections: JsonObject | None = None


class PlayerNotification(BoundaryModel):
    handle: str
    text: str
    category: str
    icons: list[str]
    locations: list[Position]
    canDismiss: bool


class TurnOverview(BoundaryModel):
    stateId: str | None = None
    turn: int
    phase: str
    canEndTurn: bool
    endTurn: JsonObject | None = None
    tacticalReview: JsonObject = Field(default_factory=dict)
    strategyReview: JsonObject = Field(default_factory=dict)
    pending: list[JsonObject] = Field(default_factory=list)
    alerts: list[JsonObject] = Field(default_factory=list)
    notifications: list[PlayerNotification] = Field(default_factory=list)
    combatReports: list[JsonObject] = Field(default_factory=list)
    rules: JsonObject = Field(default_factory=dict)
    scenario: JsonObject | None = None
    result: JsonObject | None = None
    victory: JsonObject = Field(default_factory=dict)
    negotiation: JsonObject = Field(default_factory=dict)
    cities: list[CitySummary] = Field(default_factory=list)
    units: list[UnitSummary] = Field(default_factory=list)
    researchQueue: list[str] = Field(default_factory=list)
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
    cityStates: list[JsonObject] = Field(default_factory=list)
    communication: JsonObject = Field(default_factory=dict)
    socialAdvisories: list[JsonObject] = Field(default_factory=list)
    turnReview: JsonObject = Field(default_factory=dict)
    mapSummary: JsonObject = Field(default_factory=dict)
    evidence: str = "observation:overview"


def overview(snapshot: JsonObject) -> JsonObject:
    """No legal-offer lists, map tiles, yield breakdowns or repeated receipt bodies."""
    value = {key: copy.deepcopy(snapshot[key]) for key in TurnOverview.model_fields if key in snapshot}
    value["cities"] = []
    for city in snapshot.get("cities", []):
        development = city.get("development") or {}
        progress = development.get("constructions", [])
        value["cities"].append(
            {
                **{key: city[key] for key in ("handle", "name", "constructionQueue", "puppet")},
                **{
                    key: development.get(key)
                    for key in ("position", "population", "foodPerTurn", "turnsToGrowth", "turnsToStarvation")
                },
                "constructionTurns": progress[0].get("turns") if progress else None,
                "inResistance": development.get("inResistance", False),
                "availableAttacks": len(city.get("attacks", [])),
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
                "missions": [m["mission"] for m in (unit.get("air") or {}).get("missions", []) if m.get("available")],
                "transported": (unit.get("air") or {}).get("transported", False),
                "carriedUnits": (unit.get("air") or {}).get("carriedUnits", []),
                "interceptionReady": ((unit.get("air") or {}).get("interception") or {}).get("ready", False),
                "canCreateWaterImprovement": (unit.get("waterImprovement") or {}).get("available", False),
                "legalDestinations": len(unit.get("moves", [])),
                "availableAttacks": len(combat.get("attacks", [])),
                "order": (combat.get("order") or {}).get("action"),
                "workerJob": (unit.get("worker") or {}).get("currentJob"),
                "availablePromotions": sum(
                    p.get("available", False) for p in (combat.get("promotions") or {}).get("choices", [])
                ),
                "greatPersonAbilities": len((unit.get("greatPerson") or {}).get("abilities", [])),
                "canGiftToCityState": ((unit.get("cityStateGift") or {}).get("action") or {}).get("available", False),
                "canAssembleSpaceshipPart": (unit.get("spaceshipAssembly") or {}).get("available", False),
            }
        )
    value["economy"] = {
        key: item
        for key, item in (snapshot.get("economy") or {}).items()
        if key in {"treasury", "goldPerTurn", "happiness", "unitMaintenance", "unitSupply", "goldenAge"}
    }
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
            }
        }
        for state in snapshot.get("diplomacy", {}).get("cityStates", [])
    ]
    social = snapshot.get("communication", {})
    value["communication"] = {
        "commitments": [
            {
                key: item
                for key, item in offer.items()
                if key in {"proposal", "participants", "kind", "effective", "expiresTurn"}
            }
            for offer in social.get("offers", [])
        ],
        "trades": social.get("trades", []),
    }
    review = snapshot.get("turnReview") or {}
    value["turnReview"] = {"units": review.get("units", []), "confirmedActions": len(review.get("actions", []))}
    value["mapSummary"] = {
        "exploredTiles": len(snapshot.get("map", {}).get("tiles", [])),
        "frontierTiles": len(snapshot.get("map", {}).get("frontier", [])),
    }
    add_overview_commands(value)
    return TurnOverview.model_validate(value).model_dump(exclude_none=True)


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


def region_distance(a: JsonObject, b: JsonObject, wrap: JsonObject | None) -> int:
    def distance(offset: int):
        dx = a["x"] - b["x"] + offset * (wrap["x"] if wrap else 0)
        dy = a["y"] - b["y"] + offset * (wrap["y"] if wrap else 0)
        return max(abs(dx), abs(dy), abs(dx - dy))

    return min(distance(offset) for offset in (-1, 0, 1)) if wrap else distance(0)


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
                else:
                    add_city_commands(data["entity"])
            else:
                purchases = {p["name"]: p for p in (entity.get("purchases") or {}).get("constructions", [])}
                progress = (entity.get("development") or {}).get("constructions", [])
                options = entity.get("constructionOptions", [])
                names = args.get("names", [])
                catalogue = sorted(set(options) | purchases.keys() | {p["name"] for p in progress})
                data = {
                    "city": entity["handle"],
                    "name": entity["name"],
                    "queue": entity["constructionQueue"],
                    "constructions": [
                        {
                            "name": n,
                            "canQueue": n in options,
                            "purchase": purchases.get(n)
                            if names
                            else (
                                {k: v for k, v in purchases[n].items() if k in {"goldCost", "available"}}
                                if n in purchases
                                else None
                            ),
                            "progress": [p for p in progress if p["name"] == n],
                        }
                        for n in (names or catalogue)
                    ],
                    "descriptionsIncluded": bool(names),
                }
                data = copy.deepcopy(data)
                add_construction_commands(data)
    elif name in {"movement_preview", "mission_preview"}:
        # Native pathfinding cannot be reconstructed from a cached turn projection.
        available = False
        data = {"requiresRefresh": True}
    elif name == "map_region":
        map_view = snapshot.get("map", {})
        center, radius = args["center"], args.get("radius", 2)
        wrap = map_view.get("wrapOffset")
        # Use an observed position: arbitrary coordinates must not imply discovered terrain.
        positions = [t["position"] for t in map_view.get("tiles", [])] + map_view.get("frontier", [])
        available = center in positions
        if available:
            data = {
                "center": center,
                "radius": radius,
                "tiles": [
                    t for t in map_view.get("tiles", []) if region_distance(t["position"], center, wrap) <= radius
                ],
                "frontier": [p for p in map_view.get("frontier", []) if region_distance(p, center, wrap) <= radius],
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
                "policies": ("policyOptions", "freePolicies"),
            }.get(section, (section,))
            data = {key: snapshot.get(key) for key in fields}
    return DetailResult(
        evidence="observation:" + detail_scope(query),
        turn=snapshot["turn"],
        query=query,
        available=available,
        data=copy.deepcopy(data),
    ).model_dump()


def required_detail(command: JsonObject) -> DetailQuery | None:
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
    if kind in {"DECLARE_WAR", "CITY_STATE_ACTION"}:
        return DetailQuery(tool="player_details", arguments={"section": "diplomacy"})
    if kind in {"ACKNOWLEDGE_ALERT", "RESPOND_FRIENDSHIP", "RESOLVE_CAPTURED_CITY"} or target is None:
        return None  # Full decision content is always in the overview.
    if kind in {"QUEUE_CONSTRUCTION", "BUY_CONSTRUCTION"}:
        return DetailQuery(tool="construction_details", arguments={"target": target, "names": [command["selection"]]})
    unit = kind in UNIT_COMMAND_SECTIONS
    section = (UNIT_COMMAND_SECTIONS if unit else CITY_COMMAND_SECTIONS).get(kind, "all")
    return DetailQuery(
        tool="unit_details" if unit else "city_details", arguments={"target": target, "sections": [section]}
    )


def detail_satisfies(read: DetailQuery, required: DetailQuery) -> bool:
    if required.tool == "construction_details" and read.arguments.get("target") == required.arguments["target"]:
        sections = set(read.arguments.get("sections", ["all"]))
        return (read.tool == "city_details" and ("all" in sections or {"production", "purchases"} <= sections)) or (
            read.tool == "construction_details"
            and set(required.arguments["names"]) <= set(read.arguments.get("names", []))
        )
    if read.tool == required.tool and read.tool in {"unit_details", "city_details"}:
        sections = set(read.arguments.get("sections", ["all"]))
        return read.arguments.get("target") == required.arguments.get("target") and (
            "all" in sections or set(required.arguments.get("sections", ["all"])) <= sections
        )
    return read == required
