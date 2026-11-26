"""Bounded managed-game controls and frozen native workflow continuation state."""

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from civ_agent.orchestration.contracts import Digest, ManagedModelFailures, ModelFailure, OpaqueId, Scope, digest
from civ_agent.orchestration.managed_checkpoints import GameCheckpointReference
from civ_agent.orchestration.managed_host import (
    GameAuthority,
    ManagedHostStatus,
    ManagedLimits,
    validate_limit_increase,
)
from civ_agent.orchestration.managed_player import NativePlayerSession, PrepareManagedTurn
from civ_agent.orchestration.managed_retirement import ManagedRetirementIntent
from civ_agent.orchestration.managed_turn_contracts import ManagedTurnResult, ManagedTurnStatus
from civ_agent.schemas import BoundaryModel, NonnegativeInt


class ManagedSeat(BoundaryModel):
    civilization: Annotated[str, Field(min_length=1, max_length=128)]
    session: NativePlayerSession


class ManagedTurnControl(BoundaryModel):
    scope: Scope
    workflow_id: OpaqueId
    operation: Literal["pause", "resume", "status", "close"]
    id: OpaqueId
    deadline: AwareDatetime | None = None
    turn_timeout_seconds: Annotated[int, Field(ge=1, le=86400)] | None = None


class ManagedTurnProbe(BoundaryModel):
    status: ManagedTurnStatus
    result: ManagedTurnResult | None = None


class ManagedGameContinuation(BoundaryModel):
    initial_started_at: AwareDatetime
    deadline: AwareDatetime
    initial_turn: NonnegativeInt
    initial_handoff: NonnegativeInt
    host: ManagedHostStatus
    model_invocations: NonnegativeInt
    dispatch_index: NonnegativeInt = 0
    control_sequence: NonnegativeInt = 0
    rollovers: NonnegativeInt = 0
    communications_started: bool = False
    initial_startup_complete: bool = Field(default=False, exclude_if=lambda value: not value)
    active_preparation: PrepareManagedTurn | None = None
    active_turn_id: OpaqueId | None = None
    human_activation_handoff: NonnegativeInt | None = None
    mode: Literal["RUNNING", "PAUSED", "STOPPED"] = "RUNNING"
    reason: Annotated[str, Field(max_length=128)] | None = None
    resume_after_rollover: bool = False
    effective_limits: ManagedLimits | None = None
    model_recovery: bool = False
    model_pause: bool = False
    host_recovery_pending: bool = Field(default=False, exclude_if=lambda value: not value)
    turn_model_failures: list[ModelFailure] = Field(default_factory=list)
    communication_model: Annotated[list[ManagedModelFailures], Field(max_length=64)] = Field(default_factory=list)
    acknowledged_model_failures: Annotated[list[Digest], Field(max_length=128)] = Field(default_factory=list)
    starting_checkpoint: GameCheckpointReference | None = None
    retirement: ManagedRetirementIntent | None = None

    @model_validator(mode="after")
    def active_identity(self):
        if (self.active_turn_id is None) != (self.active_preparation is None):
            raise ValueError("MANAGED_ACTIVE_TURN_INCOMPLETE")
        if self.host.turn < self.initial_turn or self.host.handoff < self.initial_handoff:
            raise ValueError("MANAGED_INITIAL_POSITION_CONFLICT")
        if self.host.modelInvocations > self.model_invocations:
            raise ValueError("MANAGED_ACCOUNTING_CONFLICT")
        return self


class ManagedGameRequest(BoundaryModel):
    version: Literal[1] = 1
    authority: GameAuthority
    seats: Annotated[list[ManagedSeat], Field(min_length=1, max_length=64)]
    continuation: ManagedGameContinuation | None = None
    rollover_events: Annotated[int, Field(ge=5, le=10000)] = 128
    poll_seconds: Annotated[float, Field(gt=0, le=10)] = 2.0
    starting_checkpoints: bool = False

    @model_validator(mode="after")
    def frozen_configuration(self):
        if (
            len({seat.civilization for seat in self.seats}) != len(self.seats)
            or len({seat.session.player for seat in self.seats}) != len(self.seats)
            or any(seat.session.game != self.authority.game for seat in self.seats)
        ):
            raise ValueError("MANAGED_SEAT_CONFLICT")
        state = self.continuation
        if state is not None:
            limits = state.effective_limits or self.authority.limits
            validate_limit_increase(self.authority.limits, limits)
            if (
                state.host.epoch != self.authority.epoch
                or state.deadline != state.initial_started_at + timedelta(seconds=limits.max_seconds)
                or state.active_preparation is not None
                and state.active_preparation.session not in [seat.session for seat in self.seats]
            ):
                raise ValueError("MANAGED_CONTINUATION_CONFLICT")
            checkpoint = state.starting_checkpoint
            if checkpoint is not None and (
                not self.starting_checkpoints
                or (checkpoint.request.game, checkpoint.request.epoch) != (self.authority.game, self.authority.epoch)
                or checkpoint.request.turn > state.host.turn
                or checkpoint.request.handoff > state.host.handoff
            ):
                raise ValueError("MANAGED_CHECKPOINT_CONTINUATION_CONFLICT")
            if state.retirement is not None and (
                (state.retirement.game, state.retirement.source_epoch) != (self.authority.game, self.authority.epoch)
                or state.retirement.checkpoint != checkpoint
                or state.mode == "RUNNING"
                or state.resume_after_rollover
            ):
                raise ValueError("MANAGED_RETIREMENT_CONTINUATION_CONFLICT")
        return self


class ManagedGameControl(BoundaryModel):
    operation: Literal["pause", "resume", "stop"]
    limits: ManagedLimits | None = None

    @model_validator(mode="after")
    def resume_limits(self):
        if self.limits is not None and self.operation != "resume":
            raise ValueError("MANAGED_LIMITS_REQUIRE_RESUME")
        return self


class ManagedGameStatus(BoundaryModel):
    game: OpaqueId
    epoch: OpaqueId
    phase: Annotated[str, Field(max_length=64)]
    mode: Literal["RUNNING", "PAUSED", "STOPPED"]
    reason: Annotated[str, Field(max_length=128)] | None
    quiesced: bool
    initial_started_at: AwareDatetime
    deadline: AwareDatetime
    initial_turn: NonnegativeInt
    turn: NonnegativeInt
    handoff: NonnegativeInt
    model_invocations: NonnegativeInt
    active_civilization: Annotated[str, Field(min_length=1, max_length=128)]
    active_controller: Literal["AGENT", "HUMAN", "BUILT_IN_AI"]
    active_turn_id: OpaqueId | None
    rollovers: NonnegativeInt
    effective_limits: ManagedLimits | None = None
    model_failures: list[ModelFailure] = Field(default_factory=list)


class ManagedGameResult(BoundaryModel):
    version: Literal[1] = 1
    status: Literal["FINISHED"] = "FINISHED"
    game: OpaqueId
    epoch: OpaqueId
    initial_started_at: AwareDatetime
    deadline: AwareDatetime
    initial_turn: NonnegativeInt
    turn: NonnegativeInt
    handoffs: NonnegativeInt
    model_invocations: NonnegativeInt
    rollovers: NonnegativeInt
    effective_limits: ManagedLimits | None = None


def managed_game_id(game: str, epoch: str) -> str:
    return "managed-game-" + digest([game, epoch])[:48]


def managed_game_queue(game: str) -> str:
    return "civ-managed-game-v1-" + digest(game)[:32]
