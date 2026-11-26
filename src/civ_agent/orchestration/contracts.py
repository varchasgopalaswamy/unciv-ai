"""Versioned, credential-free identities crossing durable activity boundaries."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from civ_agent.execution_policy import ExecutionPolicy, legacy_execution_policy
from civ_agent.gameplay_policy import GameplayPolicy, legacy_gameplay_policy
from civ_agent.orchestration.provider_contracts import ProviderModelContract
from civ_agent.protocol import JsonObject
from civ_agent.provider_credentials import Provider
from civ_agent.schemas import BoundaryModel, Identifier, NonnegativeInt

OpaqueId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Scope(BoundaryModel):
    version: Literal[1] = 1
    game: OpaqueId
    player: OpaqueId
    epoch: OpaqueId
    activation: OpaqueId

    def identity(self, kind: str, key: str) -> str:
        return digest([self.model_dump(), kind, key])


type FailureCategory = Literal[
    "quota",
    "rate_limit",
    "overloaded",
    "unavailable",
    "server_error",
    "authentication",
    "context_limit",
    "request_rejected",
    "not_found",
    "connection",
    "timeout",
    "request_budget",
    "unknown",
]


class ModelFailure(BoundaryModel):
    model_config = ConfigDict(frozen=True)
    scope: Scope
    run_id: Annotated[str, Field(min_length=1, max_length=1024)]
    model_step: Annotated[int, Field(ge=0)]
    attempt: Annotated[int, Field(ge=1)]
    category: FailureCategory
    dispatch: Literal["not_sent", "rejected", "uncertain"]
    retry_mode: Literal["automatic", "manual", "never"]
    http_status: Annotated[int, Field(ge=400, le=599)] | None = None
    phase: Literal["pool", "connect", "write", "read"] | None = None
    in_stream: bool | None = None
    provider_code: Annotated[str, Field(max_length=80)] | None = None
    provider_type: Annotated[str, Field(max_length=80)] | None = None
    retry_after: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    retry_at: AwareDatetime | None = None


class ModelFailureNotice(BoundaryModel):
    scope: Scope
    sequence: Annotated[int, Field(ge=1)]
    failure: ModelFailure | None

    @model_validator(mode="after")
    def scoped_failure(self):
        if self.failure is not None and self.failure.scope != self.scope:
            raise ValueError("MODEL_FAILURE_SCOPE_CONFLICT")
        return self


class ManagedModelFailures(BoundaryModel):
    scope: Scope
    sequence: Annotated[int, Field(ge=1)]
    failures: Annotated[list[ModelFailure], Field(max_length=3)]


class Reference(BoundaryModel):
    sha256: Digest


class Transcript(BoundaryModel):
    version: Literal["pydantic-steps-v1"] = "pydantic-steps-v1"
    run_id: OpaqueId
    sha256: Digest


type PlayerRole = Literal["gameplay", "economy", "military", "communication"]


class GatewayJob(BoundaryModel):
    scope: Scope
    job: Identifier
    purpose: PlayerRole


class GatewayDependencies(GatewayJob):
    # Corrections/resumes continue the sequence rather than reusing identities.
    model_step_offset: Annotated[int, Field(ge=0)] = 0


class GatewayReceipt(BoundaryModel):
    version: Literal[1] = 1
    result: JsonObject


class GatewayCall(BoundaryModel):
    call_id: Identifier
    model_step: Annotated[int, Field(ge=1)]
    decision: JsonObject


class GatewayCallRequest(BoundaryModel):
    identity: GatewayJob
    call: GatewayCall


class GatewayCatalogue(BoundaryModel):
    catalogue: list[JsonObject]


class ModelSelection(BoundaryModel):
    provider: Provider = "openai"
    authentication: Literal["subscription", "api_key", "adc"] = "subscription"
    model: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S+$")]
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = "medium"
    fast: bool = False


class RoleRequest(GatewayDependencies):
    gameplay_policy: GameplayPolicy = Field(
        default_factory=legacy_gameplay_policy, exclude_if=lambda value: value.version == 0
    )
    execution_policy: ExecutionPolicy = Field(
        default_factory=legacy_execution_policy, exclude_if=lambda value: value.version == 0
    )
    # New trusted requests freeze the typed plan interface. Retained inputs
    # omit this field and keep their original memory tools and history.
    typed_authored_plans: bool = Field(default=False, exclude_if=lambda value: not value)
    provider_contract: ProviderModelContract | None = None
    startup: Reference
    catalogue: Reference
    selection: ModelSelection
    request_limit: Annotated[int, Field(ge=1, le=100)] = 24
    attempt_limit: Literal[1, 2] = 2
    attempt_offset: Annotated[int, Field(ge=0)] = 0
    managed: bool = False
    peace_approvals: bool = False


class RoleReport(BoundaryModel):
    summary: Annotated[str, Field(min_length=1, max_length=8000)]


class RoleCheck(BoundaryModel):
    finished: bool
    feedback: str = ""


class RoleWait(GatewayJob):
    run_id: OpaqueId
    call_id: Identifier
    model_step: Annotated[int, Field(ge=1)]
    expired: bool = False


class RoleWaitStatus(BoundaryModel):
    waiting: bool
    remaining_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0
    result: JsonObject | None = None


class RoleResult(GatewayJob):
    version: Literal[1] = 1
    status: Literal["COMPLETED", "UNAVAILABLE"]
    history: Reference
    usage: JsonObject


class FinishRoleRun(BoundaryModel):
    request: RoleRequest
    completed: bool
    history: Transcript
    usage: JsonObject


class GameRunRequest(BoundaryModel):
    game: OpaqueId
    player: OpaqueId
    turns: Annotated[int, Field(ge=1, le=20)] = 3
    selection: ModelSelection
    request_limit: Annotated[int, Field(ge=1, le=100)] = 24
    restart_after: Annotated[int, Field(ge=1)] | None = 1
    review_interval: Annotated[int, Field(ge=1, le=100)] = 5
    communication_after_handoff: bool = False
    exercise_role_wait: bool = False
    exercise_native_stores: bool = False
    routing_fixture_barrier: bool = False


class PrepareTurn(BoundaryModel):
    run: GameRunRequest
    index: Annotated[int, Field(ge=0)]


class TurnTicket(BoundaryModel):
    scope: Scope
    turn: Annotated[int, Field(ge=0)]
    handoff: Annotated[int, Field(ge=0)]
    index: Annotated[int, Field(ge=0)]


class PrepareRole(BoundaryModel):
    ticket: TurnTicket
    purpose: PlayerRole
    pass_number: Annotated[int, Field(ge=0)] = 0


class HandoffResult(BoundaryModel):
    committed: bool
    turn: Annotated[int, Field(ge=0)]
    handoff: Annotated[int, Field(ge=0)]


class WorkflowLink(BoundaryModel):
    id: OpaqueId
    kind: Literal["communication", "review", "peace", "advisory", "routing", "outgoing-draft", "outgoing-delivery"]


class EventNotice(BoundaryModel):
    scope: Scope
    id: Digest
    native_id: Annotated[int, Field(ge=1)]
    content: Reference


class CommunicationRun(BoundaryModel):
    scope: Scope
    selection: ModelSelection
    request_limit: Annotated[int, Field(ge=1, le=100)] = 24
    managed: bool = False
    model_recovery: bool = False


class CommunicationBatch(BoundaryModel):
    run: CommunicationRun
    notices: Annotated[list[EventNotice], Field(min_length=1, max_length=50)]


class FinishCommunication(BoundaryModel):
    batch: CommunicationBatch
    result: RoleResult


class CommunicationControl(BoundaryModel):
    scope: Scope
    id: OpaqueId
    operation: Literal["hold", "release", "drain", "stop"]


class CommunicationStatus(BoundaryModel):
    held: bool
    active: bool
    pending: int
    completed: int
    duplicates: int
    rejected: int
    failed: bool
    model_failures: list[ModelFailure] = Field(default_factory=list)


class CommunicationResult(BoundaryModel):
    roles: list[RoleResult]
    duplicates: int
    rejected: int


def coordinator_queue(game: str, player: str) -> str:
    return "civ-coordinator-v1-" + digest([game, player])[:32]


def player_queue(game: str, player: str) -> str:
    return "civ-player-v1-" + digest([game, player])[:32]


class ReviewRequest(BoundaryModel):
    execution_policy: ExecutionPolicy = Field(
        default_factory=legacy_execution_policy, exclude_if=lambda value: value.version == 0
    )
    provider_contract: ProviderModelContract | None = None
    scope: Scope
    inputs: Reference
    selection: ModelSelection
    request_limit: Annotated[int, Field(ge=1, le=100)] = 12
    attempt_limit: Annotated[int, Field(ge=1)] = 2
    attempt_offset: Annotated[int, Field(ge=0)] = 0
    timeout_seconds: Annotated[int, Field(ge=1, le=86400)] = 600
    strategy_guide: bool = False
    civilization_guide: bool = False
    managed: bool = False
    source_turn: NonnegativeInt | None = Field(default=None, exclude_if=lambda value: value is None)
    target_turn: NonnegativeInt | None = Field(default=None, exclude_if=lambda value: value is None)
    review_cycle_id: Digest | None = Field(default=None, exclude_if=lambda value: value is None)


class ReviewResult(BoundaryModel):
    version: Literal[1] = 1
    scope: Scope
    status: Literal["COMPLETED", "UNAVAILABLE"] = "COMPLETED"
    report: Reference | None = None
    history: Reference
    usage: JsonObject

    @model_validator(mode="after")
    def report_matches_status(self):
        if (self.status == "COMPLETED") != (self.report is not None):
            raise ValueError("WORKFLOW_REPORT_STATUS_CONFLICT")
        return self


class PrepareReview(BoundaryModel):
    turn: TurnTicket
    selection: ModelSelection
    interval: Annotated[int, Field(ge=1)] = 5
    request_limit: Annotated[int, Field(ge=1, le=100)] = 12
    attempt_limit: Annotated[int, Field(ge=1)] = 2
    timeout_seconds: Annotated[int, Field(ge=1, le=86400)] = 600
    strategy_guide: bool = False
    civilization_guide: bool = False


class PublishReview(BoundaryModel):
    turn: TurnTicket
    request: ReviewRequest
    result: ReviewResult


class PeaceRequest(BoundaryModel):
    execution_policy: ExecutionPolicy = Field(
        default_factory=legacy_execution_policy, exclude_if=lambda value: value.version == 0
    )
    provider_contract: ProviderModelContract | None = None
    military_provider_contract: ProviderModelContract | None = None
    managed: bool = False
    scope: Scope
    basis: Digest
    inputs: Reference
    selection: ModelSelection
    military_selection: ModelSelection | None = None
    request_limit: Annotated[int, Field(ge=1, le=10)] = 2


class PeacePhase(BoundaryModel):
    request: PeaceRequest
    phase: Literal["military", "gameplay"]
    military: JsonObject | None = None

    @property
    def execution_policy(self) -> ExecutionPolicy:
        return self.request.execution_policy

    @property
    def scope(self) -> Scope:
        return self.request.scope.model_copy(
            update={"activation": digest([self.request.scope.model_dump(), self.phase])}
        )

    @property
    def managed(self) -> bool:
        return self.request.managed

    @property
    def provider_contract(self) -> ProviderModelContract | None:
        if self.phase == "military" and self.request.military_selection is not None:
            return self.request.military_provider_contract
        return self.request.provider_contract

    @property
    def selection(self) -> ModelSelection:
        if self.phase == "military" and self.request.military_selection is not None:
            return self.request.military_selection
        return self.request.selection


class PeacePhaseResult(BoundaryModel):
    phase: Literal["military", "gameplay"]
    status: Literal["COMPLETED", "UNAVAILABLE"]
    report: JsonObject | None
    history: Reference
    usage: JsonObject


class FinishPeacePhase(BoundaryModel):
    phase: PeacePhase
    report: JsonObject | None
    history: Transcript
    usage: JsonObject


class PeaceResult(BoundaryModel):
    scope: Scope
    basis: Digest
    status: Literal["COMPLETED", "UNAVAILABLE"]
    overview: JsonObject | None
    approval: JsonObject | None
    phases: list[PeacePhaseResult]


class FinishPeace(BoundaryModel):
    request: PeaceRequest
    phases: list[PeacePhaseResult]


class NativeAuxiliaryTask(BoundaryModel):
    """Trusted logical identity, separate from current classifier evidence."""

    model_config = ConfigDict(frozen=True)
    version: Literal[1] = 1
    kind: Literal["effort_selection", "military_triage", "incoming_routing", "outgoing_routing"]
    key: Digest


class AdvisoryRequest(BoundaryModel):
    scope: Scope
    evidence: Reference
    policy: Reference
    request_id: Digest
    timeout_seconds: Annotated[int, Field(ge=1, le=300)]
    task: NativeAuxiliaryTask | None = Field(default=None, exclude_if=lambda value: value is None)


class AdvisoryExchange(BoundaryModel):
    request: AdvisoryRequest
    reconcile: bool = False


class AdvisoryReply(BoundaryModel):
    outcome: Reference
    unresolved: bool


class FinishAdvisory(BoundaryModel):
    request: AdvisoryRequest
    reply: AdvisoryReply
    reconciled: bool = False


class AdvisoryResult(BoundaryModel):
    scope: Scope
    request_id: Digest
    outcome: Reference


class GameRunResult(BoundaryModel):
    game: OpaqueId
    player: OpaqueId
    handoffs: Annotated[int, Field(ge=0)]
    turn: Annotated[int, Field(ge=0)]
    roles: list[RoleResult]
    reviews: list[ReviewResult] = Field(default_factory=list)
    workflow_links: list[WorkflowLink] = Field(default_factory=list)
    communication_duplicates: Annotated[int, Field(ge=0)] = 0
    peace: list[PeaceResult] = Field(default_factory=list)
    advisories: list[AdvisoryResult] = Field(default_factory=list)
    routing_reports: list[JsonObject] = Field(default_factory=list)
