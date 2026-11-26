"""One game-owned host lease shared by the managed player coordinators."""

from pathlib import Path
from typing import Literal, Protocol

from pydantic import ConfigDict, Field

from civ_agent.game_config import Limits
from civ_agent.operator_connection import OperatorError
from civ_agent.orchestration.contracts import OpaqueId
from civ_agent.orchestration.managed_checkpoints import HostCheckpointReference, HostCheckpointRequest, HostCheckpoints
from civ_agent.orchestration.store import JournalConflict
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, NonnegativeInt
from civ_agent.service_io import ServiceIO

# OperatorControl.job_limits accepts 1..64 globally and 1..4 per civilization.
# A larger global worker allowance must preserve that independent native limit.
HOST_MAX_JOBS_PER_CIVILIZATION = 4


class ManagedLimits(Limits):
    model_config = ConfigDict(strict=True, extra="forbid", validate_default=True, hide_input_in_errors=True)


def validate_limit_increase(current: Limits, proposed: Limits) -> None:
    """Only the existing operator resume limits may increase; None means unlimited turns."""
    increases = {"max_turns", "max_invocations", "max_seconds", "turn_timeout", "startup_timeout"}
    for name in Limits.model_fields:
        previous, value = getattr(current, name), getattr(proposed, name)
        if name not in increases:
            if value != previous:
                raise ValueError("Resume cannot change saved capacity settings")
        elif name == "max_turns":
            if (
                previous is None
                and value is not None
                or previous is not None
                and value is not None
                and value < previous
            ):
                raise ValueError("Resume cannot decrease saved run limits")
        elif value < previous:
            raise ValueError("Resume cannot decrease saved run limits")


class GameAuthority(BoundaryModel):
    version: Literal[1] = 1
    game: OpaqueId
    epoch: OpaqueId
    lease_id: OpaqueId
    limits: Limits


class ManagedLimitsRequest(BoundaryModel):
    authority: GameAuthority
    limits: ManagedLimits

    @classmethod
    def for_limits(cls, authority: GameAuthority, limits: Limits):
        return cls(authority=authority, limits=ManagedLimits.model_validate(limits.model_dump()))


class ManagedHostStatus(BoundaryModel):
    model_config = ConfigDict(extra="ignore")

    epoch: OpaqueId
    serverInstance: OpaqueId
    phase: Literal["ACTIVE", "PAUSED", "FINISHED", "HALTED"]
    turn: NonnegativeInt
    handoff: NonnegativeInt
    activeCivilization: str = Field(min_length=1)
    activeController: Literal["AGENT", "HUMAN", "BUILT_IN_AI"]
    scheduled: bool
    leaseId: OpaqueId | None
    leaseExpiresInMs: NonnegativeInt
    modelInvocations: NonnegativeInt = 0
    modelUsage: dict[str, NonnegativeInt] = Field(default_factory=dict)
    lease_reacquired: bool = Field(default=False, exclude_if=lambda value: not value)


class HumanActivation(BoundaryModel):
    authority: GameAuthority
    civilization: str = Field(min_length=1)
    handoff: NonnegativeInt
    activation_id: OpaqueId


class HostOperator(Protocol):
    def call(self, operation: str, **fields) -> JsonObject: ...


