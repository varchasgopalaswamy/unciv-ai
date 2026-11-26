"""Intent-bound two-stage parent retirement; native histories retain the detail."""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from civ_agent.orchestration.contracts import Digest, OpaqueId, Scope, digest
from civ_agent.orchestration.job_contracts import HostUsage
from civ_agent.orchestration.managed_checkpoints import NativeRunBinding
from civ_agent.orchestration.managed_retirement import ManagedRetirementIntent
from civ_agent.orchestration.recovery_task_contracts import RecoveryTaskInventoryReference
from civ_agent.schemas import BoundaryModel, NonnegativeInt

ROOT_RETIREMENT_ACTIVITY = "verify_managed_root_retirement_v1"
PARENT_RETIREMENT_ACTIVITY = "authorize_managed_parent_retirement_v1"
RunId = Annotated[str, Field(min_length=1, max_length=1024)]


class ManagedParentBinding(BoundaryModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["turn", "communication"]
    scope: Scope
    workflow_id: OpaqueId
    run_id: RunId
    first_run_id: RunId

    @model_validator(mode="after")
    def identity(self):
        if self.kind == "communication":
            expected = self.scope.identity("workflow", "communication")
            if self.scope.activation != "communication":
                raise ValueError("MANAGED_RETIREMENT_PARENT_SCOPE_CONFLICT")
        else:
            expected = self.scope.activation
        if self.workflow_id != expected:
            raise ValueError("MANAGED_RETIREMENT_PARENT_IDENTITY_CONFLICT")
        return self


def retained_runs_sha256(runs: list[NativeRunBinding]) -> str:
    checked = [NativeRunBinding.model_validate_json(run.model_dump_json()) for run in runs]
    if len({run.workflow_id for run in checked}) != len(checked):
        raise ValueError("MANAGED_RETIREMENT_RUN_DUPLICATE")
    return digest([run.model_dump(mode="json") for run in sorted(checked, key=lambda run: run.workflow_id)])


class FrozenManagedParent(BoundaryModel):
    model_config = ConfigDict(frozen=True)
    version: Literal[1] = 1
    intent: ManagedRetirementIntent
    parent: ManagedParentBinding
    run_count: NonnegativeInt
    runs_sha256: Digest

    @model_validator(mode="after")
    def owner(self):
        if (self.parent.scope.game, self.parent.scope.epoch) != (self.intent.game, self.intent.source_epoch):
            raise ValueError("MANAGED_RETIREMENT_PARENT_OWNER_CONFLICT")
        return self


class RetireManagedParent(BoundaryModel):
    frozen: FrozenManagedParent
    inventory: RecoveryTaskInventoryReference

    @model_validator(mode="after")
    def owner(self):
        parent = self.frozen.parent.scope
        if (self.inventory.scope.game, self.inventory.scope.player, self.inventory.scope.epoch) != (
            parent.game,
            parent.player,
            parent.epoch,
        ):
            raise ValueError("MANAGED_RETIREMENT_INVENTORY_OWNER_CONFLICT")
        return self


class AuthorizeParentRetirement(RetireManagedParent):
    runs: Annotated[list[NativeRunBinding], Field(max_length=1024)]

    @model_validator(mode="after")
    def bindings(self):
        parent = self.frozen.parent.scope
        if (
            len(self.runs) != self.frozen.run_count
            or retained_runs_sha256(self.runs) != self.frozen.runs_sha256
            or any(
                (run.scope.game, run.scope.player, run.scope.epoch) != (parent.game, parent.player, parent.epoch)
                for run in self.runs
            )
        ):
            raise ValueError("MANAGED_RETIREMENT_RETAINED_RUN_CONFLICT")
        return self


class CancelRecoveryRun(AuthorizeParentRetirement):
    run: NativeRunBinding

    @model_validator(mode="after")
    def retained(self):
        if self.run not in self.runs:
            raise ValueError("MANAGED_RETIREMENT_RUN_NOT_RETAINED")
        return self


class NativeRunRetired(BoundaryModel):
    run: NativeRunBinding
    status: Literal["COMPLETED", "CANCELED"]
    usage: (
        dict[
            Literal["requests", "inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens"],
            NonnegativeInt,
        ]
        | None
    )

    @model_validator(mode="after")
    def observed(self):
        if (self.status == "COMPLETED") != (self.usage is not None):
            raise ValueError("MANAGED_RETIREMENT_RUN_USAGE_CONFLICT")
        return self


class ManagedParentRetired(BoundaryModel):
    version: Literal[1] = 1
    frozen: FrozenManagedParent
    inventory: RecoveryTaskInventoryReference
    joined_count: NonnegativeInt
    joined_sha256: Digest
    report_count: NonnegativeInt
    reports_sha256: Digest
    usage: HostUsage
    model_requests: NonnegativeInt
    usage_reported: bool
    usage_complete: bool

    @model_validator(mode="after")
    def joined(self):
        RetireManagedParent(frozen=self.frozen, inventory=self.inventory)
        if (
            self.joined_count != self.frozen.run_count
            or self.joined_sha256 != self.frozen.runs_sha256
            or self.usage_complete
            and not self.usage_reported
        ):
            raise ValueError("MANAGED_RETIREMENT_RECEIPT_CONFLICT")
        return self
