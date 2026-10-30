"""Agent-facing command shapes. Legality and execution remain the host's responsibility."""

from typing import Annotated, Literal

from pydantic import Field, StrictBool, TypeAdapter, ValidationError, field_validator

from civ_agent.memory_records import InitiativeDecision, Intent
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, NonnegativeInt, argument_issues, argument_shape

Name = Annotated[str, Field(min_length=1)]


class Position(BoundaryModel):
    x: int
    y: int


class Command(BoundaryModel):
    intent: Intent | None = None
    reviewAfter: StrictBool = Field(
        default=False,
        description="Military only: include a fresh military completion review after this action succeeds. "
        "The receipt remains authoritative if the subsequent refresh fails.",
    )


class ResearchQueue(Command):
    kind: Literal["SET_RESEARCH_QUEUE"]
    queue: Annotated[
        list[Name],
        Field(
            min_length=1,
            max_length=1024,
            description="Replace the entire research queue, including current research, in prerequisite order. The first technology becomes active. To finish Mining before Calendar, send ['Mining', 'Calendar']; ['Calendar'] switches immediately. Stored science is retained.",
        ),
    ]


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
        "RESPOND_CITY_STATE_ALERT",
        "MANAGE_CAPTURED_CITY",
        "SET_CITY_FOCUS",
        "ASSIGN_SPECIALIST",
        "UNASSIGN_SPECIALIST",
        "ACTIVATE_GREAT_PERSON",
    ]
    target: Name
    selection: Name


class RespondFriendship(Command):
    kind: Literal["RESPOND_FRIENDSHIP"]
    target: Annotated[
        Name, Field(description="The friendship popup's alert handle from player_turn.alerts, not a civilization ID.")
    ]
    selection: Literal["Accept", "Decline"]


class RenameUnit(Command):
    kind: Literal["RENAME_UNIT"]
    target: Name
    selection: str


class DefaultPromotions(Command):
    kind: Literal["SET_DEFAULT_PROMOTIONS"]
    target: Name
    selection: Name
    enabled: bool


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
        "SAVE_UNIT_PROMOTIONS",
    ]
    target: Name


class EconomicCommand(Command):
    kind: Literal["PILLAGE", "DISBAND_UNIT"]
    target: Name
    confirmAction: bool = False


class MoveCommand(Command):
    kind: Literal["MOVE_UNIT", "SET_UNIT_DESTINATION", "SWAP_UNITS"]
    target: Name
    destination: Position
    confirmAction: StrictBool = False


class PositionCommand(Command):
    kind: Literal["REASSIGN_CITY_TILE", "REBASE_UNIT", "PARADROP", "AIR_SWEEP", "CONNECT_ROAD"]
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
    initiativeDecisions: Annotated[list[InitiativeDecision], Field(max_length=2)] = Field(default_factory=list)
    deferReason: Annotated[str, Field(min_length=1, max_length=500)] = ""
    deferChoices: StrictBool = False

    @field_validator("initiativeDecisions")
    @classmethod
    def unique_initiatives(cls, value):
        if len({decision.initiative for decision in value}) != len(value):
            raise ValueError("Assess each initiative only once")
        return value


class Autoplay(Command):
    kind: Literal["AUTOPLAY"]
    selection: Literal["ALL", "MILITARY", "CIVILIANS", "ECONOMY"]


class DismissPolicy(Command):
    kind: Literal[
        "DISMISS_POLICY_PICKER", "ABSTAIN_DIPLOMATIC_VOTE", "ACKNOWLEDGE_DIPLOMATIC_RESULTS", "MOVE_AUTOMATED_UNITS"
    ]


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
    | RespondFriendship
    | RenameUnit
    | DefaultPromotions
    | EconomicChoice
    | TargetCommand
    | EconomicCommand
    | MoveCommand
    | PositionCommand
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
    | Autoplay
    | DismissPolicy
    | DismissNotifications,
    Field(discriminator="kind"),
]
COMMANDS = TypeAdapter(PlayerCommand)


