"""Small managed-turn inputs and outcomes; SDK histories stay player-scoped."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from civ_agent.orchestration.contracts import ModelFailure, Scope, TurnTicket, digest
from civ_agent.orchestration.job_contracts import HostUsage
from civ_agent.orchestration.managed_player import PrepareManagedTurn
from civ_agent.schemas import BoundaryModel, NonnegativeInt


class ManagedTurnRequest(BoundaryModel):
    preparation: PrepareManagedTurn
    communication_scopes: Annotated[list[Scope], Field(max_length=64)]
    deadline: AwareDatetime
    turn_timeout_seconds: Annotated[int, Field(ge=1, le=86400)] = 3600
    capacity_retries: Annotated[int, Field(ge=0, le=100)] = 5
    capacity_retry_delay: Annotated[int, Field(ge=1, le=86400)] = 30

    @model_validator(mode="after")
    def communication_binding(self):
        if len({scope.player for scope in self.communication_scopes}) != len(self.communication_scopes):
            raise ValueError("MANAGED_COMMUNICATION_DUPLICATE")
        for scope in self.communication_scopes:
            if (scope.game, scope.epoch) != (self.preparation.session.game, self.preparation.expected_epoch):
                raise ValueError("MANAGED_COMMUNICATION_SCOPE_CONFLICT")
        return self


class ManagedTurnResult(BoundaryModel):
    ticket: TurnTicket | None = None
    committed: bool
    turn: NonnegativeInt
    handoff: NonnegativeInt
    status: Literal["COMPLETED", "UNAVAILABLE"]
    reason: Annotated[str, Field(max_length=160)] | None = None
    usage: HostUsage
    usage_complete: bool
    role_count: NonnegativeInt
    review_count: NonnegativeInt


class ManagedTurnStatus(BoundaryModel):
    phase: str
    paused: bool
    quiesced: bool
    reason: Annotated[str, Field(max_length=160)] | None = None
    role_count: NonnegativeInt
    review_count: NonnegativeInt
    model_failures: list[ModelFailure] = Field(default_factory=list)
    deadline: AwareDatetime | None = None


def managed_turn_id(preparation: PrepareManagedTurn) -> str:
    return digest(
        [
            "managed-turn",
            preparation.session.game,
            preparation.session.player,
            preparation.expected_epoch,
            preparation.expected_handoff,
            preparation.index,
        ]
    )
