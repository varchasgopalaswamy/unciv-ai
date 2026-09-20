"""Explicit projections of already visible entities; unknown fields stay in full reads."""

import copy
from typing import Annotated, Literal

from pydantic import Field, field_validator

from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel

UnitSection = Literal[
    "all", "movement", "combat", "orders", "promotions", "work", "settlement", "economy", "abilities", "air"
]
CitySection = Literal["all", "status", "production", "citizens", "economy", "purchases", "combat"]


class EntityDetails(BoundaryModel):
    target: Annotated[str, Field(min_length=1, max_length=128)]


class UnitDetails(EntityDetails):
    sections: Annotated[list[UnitSection], Field(min_length=1, max_length=10)] = Field(default_factory=lambda: ["all"])

    @field_validator("sections")
    @classmethod
    def canonical_sections(cls, value):
        return ["all"] if "all" in value else sorted(set(value))


class CityDetails(EntityDetails):
    sections: Annotated[list[CitySection], Field(min_length=1, max_length=7)] = Field(default_factory=lambda: ["all"])

    @field_validator("sections")
    @classmethod
    def canonical_sections(cls, value):
        return ["all"] if "all" in value else sorted(set(value))


UNIT_FIELDS = {
    "movement": ("moves", "movementOrder", "nearbyHostiles"),
    "combat": (
        "combat.supported",
        "combat.strength",
        "combat.rangedStrength",
        "combat.range",
        "combat.attacksRemaining",
        "combat.attacks",
        "nearbyHostiles",
    ),
    "orders": ("combat.order", "combat.orders"),
    "promotions": ("combat.promotions",),
    "work": ("worker", "waterImprovement"),
    "settlement": ("canFoundCity", "settlementPromises", "nearbyHostiles"),
    "economy": ("economy",),
    "abilities": ("greatPerson", "spaceshipAssembly", "cityStateGift"),
    "air": ("air",),
}
CITY_FIELDS = {
    "status": ("captureOptions",),
    "production": ("constructionQueue", "constructionOptions", "development.constructions"),
    "citizens": (
        "development.unassignedPopulation",
        "development.foodStored",
        "development.foodToNextPopulation",
        "development.foodPerTurn",
        "development.turnsToGrowth",
        "development.turnsToStarvation",
        "development.growing",
        "development.starving",
        "development.avoidGrowth",
        "development.focus",
        "development.tiles",
        "management.focusOptions",
        "management.avoidGrowth",
        "management.manualSpecialists",
        "management.specialistControl",
        "management.resetCitizens",
        "management.specialists",
        "management.greatPeople",
        "management.sharedTiles",
    ),
    "economy": (
        "development.yields",
        "development.baseYieldBreakdown",
        "development.percentYieldBreakdown",
        "development.finalYieldBreakdown",
        "development.builtBuildings",
        "management.buildingSales",
    ),
    "purchases": ("purchases",),
    "combat": ("attacks",),
}
UNIT_IDENTITY = (
    "handle",
    "name",
    "displayName",
    "position",
    "health",
    "movement",
    "military",
    "idle",
    "movementSupported",
)
CITY_IDENTITY = (
    "handle",
    "name",
    "puppet",
    "development.position",
    "development.population",
    "development.inResistance",
)


def project_entity(entity: JsonObject, tool: str, sections: list[str]) -> JsonObject:
    if "all" in sections:
        return copy.deepcopy(entity)
    fields = UNIT_FIELDS if tool == "unit_details" else CITY_FIELDS
    paths = list(UNIT_IDENTITY if tool == "unit_details" else CITY_IDENTITY)
    paths.extend(path for section in sections for path in fields[section])
    projected: JsonObject = {}
    for path in paths:
        source, target = entity, projected
        parts = path.split(".")
        for key in parts[:-1]:
            if not isinstance(source.get(key), dict):
                break
            source = source[key]
            target = target.setdefault(key, {})
        else:
            if parts[-1] in source:
                target[parts[-1]] = copy.deepcopy(source[parts[-1]])
    return projected


UNIT_COMMAND_SECTIONS = {
    "MOVE_UNIT": "movement",
    "CANCEL_UNIT_MOVEMENT": "movement",
    "ATTACK": "combat",
    "SET_UNIT_ORDER": "orders",
    "PROMOTE_UNIT": "promotions",
    "START_WORKER_JOB": "work",
    "STOP_WORKER_JOB": "work",
    "CREATE_WATER_IMPROVEMENT": "work",
    "FOUND_CITY": "settlement",
    "UPGRADE_UNIT": "economy",
    "PILLAGE": "economy",
    "DISBAND_UNIT": "economy",
    "ACTIVATE_GREAT_PERSON": "abilities",
    "ADD_SPACESHIP_PART": "abilities",
    "GIFT_UNIT": "abilities",
}
CITY_COMMAND_SECTIONS = {
    "REMOVE_CONSTRUCTION": "production",
    "MOVE_CONSTRUCTION": "production",
    "BUY_TILE": "purchases",
    "SELL_BUILDING": "economy",
    "CITY_ATTACK": "combat",
    "MANAGE_CAPTURED_CITY": "status",
    "RENAME_CITY": "status",
    **dict.fromkeys(
        (
            "SET_CITY_FOCUS",
            "SET_AVOID_GROWTH",
            "SET_MANUAL_SPECIALISTS",
            "RESET_CITIZENS",
            "SET_WORKED_TILE",
            "SET_TILE_LOCKED",
            "REASSIGN_CITY_TILE",
            "ASSIGN_SPECIALIST",
            "UNASSIGN_SPECIALIST",
        ),
        "citizens",
    ),
}