def command_schema(
    allowed_kinds: set[str] | frozenset[str] | None = None, *, military_review: bool = False
) -> JsonObject:
    """Compact transport envelope; COMMANDS enforces the selected action's shape.

    Inspected options provide complete commands. Keep the accepted field types
    discoverable without repeating the same envelope in every action variant.
    """
    schema = COMMANDS.json_schema()
    properties: JsonObject = {}
    kinds = []
    for variant in schema["oneOf"]:
        fields = schema["$defs"][variant["$ref"].rsplit("/", 1)[1]]["properties"]
        kind = fields["kind"]
        selected = [
            value for value in kind.get("enum", [kind.get("const")]) if allowed_kinds is None or value in allowed_kinds
        ]
        if not selected:
            continue
        kinds.extend(selected)
        for name, shape in fields.items():
            properties.setdefault(
                name, {k: v for k, v in shape.items() if k not in {"title", "description", "default"}}
            )
    properties["kind"] = {"type": "string", "enum": sorted(kinds)}
    if military_review:
        properties["reviewAfter"]["description"] = Command.model_fields["reviewAfter"].description
    else:
        properties.pop("reviewAfter", None)
    if "initiativeDecisions" in properties:
        properties["initiativeDecisions"]["description"] = (
            "END_TURN only. Optional private decisions about the delivered strategic report's numbered initiatives."
        )
    definitions = {}

    def references(value):
        if isinstance(value, dict):
            if "$ref" in value:
                key = value["$ref"].rsplit("/", 1)[1]
                if key not in definitions:
                    definitions[key] = schema["$defs"][key]
                    references(definitions[key])
            for part in value.values():
                references(part)
        elif isinstance(value, list):
            for part in value:
                references(part)

    references(properties)
    return {
        "type": "object",
        "properties": properties,
        "required": ["kind"],
        "additionalProperties": False,
        "$defs": definitions,
    }


def command_arguments(arguments: JsonObject) -> JsonObject:
    return COMMANDS.validate_python(arguments).model_dump(exclude_unset=True)


class InvalidCommandArguments(BoundaryModel):
    error: Literal["INVALID_COMMAND_ARGUMENTS"] = "INVALID_COMMAND_ARGUMENTS"
    applied: Literal[False] = False
    recoverable: Literal[True] = True
    requiredFields: list[str]
    allowedFields: list[str]
    missingFields: list[str]
    invalidFields: list[JsonObject]
    expectedArguments: JsonObject
    instruction: str = (
        'Copy an offered command; optional intent is an object, e.g. {"reason":"your reason"} or {"cityPlan":"delivered plan ID"}. '
        "Otherwise use expectedArguments for this kind. Supply confirmations only after reading consequences. "
        "Nothing was dispatched; this argument error alone does not require another player_turn."
    )


def command_error(arguments: JsonObject, error: ValidationError) -> JsonObject:
    """Return field guidance without echoing arbitrary invalid input or dispatching anything."""
    schema = COMMANDS.json_schema()
    kind = arguments.get("kind")
    variant = schema["discriminator"]["mapping"].get(kind) if isinstance(kind, str) else None
    shape: JsonObject = schema["$defs"][variant.rsplit("/", 1)[1]] if variant else {}
    allowed = set(shape.get("properties", {}))
    expected = argument_shape(shape, schema.get("$defs", {}))
    if variant:
        expected["properties"]["kind"] = {"const": kind}
    return InvalidCommandArguments(
        requiredFields=shape.get("required", ["kind"]),
        allowedFields=sorted(allowed),
        missingFields=sorted({str(e["loc"][-1]) for e in error.errors() if e["type"] == "missing"} & allowed),
        invalidFields=argument_issues(error, {**shape, "$defs": schema.get("$defs", {})}),
        expectedArguments=expected or {"instruction": "Use a kind from the player_command schema."},
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
