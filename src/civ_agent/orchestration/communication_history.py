"""Passive archived communication continuation formats; no execution or control."""

from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from civ_agent.orchestration.contracts import (
    CommunicationBatch,
    CommunicationRun,
    Digest,
    EventNotice,
    ModelFailure,
    ModelSelection,
    PeacePhase,
    PeacePhaseResult,
    ReviewRequest,
    ReviewResult,
    RoleRequest,
    RoleResult,
    Scope,
)
from civ_agent.orchestration.job_contracts import HostUsage, NativeJob, NativeJobReport
from civ_agent.orchestration.managed_retirement import ManagedRetirementIntent
from civ_agent.orchestration.peace_wait_contracts import PeaceApprovalWait
from civ_agent.schemas import BoundaryModel, NonnegativeInt

type ExecutionId = Annotated[str, Field(min_length=1, max_length=1024)]
type RunInput = RoleRequest | ReviewRequest | PeacePhase
type RunResult = RoleResult | ReviewResult | PeacePhaseResult


@dataclass
class CommunicationJob:
    job: NativeJob
    selection: ModelSelection
    started: AwareDatetime
    usage: HostUsage = field(default_factory=HostUsage)
    requests: NonnegativeInt = 0
    sequence: NonnegativeInt = 0
    reported: bool = False
    complete: bool = False
    released: bool = False
    report: NativeJobReport | None = None


class AwaitNativeRun(BoundaryModel):
    request: RunInput
    run_id: ExecutionId

    @property
    def scope(self) -> Scope:
        return self.request.scope

    @property
    def kind(self) -> Literal["role", "review", "peace_phase"]:
        if isinstance(self.request, RoleRequest):
            return "role"
        return "review" if isinstance(self.request, ReviewRequest) else "peace_phase"

    @property
    def workflow_id(self) -> str:
        return self.scope.identity("workflow", self.kind)

    @property
    def workflow_type(self) -> str:
        return {
            "role": "PlayerRoleWorkflow",
            "review": "StrategicReviewWorkflow",
            "peace_phase": "PeacePhaseWorkflow",
        }[self.kind]

    @property
    def result_type(self) -> type[RoleResult] | type[ReviewResult] | type[PeacePhaseResult]:
        return {"role": RoleResult, "review": ReviewResult, "peace_phase": PeacePhaseResult}[self.kind]


