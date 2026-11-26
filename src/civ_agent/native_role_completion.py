"""Role completion uses native required choices, never inferred gameplay rules."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from civ_agent.protocol import JsonObject


class _PendingChoice(BaseModel):
    # Preserve new native fields and kinds rather than translating them into a
    # second list of game rules. Kind spelling is the native protocol spelling.
    model_config = ConfigDict(strict=True, extra="allow")
    kind: Annotated[str, Field(min_length=1)]
    supported: bool


class _City(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    handle: str
    puppet: bool | None = None
    constructionQueue: list[str] | None = None


class _Controls(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    canDeferChoices: bool = False


class _Negotiation(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    active: bool = False


class _NativeChoices(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    phase: str
    epoch: str
    turn: int
    canEndTurn: bool
    pending: list[_PendingChoice] = Field(default_factory=list)
    turnControls: _Controls = Field(default_factory=_Controls)
    cities: list[_City] = Field(default_factory=list)
    negotiation: _Negotiation = Field(default_factory=_Negotiation)


def _problem(error, review, instruction):
    return {
        "error": error,
        "applied": False,
        "recoverable": True,
        "roleCompletion": review,
        "recovery": {"reads": [{"tool": "player_turn", "arguments": {}}], "instruction": instruction},
    }


def required_choice_problem(
    view,
    role,
    arguments,
    *,
    epoch,
    turn,
    city_owner,
    handoff_review,
    main_deferred=False,
) -> JsonObject | None:
    """Validate a fresh native read without changing observations or role state."""
    try:
        facts = _NativeChoices.model_validate(view)
    except ValidationError:
        return _problem(
            "NATIVE_ROLE_REFRESH_REQUIRED", {}, "Read a current native player_turn before finishing this role."
        )
    if facts.phase != "ACTIVE" or facts.epoch != epoch or facts.turn != turn or facts.negotiation.active:
        return _problem(
            "NATIVE_ROLE_REFRESH_REQUIRED", {}, "Read a current active native player_turn before finishing this role."
        )
    pending = [choice.model_dump(mode="json") for choice in facts.pending] if not facts.canEndTurn else []
    if not facts.canEndTurn and not pending:
        return _problem(
            "NATIVE_ROLE_REFRESH_REQUIRED",
            {"purpose": role, "pendingChoices": []},
            "The native turn is blocked without current required-choice facts. Read player_turn before finishing.",
        )
    construction = [choice for choice in pending if choice["kind"] == "Construction"]
    cities = [city for city in facts.cities if city.puppet is not True and city.constructionQueue == []]
    city_owners = {city.handle: city_owner(city.handle) for city in cities}
    own_construction = bool(construction) and any(city_owners.get(city.handle) == role for city in cities)
    returned_to_main = handoff_review.get("role") == "gameplay" and any(
        choice.get("kind") == "Construction" for choice in handoff_review.get("pending", [])
    )
    blocked = []
    if role == "gameplay":
        blocked = [choice for choice in pending if choice["kind"] != "Construction"]
        if own_construction or returned_to_main:
            blocked.extend(construction)
    elif role == "economy" and own_construction:
        blocked = construction
    review = {
        "purpose": role,
        "pendingChoices": blocked,
        "canEndTurn": facts.canEndTurn,
        "canDeferChoices": facts.turnControls.canDeferChoices,
        "constructionCities": [{"city": city.handle, "owner": city_owners.get(city.handle)} for city in cities],
    }
    escalation = arguments.get("escalateChoices", "")
    if escalation and (
        role != "economy"
        or not isinstance(escalation, str)
        or not escalation.strip()
        or not own_construction
        or arguments.get("deferChoices")
        or arguments.get("deferReason")
        or arguments.get("initiativeDecisions")
        or main_deferred
    ):
        return _problem(
            "NATIVE_ROLE_ESCALATION_INVALID",
            review,
            "Only Economy may transfer its remaining native Construction choices to Main with an escalateChoices reason.",
        )
    if role == "gameplay" and arguments.get("deferChoices"):
        if facts.turnControls.canDeferChoices:
            return None
        return _problem(
            "NATIVE_ROLE_DEFERRAL_NOT_PERMITTED",
            review,
            "The native turn does not permit deferring these choices. Resolve them before finishing.",
        )
    if role == "economy" and main_deferred and facts.turnControls.canDeferChoices:
        return None
    if escalation:
        return None
    if blocked:
        return _problem(
            "NATIVE_ROLE_DECISIONS_REQUIRED",
            review,
            "Resolve the remaining native choices with native actions before finishing. "
            + (
                "Economy may instead transfer its remaining Construction choices to Main with an escalateChoices reason."
                if role == "economy"
                else "For an Economy-owned city, use city_override or request specific economic work. "
                "You may explicitly choose deferChoices=true only when native canDeferChoices permits it."
            ),
        )
    if construction and not cities:
        return _problem(
            "NATIVE_ROLE_REFRESH_REQUIRED",
            review,
            "The native Construction requirement has no matching city facts. Read player_turn before finishing.",
        )
    return None
