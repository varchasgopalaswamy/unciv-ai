"""Cumulative host job accounting carried by native durable activity inputs."""

from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_ai.usage import RunUsage

from civ_agent.orchestration.contracts import Digest, Scope, digest
from civ_agent.provider_credentials import Provider
from civ_agent.schemas import BoundaryModel, Identifier, NonnegativeInt

Count = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]
type JobPurpose = Literal["gameplay", "economy", "military", "communication", "review", "consultation"]
type JobPhase = Literal[
    "STARTING", "RUNNING", "ROLE_FINISHED", "WAITING_NEGOTIATION", "WAITING_ROLE", "COMPLETED", "INTERRUPTED", "FAILED"
]


class NativeReviewBinding(BoundaryModel):
    cycle_id: Digest
    source_turn: NonnegativeInt
    target_turn: NonnegativeInt

    @model_validator(mode="after")
    def consecutive_turns(self):
        if self.target_turn != self.source_turn + 1 and (self.source_turn, self.target_turn) != (0, 0):
            raise ValueError("NATIVE_REVIEW_TURN_CONFLICT")
        return self


class NativeJob(BoundaryModel):
    version: Literal[1] = 1
    scope: Scope
    job: Identifier
    purpose: JobPurpose
    parent: "NativeJob | None" = None
    review: NativeReviewBinding | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def legal_parent(self):
        parent = self.parent
        if self.review is not None and (self.purpose != "review" or parent is not None):
            raise ValueError("NATIVE_REVIEW_BINDING_CONFLICT")
        if self.purpose in {"economy", "military"} and parent is None:
            raise ValueError("NATIVE_JOB_PARENT_REQUIRED")
        if parent is not None:
            if (
                self.purpose not in {"economy", "military", "review"}
                or parent.purpose != "gameplay"
                or parent.parent is not None
                or (parent.scope.game, parent.scope.player) != (self.scope.game, self.scope.player)
                or (self.purpose != "review" and parent.scope.epoch != self.scope.epoch)
            ):
                raise ValueError("NATIVE_JOB_PARENT_CONFLICT")
        return self


def host_job_id(job: NativeJob) -> str:
    """Bind a host activation to its immutable native role and parent identity."""
    return "native:" + digest(job.model_dump(mode="json"))


class HostUsage(BoundaryModel):
    input_tokens: Count = 0
    cached_input_tokens: Count = 0
    output_tokens: Count = 0
    reasoning_output_tokens: Count = 0


def host_usage(usage: RunUsage) -> HostUsage:
    """Translate observed SDK totals; this does not assert accounting completeness."""
    return HostUsage(
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cache_read_tokens,
        output_tokens=usage.output_tokens,
        reasoning_output_tokens=usage.output_reasoning_tokens or usage.details.get("reasoning_tokens", 0),
    )


class ApiCounts(BoundaryModel):
    calls: Count = 0
    succeeded: Count = 0
    errors: Count = 0
    replayed: Count = 0
    incomplete: Count = 0
    elapsedMs: Count = 0


class MemoryEditCounts(BoundaryModel):
    batches: Count = 0
    accepted: Count = 0
    rejected: Count = 0
    rejectedBatches: Count = 0
    rejectionReasons: dict[Annotated[str, Field(max_length=160)], Count] = Field(default_factory=dict)
    complete: bool = True

    @model_validator(mode="after")
    def valid_totals(self):
        if (
            self.rejectedBatches > self.batches
            or self.rejectedBatches > self.rejected
            or sum(self.rejectionReasons.values()) != self.rejected
        ):
            raise ValueError("NATIVE_JOB_MEMORY_COUNTS")
        return self


class JobTelemetry(BoundaryModel):
    sequence: Count
    phase: JobPhase
    startedAtMs: Count
    endedAtMs: Count | None = None
    elapsedMs: Count
    negotiationWaitMs: Count = 0
    roleWaitMs: Count = 0
    usageReported: bool
    usageComplete: bool
    model: Annotated[str, Field(max_length=256)] | None = None
    reasoningEffort: Annotated[str, Field(max_length=256)] | None = None
    provider: Provider | None = None
    modelRequests: Count = 0
    apiCalls: Annotated[dict[Annotated[str, Field(max_length=160)], ApiCounts], Field(max_length=512)] = Field(
        default_factory=dict
    )
    gameplayElapsedMs: Count | None = None
    finalizationElapsedMs: Count | None = None
    memoryEdits: MemoryEditCounts | None = None

    @model_validator(mode="after")
    def complete_usage(self):
        if self.usageComplete and (not self.usageReported or self.endedAtMs is None):
            raise ValueError("NATIVE_JOB_USAGE_INCOMPLETE")
        return self


class NativeJobReport(BoundaryModel):
    version: Literal[1] = 1
    job: NativeJob
    usage: HostUsage
    telemetry: JobTelemetry


class JobGrant(BoundaryModel):
    granted: bool
    reason: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    # The current host reports neither field. Preserve them if a host exposes
    # them; absence must not become an invented remaining budget or invocation.
    remaining: Count | None = None
    modelInvocations: Count | None = None
