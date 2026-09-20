"""Agent-facing command shapes. Legality and execution remain the host's responsibility."""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, ValidationError, field_validator

from civ_agent.memory_records import Intent
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, NonnegativeInt

Name = Annotated[str, Field(min_length=1)]


class Position(BoundaryModel):
    x: int
    y: int


class Command(BoundaryModel):
    intent: Intent | None = None


class ResearchQueue(Command):
    kind: Literal["SET_RESEARCH_QUEUE"]
    queue: Annotated[list[Name], Field(min_length=1, max_length=1024)]


class Choice(Command):
    kind: Literal["CHOOSE_FREE_TECHNOLOGY", "ADOPT_POLICY", "CHOOSE_FREE_GREAT_PERSON", "CAST_DIPLOMATIC_VOTE"]
    selection: Name


class TargetChoice(Command):
    kind: Literal[
        "QUEUE_CONSTRUCTION",
        "RENAME_CITY",
        "START_WORKER_JOB",
        "CREATE_WATER_IMPROVEMENT",
        "SET_UNIT_ORDER",
        "PROMOTE_UNIT",
        "RESOLVE_CAPTURED_CITY",
        "RESPOND_FRIENDSHIP",
        "MANAGE_CAPTURED_CITY",
        "SET_CITY_FOCUS",
        "ASSIGN_SPECIALIST",
        "UNASSIGN_SPECIALIST",
        "ACTIVATE_GREAT_PERSON",
    ]
    target: Name
    selection: Name


class EconomicChoice(Command):
    kind: Literal["BUY_CONSTRUCTION", "UPGRADE_UNIT", "SELL_BUILDING", "CITY_STATE_ACTION"]
    target: Name
    selection: Name
    confirmAction: bool = False


class TargetCommand(Command):
    kind: Literal[
        "ACKNOWLEDGE_ALERT",
        "STOP_WORKER_JOB",
        "CANCEL_UNIT_MOVEMENT",
        "RESET_CITIZENS",
        "ADD_SPACESHIP_PART",
        "GIFT_UNIT",
    ]
    target: Name


class EconomicCommand(Command):
    kind: Literal["PILLAGE", "DISBAND_UNIT"]
    target: Name
    confirmAction: bool = False


class MoveCommand(Command):
    kind: Literal["MOVE_UNIT", "SET_UNIT_DESTINATION", "REASSIGN_CITY_TILE", "REBASE_UNIT", "PARADROP", "AIR_SWEEP"]
    target: Name
    destination: Position


class NuclearStrike(Command):
    kind: Literal["NUCLEAR_STRIKE"]
    target: Name
    destination: Position
    confirmAction: bool = False


class CityAttack(Command):
    kind: Literal["CITY_ATTACK"]
    target: Name
    destination: Position


class UnitAttack(Command):
    kind: Literal["ATTACK"]
    target: Name
    destination: Position
    attackFrom: Position


class BuyTile(Command):
    kind: Literal["BUY_TILE"]
    target: Name
    destination: Position
    confirmAction: bool = False


class WorkTile(Command):
    kind: Literal["SET_WORKED_TILE"]
    target: Name
    destination: Position
    worked: bool


class LockTile(Command):
    kind: Literal["SET_TILE_LOCKED"]
    target: Name
    destination: Position
    locked: bool


class Toggle(Command):
    kind: Literal["SET_AVOID_GROWTH", "SET_MANUAL_SPECIALISTS"]
    target: Name
    enabled: bool


class RemoveConstruction(Command):
    kind: Literal["REMOVE_CONSTRUCTION"]
    target: Name
    index: NonnegativeInt


class MoveConstruction(Command):
    kind: Literal["MOVE_CONSTRUCTION"]
    target: Name
    index: NonnegativeInt
    toIndex: NonnegativeInt


class FoundCity(Command):
    kind: Literal["FOUND_CITY"]
    target: Name
    confirmBreakPromise: bool = False


