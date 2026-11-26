"""Frozen reasons and native facts for a newly admitted player role."""

import copy
from typing import Literal

from pydantic import ConfigDict, Field

from civ_agent.orchestration.contracts import PlayerRole, TurnTicket
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, NonnegativeInt


class NativeRoleTask(BoundaryModel):
    model_config = ConfigDict(frozen=True)
    version: Literal[1] = 1
    purpose: PlayerRole
    turn: NonnegativeInt
    handoff: NonnegativeInt
    passNumber: NonnegativeInt
    reason: Literal["turn", "role_attention", "required_choices"]
    message: str | None
    pendingChoices: list[JsonObject] = Field(default_factory=list)
    canEndTurn: bool | None
    canDeferChoices: bool | None


def build_role_task(
    ticket: TurnTicket,
    purpose: PlayerRole,
    pass_number: int,
    view: JsonObject,
    *,
    message: str | None = None,
    handoff_review: JsonObject | None = None,
) -> NativeRoleTask:
    """Use the exact startup view; a stale review never invents new obligations."""
    pending = view.get("pending", [])
    review = handoff_review or {}
    reason = (
        "required_choices"
        if pending and review.get("role") == purpose and review.get("pending") == pending
        else "role_attention"
        if message
        else "turn"
    )
    return NativeRoleTask(
        purpose=purpose,
        turn=ticket.turn,
        handoff=ticket.handoff,
        passNumber=pass_number,
        reason=reason,
        message=message,
        pendingChoices=copy.deepcopy(pending),
        canEndTurn=view.get("canEndTurn"),
        canDeferChoices=view.get("turnControls", {}).get("canDeferChoices"),
    )