class ManagedHost:
    """Operator I/O only; the local controller owns renewal timers and player sequencing."""

    def __init__(self, authority: GameAuthority, operator: HostOperator, *, checkpoint_root: Path | None = None):
        self.authority, self.operator = authority.model_copy(deep=True), operator
        self.io = ServiceIO()
        self.lease_reacquired = False
        self.checkpoints = HostCheckpoints(checkpoint_root) if checkpoint_root is not None else None

    def _checkpoint_check(self, request: HostCheckpointRequest):
        if (request.game, request.epoch) != (self.authority.game, self.authority.epoch):
            raise JournalConflict("MANAGED_CHECKPOINT_AUTHORITY_CONFLICT")
        if self.checkpoints is None:
            raise JournalConflict("MANAGED_CHECKPOINT_STORE_REQUIRED")
        return self.checkpoints

    async def capture_checkpoint(self, request: HostCheckpointRequest) -> HostCheckpointReference:
        request = HostCheckpointRequest.model_validate_json(request.model_dump_json())
        checkpoints = self._checkpoint_check(request)
        status = await self._owned()
        if (
            status.phase != "PAUSED"
            or not status.scheduled
            or status.leaseId != self.authority.lease_id
            or (status.turn, status.handoff) != (request.turn, request.handoff)
        ):
            raise JournalConflict("MANAGED_CHECKPOINT_POSITION_CONFLICT")
        # Native snapshot itself checks the stronger distributed host barrier.
        # PAUSED alone does not prove that communication has stopped.
        response = await self.io.call(self.operator.call, "snapshot", leaseId=self.authority.lease_id)
        current = ManagedHostStatus.model_validate(response["status"])
        if (
            current.epoch != request.epoch
            or current.leaseId != self.authority.lease_id
            or current.phase != "PAUSED"
            or not current.scheduled
            or (current.turn, current.handoff) != (request.turn, request.handoff)
        ):
            raise JournalConflict("MANAGED_CHECKPOINT_POSITION_CONFLICT")
        await self.io.call(checkpoints.validate_boundary, request, response["checkpoint"])
        # A retry keeps its original turn boundary rather than replacing it
        # with subsequently played actions at the same native handoff.
        existing = await self.io.call(checkpoints.existing, request)
        if existing is not None:
            return existing
        return await self.io.call(checkpoints.save, request, response["checkpoint"])

    async def verify_checkpoint(self, reference: HostCheckpointReference) -> HostCheckpointReference:
        reference = HostCheckpointReference.model_validate_json(reference.model_dump_json())
        checkpoints = self._checkpoint_check(reference.request)
        await self.io.call(checkpoints.load, reference)
        return reference

    async def barrier(self, authority: GameAuthority) -> ManagedHostStatus:
        self._check(authority)
        status = await self._owned()
        if status.phase in {"FINISHED", "HALTED"}:
            raise JournalConflict("MANAGED_CHECKPOINT_GAME_UNAVAILABLE")
        return await self._call("barrier", leaseId=authority.lease_id)

    async def unbarrier(self, authority: GameAuthority) -> ManagedHostStatus:
        self._check(authority)
        status = await self._owned()
        if status.phase != "PAUSED":
            raise JournalConflict("MANAGED_CHECKPOINT_BARRIER_REQUIRED")
        return await self._call("unbarrier", leaseId=authority.lease_id)

    def _check(self, authority: GameAuthority):
        if authority != self.authority:
            raise JournalConflict("MANAGED_GAME_AUTHORITY_CONFLICT")

    async def _call(self, operation, **fields) -> ManagedHostStatus:
        try:
            response = await self.io.call(self.operator.call, operation, **fields)
        except OperatorError as error:
            if error.status is None and error.code in {"TRANSPORT_TIMEOUT", "TRANSPORT_UNAVAILABLE"}:
                raise ConnectionError("NATIVE_HOST_UNAVAILABLE") from None
            raise
        status = ManagedHostStatus.model_validate(response["status"]).model_copy(update={"lease_reacquired": False})
        if status.epoch != self.authority.epoch:
            raise JournalConflict("MANAGED_GAME_EPOCH_CONFLICT")
        if status.leaseId is not None and status.leaseId != self.authority.lease_id:
            raise JournalConflict("MANAGED_GAME_LEASE_CONFLICT")
        return status

    async def _owned(self) -> ManagedHostStatus:
        status = await self._call("status")
        if status.phase in {"FINISHED", "HALTED"}:
            return status
        if status.leaseId is None:
            status = await self._call("acquire", leaseId=self.authority.lease_id)
            if status.leaseId == self.authority.lease_id:
                self.lease_reacquired = True
        if status.leaseId != self.authority.lease_id:
            raise JournalConflict("MANAGED_GAME_LEASE_REQUIRED")
        return status.model_copy(update={"lease_reacquired": self.lease_reacquired})

    async def start(self, authority: GameAuthority) -> ManagedHostStatus:
        self._check(authority)
        return await self._owned()

    async def configure_limits(self, request: ManagedLimitsRequest) -> ManagedHostStatus:
        self._check(request.authority)
        validate_limit_increase(request.authority.limits, request.limits)
        status = await self._owned()
        if status.phase in {"FINISHED", "HALTED"}:
            return status
        if status.phase != "PAUSED":
            raise JournalConflict("MANAGED_LIMITS_REQUIRE_PAUSED")
        result = await self._call(
            "job_limits",
            leaseId=self.authority.lease_id,
            maxInvocations=0,
            maxConcurrent=request.limits.max_concurrent_jobs,
            maxPerCivilization=min(request.limits.max_concurrent_jobs, HOST_MAX_JOBS_PER_CIVILIZATION),
        )
        self.lease_reacquired = False
        return result

    async def poll(self, authority: GameAuthority) -> ManagedHostStatus:
        self._check(authority)
        status = await self._owned()
        if status.phase == "HALTED" or status.phase == "FINISHED" and status.leaseId is None:
            return status
        result = await self._call("heartbeat", leaseId=authority.lease_id)
        return result.model_copy(update={"lease_reacquired": self.lease_reacquired})

    async def activate_human(self, request: HumanActivation) -> ManagedHostStatus:
        self._check(request.authority)
        status = await self._owned()
        if (
            status.phase in {"FINISHED", "HALTED"}
            or status.activeController != "HUMAN"
            or (status.activeCivilization, status.handoff) != (request.civilization, request.handoff)
        ):
            raise JournalConflict("MANAGED_HUMAN_TURN_CONFLICT")
        # The human plays in the native UI. Its activation token stays inside
        # this activity; only status crosses the workflow boundary.
        return await self._call(
            "activate",
            leaseId=self.authority.lease_id,
            epoch=status.epoch,
            serverInstance=status.serverInstance,
            handoff=request.handoff,
            civilization=request.civilization,
            activationId=request.activation_id,
        )

    async def pause(self, authority: GameAuthority) -> ManagedHostStatus:
        self._check(authority)
        status = await self._owned()
        if status.phase in {"FINISHED", "HALTED"}:
            return status
        result = await self._call("pause", leaseId=authority.lease_id)
        self.lease_reacquired = False
        return result

    async def release(self, authority: GameAuthority) -> ManagedHostStatus:
        self._check(authority)
        status = await self._call("status")
        if status.leaseId is None:
            return status
        return await self._call("release", leaseId=authority.lease_id)

    async def close(self):
        await self.io.close()