class CommunicationContinuation(BoundaryModel):
    version: Literal[1] = 1
    run: CommunicationRun
    held: bool
    failed: bool
    managed_quiesced: bool
    pending: list[EventNotice]
    duplicates: NonnegativeInt
    rejected: NonnegativeInt
    arrivals: NonnegativeInt
    completed_count: NonnegativeInt
    model_notice_sequence: NonnegativeInt
    role_notice_sequence: NonnegativeInt
    phase_notice_sequence: NonnegativeInt
    lease_loss_sequence: NonnegativeInt = 0
    rollovers: NonnegativeInt
    model_failure: ModelFailure | None = None
    active_batch: CommunicationBatch | None = None
    active_request: RoleRequest | None = None
    active_job: CommunicationJob | None = None
    role_run_id: ExecutionId | None = None
    peace_waits: list[PeaceApprovalWait] = Field(default_factory=list)
    completed_peace: list[Digest] = Field(default_factory=list)
    completed_peace_waits: list[Digest] = Field(default_factory=list)
    peace_wait_id: Digest | None = None
    peace_request_index: NonnegativeInt = 0
    phase_results: Annotated[list[PeacePhaseResult], Field(max_length=2)] = Field(default_factory=list)
    peace_request: PeacePhase | None = None
    peace_job: CommunicationJob | None = None
    phase_run_id: ExecutionId | None = None
    peace_ready_pending: list[Digest] = Field(default_factory=list)
    retirement: ManagedRetirementIntent | None = None

    @model_validator(mode="after")
    def ownership(self):
        if not self.run.managed:
            raise ValueError("COMMUNICATION_CONTINUATION_REQUIRES_MANAGED_RUN")
        scope = self.run.scope
        if self.retirement is not None and (
            (self.retirement.game, self.retirement.source_epoch) != (scope.game, scope.epoch) or not self.held
        ):
            raise ValueError("COMMUNICATION_RETIREMENT_CONTINUATION_CONFLICT")
        if len({notice.id for notice in self.pending}) != len(self.pending) or any(
            notice.scope != scope for notice in self.pending
        ):
            raise ValueError("COMMUNICATION_CONTINUATION_NOTICE_CONFLICT")
        if self.active_batch is not None and self.active_batch.run != self.run:
            raise ValueError("COMMUNICATION_CONTINUATION_BATCH_CONFLICT")
        if self.active_batch is not None and any(notice not in self.pending for notice in self.active_batch.notices):
            raise ValueError("COMMUNICATION_CONTINUATION_BATCH_NOTICE_CONFLICT")
        for request in (self.active_request, self.peace_request):
            if request is not None and (
                request.scope.game,
                request.scope.player,
                request.scope.epoch,
            ) != (scope.game, scope.player, scope.epoch):
                raise ValueError("COMMUNICATION_CONTINUATION_OWNER_CONFLICT")
        if self.active_request is not None and self.active_request.purpose != "communication":
            raise ValueError("COMMUNICATION_CONTINUATION_ROLE_CONFLICT")
        if self.active_request is not None and self.active_batch is None:
            raise ValueError("COMMUNICATION_CONTINUATION_BATCH_REQUIRED")
        for held, request, run_id, purpose in (
            (self.active_job, self.active_request, self.role_run_id, "communication"),
            (self.peace_job, self.peace_request, self.phase_run_id, "consultation"),
        ):
            if held is None:
                if run_id is not None:
                    raise ValueError("COMMUNICATION_CONTINUATION_JOB_REQUIRED")
                continue
            if (
                request is None
                or held.job.scope != request.scope
                or held.job.purpose != purpose
                or held.selection != request.selection
            ):
                raise ValueError("COMMUNICATION_CONTINUATION_JOB_CONFLICT")
            if isinstance(request, RoleRequest) and held.job.job != request.job:
                raise ValueError("COMMUNICATION_CONTINUATION_JOB_CONFLICT")
            if isinstance(request, PeacePhase) and held.job.job != request.scope.activation:
                raise ValueError("COMMUNICATION_CONTINUATION_JOB_CONFLICT")
            if held.report is not None and (
                held.report.job != held.job or held.report.telemetry.sequence > held.sequence
            ):
                raise ValueError("COMMUNICATION_CONTINUATION_REPORT_CONFLICT")
        if self.model_failure is not None and self.model_failure.scope not in [
            request.scope for request in (self.active_request, self.peace_request) if request is not None
        ]:
            raise ValueError("COMMUNICATION_CONTINUATION_FAILURE_CONFLICT")
        if len({wait.id for wait in self.peace_waits}) != len(self.peace_waits):
            raise ValueError("COMMUNICATION_CONTINUATION_PEACE_CONFLICT")
        for wait in self.peace_waits:
            if self.active_request is None or (
                wait.identity.scope,
                wait.identity.job,
                wait.identity.purpose,
            ) != (self.active_request.scope, self.active_request.job, self.active_request.purpose):
                raise ValueError("COMMUNICATION_CONTINUATION_PEACE_CONFLICT")
        if self.peace_wait_id is not None and self.peace_wait_id not in {wait.id for wait in self.peace_waits}:
            raise ValueError("COMMUNICATION_CONTINUATION_PEACE_CONFLICT")
        if len(set(self.peace_ready_pending)) != len(self.peace_ready_pending) or not set(
            self.peace_ready_pending
        ) <= set(self.completed_peace_waits):
            raise ValueError("COMMUNICATION_CONTINUATION_READY_CONFLICT")
        if self.phase_results and (
            self.peace_wait_id is None
            or [phase.phase for phase in self.phase_results] != ["military", "gameplay"][: len(self.phase_results)]
        ):
            raise ValueError("COMMUNICATION_CONTINUATION_PHASE_CONFLICT")
        if self.peace_wait_id is None:
            if self.peace_request_index or self.peace_request is not None:
                raise ValueError("COMMUNICATION_CONTINUATION_PHASE_CONFLICT")
        else:
            wait = next(wait for wait in self.peace_waits if wait.id == self.peace_wait_id)
            if self.peace_request_index > len(wait.requests):
                raise ValueError("COMMUNICATION_CONTINUATION_PHASE_CONFLICT")
            if self.peace_request is not None:
                if self.peace_request_index == len(wait.requests) or len(self.phase_results) >= 2:
                    raise ValueError("COMMUNICATION_CONTINUATION_PHASE_CONFLICT")
                expected = PeacePhase(
                    request=wait.requests[self.peace_request_index],
                    phase="military" if not self.phase_results else "gameplay",
                    military=self.phase_results[0].report if self.phase_results else None,
                )
                if self.peace_request != expected or self.peace_job is None or self.phase_run_id is None:
                    raise ValueError("COMMUNICATION_CONTINUATION_PHASE_CONFLICT")
        return self
