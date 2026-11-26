"""Retained archive data for the retired workflow recovery protocol."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from civ_agent.orchestration.contracts import Digest, OpaqueId, digest
from civ_agent.orchestration.managed_checkpoints import GameCheckpointReference
from civ_agent.orchestration.managed_host import GameAuthority, ManagedHostStatus, ManagedLimits
from civ_agent.orchestration.managed_retirement import ManagedRetirementIntent
from civ_agent.orchestration.recovery_budgets import AllowancePolicy
from civ_agent.schemas import BoundaryModel, NonnegativeInt

RunId = Annotated[str, Field(min_length=1, max_length=1024)]
type RecoveryStage = Literal[
    "PREFLIGHT",
    "FENCING",
    "SETTLING",
    "INSPECTING",
    "RETIRING",
    "RESTORING",
    "INSTALLING",
    "PUBLISHING",
    "RELEASING",
    "RESUMING",
    "COMPLETED",
]
type RecoveryPermissionKind = Literal[
    "inspect_tasks",
    "restore_host",
    "complete_host",
    "install_player",
    "install_allowances",
    "publish_epoch",
    "release_player",
    "keep_lease",
    "settle_player",
    "close_source",
    "start_target",
    "resume_target",
]


class ManagedRecoveryRequest(BoundaryModel):
    """References only, so each player's client can authenticate original input."""

    model_config = ConfigDict(frozen=True)
    version: Literal[1] = 1
    game: OpaqueId
    source_epoch: OpaqueId
    target_epoch: OpaqueId
    source_run_id: RunId
    source_request_sha256: Digest
    checkpoint: GameCheckpointReference
    allowance_policy: AllowancePolicy = Field(default="carry", exclude_if=lambda value: value == "carry")

    @model_validator(mode="after")
    def binding(self):
        if self.source_epoch == self.target_epoch or (self.checkpoint.request.game, self.checkpoint.request.epoch) != (
            self.game,
            self.source_epoch,
        ):
            raise ValueError("MANAGED_RECOVERY_REQUEST_CONFLICT")
        # A player client must be able to authenticate the first input without
        # access to controller-private payload files. Keep private configuration
        # and all checkpoint contents out of this deliberately small envelope.
        if len(self.model_dump_json().encode()) > 1800:
            raise ValueError("MANAGED_RECOVERY_REQUEST_TOO_LARGE")
        return self

    @property
    def workflow_id(self) -> str:
        return "managed-recovery-" + digest([self.game, self.source_epoch, self.target_epoch])[:48]

    def intent(self, recovery_run_id: str) -> ManagedRetirementIntent:
        return ManagedRetirementIntent(
            game=self.game,
            source_epoch=self.source_epoch,
            target_epoch=self.target_epoch,
            source_run_id=self.source_run_id,
            recovery_run_id=recovery_run_id,
            checkpoint=self.checkpoint,
        )


class RecoveryPermissionRequest(BoundaryModel):
    kind: RecoveryPermissionKind
    player: OpaqueId | None
    command_sha256: Digest

    @model_validator(mode="after")
    def owner(self):
        local = self.kind in {
            "inspect_tasks",
            "install_player",
            "install_allowances",
            "release_player",
            "settle_player",
        }
        if local != (self.player is not None):
            raise ValueError("MANAGED_RECOVERY_PERMISSION_OWNER_CONFLICT")
        return self


def settle_player_permission(
    intent: ManagedRetirementIntent, player: str, session_sha256: str
) -> RecoveryPermissionRequest:
    return RecoveryPermissionRequest(
        kind="settle_player",
        player=player,
        command_sha256=digest([intent.model_dump(mode="json"), player, session_sha256]),
    )


class ManagedRecoveryPlayerAuthority(BoundaryModel):
    """Root-authenticated own-seat authority; no private session is returned."""

    intent: ManagedRetirementIntent
    authority: GameAuthority
    player: OpaqueId
    civilization: Annotated[str, Field(min_length=1, max_length=128)]
    session_sha256: Digest

    @model_validator(mode="after")
    def binding(self):
        if (self.authority.game, self.authority.epoch) != (self.intent.game, self.intent.source_epoch):
            raise ValueError("MANAGED_RECOVERY_PLAYER_AUTHORITY_CONFLICT")
        if len(self.model_dump_json().encode()) > 8192:
            raise ValueError("MANAGED_RECOVERY_PLAYER_AUTHORITY_TOO_LARGE")
        return self


class ManagedRecoverySourceState(BoundaryModel):
    """Original absolute clock and accounting floors, without failure payloads."""

    intent: ManagedRetirementIntent
    phase: Annotated[str, Field(max_length=64)] = "UNKNOWN"
    quiesced: bool = False
    settled: bool = False
    initial_started_at: AwareDatetime
    deadline: AwareDatetime
    initial_turn: NonnegativeInt
    initial_handoff: NonnegativeInt
    model_invocations: NonnegativeInt
    dispatch_index: NonnegativeInt
    control_sequence: NonnegativeInt
    rollovers: NonnegativeInt
    model_recovery: bool = False
    effective_limits: ManagedLimits
    host: ManagedHostStatus

    @model_validator(mode="after")
    def owner(self):
        if self.host.epoch != self.intent.source_epoch or self.host.modelInvocations > self.model_invocations:
            raise ValueError("MANAGED_RECOVERY_SOURCE_STATE_CONFLICT")
        return self


class ManagedRecoveryPermit(BoundaryModel):
    """An exact live-stage observation; its existence alone is not a lock."""

    model_config = ConfigDict(frozen=True)
    request: ManagedRecoveryRequest
    recovery_run_id: RunId
    stage: RecoveryStage
    permission: RecoveryPermissionRequest

    @model_validator(mode="after")
    def phase(self):
        expected = {
            "inspect_tasks": ("INSPECTING", "RETIRING"),
            "settle_player": ("SETTLING", "INSPECTING", "RETIRING"),
            "close_source": ("RETIRING",),
            "start_target": ("RESUMING",),
            "resume_target": ("RESUMING",),
            "restore_host": ("RESTORING",),
            "complete_host": ("PUBLISHING",),
            "install_player": ("INSTALLING",),
            "install_allowances": ("INSTALLING",),
            "publish_epoch": ("PUBLISHING",),
            "release_player": ("RELEASING",),
            "keep_lease": ("SETTLING", "INSPECTING", "RETIRING", "RESTORING", "INSTALLING", "PUBLISHING", "RELEASING"),
        }
        if self.stage not in expected[self.permission.kind]:
            raise ValueError("MANAGED_RECOVERY_PERMISSION_STAGE_CONFLICT")
        return self

    @property
    def intent(self) -> ManagedRetirementIntent:
        return self.request.intent(self.recovery_run_id)
