"""Logical correction allowance from native workflow state and SDK lifecycle facts."""

from typing import Annotated, Literal, overload

from pydantic import ConfigDict, Field, model_validator

from civ_agent.orchestration.contracts import (
    Digest,
    PlayerRole,
    ReviewRequest,
    RoleRequest,
    Scope,
)
from civ_agent.orchestration.store import JournalConflict
from civ_agent.schemas import BoundaryModel, NonnegativeInt

type CorrectionKind = Literal["role", "review"]
SdkRunId = Annotated[str, Field(min_length=1, max_length=1024)]


class CorrectionRun(BoundaryModel):
    run_id: SdkRunId
    ordinal: Annotated[int, Field(ge=1)]


class CorrectionState(BoundaryModel):
    scope: Scope
    kind: CorrectionKind
    workflow_run_id: SdkRunId
    attempt_limit: Annotated[int, Field(ge=1)]
    attempt_offset: NonnegativeInt
    runs: Annotated[list[CorrectionRun], Field(max_length=512)]
    paused: bool
    quiesced: bool

    @model_validator(mode="after")
    def ordinals(self):
        if (
            self.attempt_offset > self.attempt_limit
            or len({run.run_id for run in self.runs}) != len(self.runs)
            or any(not self.attempt_offset < run.ordinal <= self.attempt_limit for run in self.runs)
            or [run.ordinal for run in self.runs] != sorted(run.ordinal for run in self.runs)
        ):
            raise ValueError("CORRECTION_STATE_CONFLICT")
        return self


class CorrectionAllowance(BoundaryModel):
    model_config = ConfigDict(frozen=True)
    scope: Scope
    kind: CorrectionKind
    purpose: PlayerRole | Literal["review"]
    workflow_run_id: SdkRunId
    attempt_limit: Annotated[int, Field(ge=1)]
    attempt_offset: NonnegativeInt
    used: NonnegativeInt
    evidence_sha256: Digest

    @model_validator(mode="after")
    def boundary(self):
        if not self.attempt_offset <= self.used <= self.attempt_limit or (self.kind == "review") != (
            self.purpose == "review"
        ):
            raise ValueError("CORRECTION_ALLOWANCE_CONFLICT")
        return self

    @property
    def remaining(self) -> int:
        return self.attempt_limit - self.used


@overload
def carry_correction_allowance(allowance: CorrectionAllowance, request: RoleRequest) -> RoleRequest: ...


@overload
def carry_correction_allowance(allowance: CorrectionAllowance, request: ReviewRequest) -> ReviewRequest: ...


def carry_correction_allowance(
    allowance: CorrectionAllowance, request: RoleRequest | ReviewRequest
) -> RoleRequest | ReviewRequest:
    """Bind an unadmitted replacement request; the caller holds the restore permit."""
    allowance = CorrectionAllowance.model_validate_json(allowance.model_dump_json())
    model = RoleRequest if isinstance(request, RoleRequest) else ReviewRequest
    request = model.model_validate_json(request.model_dump_json())
    kind = "role" if isinstance(request, RoleRequest) else "review"
    purpose = request.purpose if isinstance(request, RoleRequest) else "review"
    if (
        (request.scope.game, request.scope.player) != (allowance.scope.game, allowance.scope.player)
        or request.scope.epoch == allowance.scope.epoch
        or (kind, purpose, request.attempt_limit) != (allowance.kind, allowance.purpose, allowance.attempt_limit)
        or request.attempt_offset not in {0, allowance.used}
    ):
        raise JournalConflict("CORRECTION_TRANSFER_CONFLICT")
    if allowance.remaining == 0:
        raise JournalConflict("CORRECTION_ATTEMPTS_EXHAUSTED")
    return model.model_validate_json(request.model_copy(update={"attempt_offset": allowance.used}).model_dump_json())