class DeclareWar(Command):
    kind: Literal["DECLARE_WAR"]
    target: Name
    confirmWar: Literal[True]

    @field_validator("confirmWar", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        # Literal[True] alone also accepts integer 1 in Pydantic.
        if value is not True:
            raise ValueError("Explicit boolean confirmation required")
        return value


class EndTurn(Command):
    kind: Literal["END_TURN"]
    deferReason: Annotated[str, Field(min_length=1, max_length=500)] = ""


class DismissPolicy(Command):
    kind: Literal["DISMISS_POLICY_PICKER", "ABSTAIN_DIPLOMATIC_VOTE", "ACKNOWLEDGE_DIPLOMATIC_RESULTS"]


class DismissNotifications(Command):
    kind: Literal["DISMISS_NOTIFICATIONS"]
    targets: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]],
        Field(min_length=1, max_length=256, json_schema_extra={"uniqueItems": True}),
    ]

    @field_validator("targets")
    @classmethod
    def unique_targets(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Choose each notification only once")
        return value


PlayerCommand = Annotated[
    ResearchQueue
    | Choice
    | TargetChoice
    | EconomicChoice
    | TargetCommand
    | EconomicCommand
    | MoveCommand
    | NuclearStrike
    | CityAttack
    | UnitAttack
    | BuyTile
    | WorkTile
    | LockTile
    | Toggle
    | RemoveConstruction
    | MoveConstruction
    | FoundCity
    | DeclareWar
    | EndTurn
    | DismissPolicy
    | DismissNotifications,
    Field(discriminator="kind"),
]
COMMANDS = TypeAdapter(PlayerCommand)


def command_schema() -> JsonObject:
    schema = COMMANDS.json_schema()
    # Each variant has disjoint kind values; anyOf retains the same validation
    # while using the ordinary JSON Schema union supported by tool consumers.
    schema["anyOf"] = schema.pop("oneOf")
    schema.pop("discriminator")
    return {"type": "object", **schema}


def command_arguments(arguments: JsonObject) -> JsonObject:
    return COMMANDS.validate_python(arguments).model_dump(exclude_unset=True)


class InvalidCommandArguments(BoundaryModel):
    error: Literal["INVALID_COMMAND_ARGUMENTS"] = "INVALID_COMMAND_ARGUMENTS"
    applied: Literal[False] = False
    recoverable: Literal[True] = True
    requiredFields: list[str]
    allowedFields: list[str]
    missingFields: list[str]
    instruction: str = (
        "Use the schema for this kind or copy an offer's command and add intent. "
        "CITY_ATTACK takes target and destination; only ATTACK takes attackFrom. Nothing was dispatched."
    )


def command_error(arguments: JsonObject, error: ValidationError) -> JsonObject:
    """Return field guidance without echoing arbitrary invalid input or dispatching anything."""
    schema = COMMANDS.json_schema()
    kind = arguments.get("kind")
    variant = schema["discriminator"]["mapping"].get(kind) if isinstance(kind, str) else None
    shape: JsonObject = schema["$defs"][variant.rsplit("/", 1)[1]] if variant else {}
    allowed = set(shape.get("properties", {}))
    return InvalidCommandArguments(
        requiredFields=shape.get("required", ["kind"]),
        allowedFields=sorted(allowed),
        missingFields=sorted({str(e["loc"][-1]) for e in error.errors() if e["type"] == "missing"} & allowed),
    ).model_dump()


def add_combat_commands(snapshot: JsonObject) -> None:
    """Attach exact arguments to engine offers; never manufacture availability or intent."""
    for kind, entities in (
        ("CITY_ATTACK", snapshot.get("cities", [])),
        ("ATTACK", snapshot.get("map", {}).get("units", [])),
    ):
        for entity in entities:
            attacks = (
                entity.get("attacks", []) if kind == "CITY_ATTACK" else (entity.get("combat") or {}).get("attacks", [])
            )
            for attack in attacks:
                arguments = {"kind": kind, "target": entity["handle"], "destination": attack["destination"]}
                if kind == "ATTACK":
                    arguments["attackFrom"] = attack["attackFrom"]
                attack["command"] = command_arguments(arguments)
            if kind == "ATTACK":
                combat = entity.get("combat") or {}
                for order in combat.get("orders", []):
                    if order["available"]:
                        order["command"] = command_arguments(
                            {"kind": "SET_UNIT_ORDER", "target": entity["handle"], "selection": order["name"]}
                        )
                for option in (combat.get("promotions") or {}).get("choices", []):
                    if option["available"]:
                        option["command"] = command_arguments(
                            {"kind": "PROMOTE_UNIT", "target": entity["handle"], "selection": option["name"]}
                        )
