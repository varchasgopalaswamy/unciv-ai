"""Permanent source-epoch admission fence, retained by the existing Root history."""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from civ_agent.orchestration.contracts import OpaqueId, digest
from civ_agent.orchestration.managed_checkpoints import GameCheckpointReference
from civ_agent.schemas import BoundaryModel


class ManagedRetirementIntent(BoundaryModel):
    """A trusted recovery execution owns this irreversible old-epoch fence.

    This intent does not prove quiescence, native effect settlement, retirement,
    or permission to restore. Those proofs must follow before epoch publication.
    """

    model_config = ConfigDict(frozen=True)

    version: Literal[1] = 1
    game: OpaqueId
    source_epoch: OpaqueId
    target_epoch: OpaqueId
    source_run_id: Annotated[str, Field(min_length=1, max_length=1024)]
    recovery_run_id: Annotated[str, Field(min_length=1, max_length=1024)]
    checkpoint: GameCheckpointReference

    @model_validator(mode="after")
    def owner(self):
        if self.source_epoch == self.target_epoch or (self.checkpoint.request.game, self.checkpoint.request.epoch) != (
            self.game,
            self.source_epoch,
        ):
            raise ValueError("MANAGED_RETIREMENT_OWNER_CONFLICT")
        return self

    @property
    def workflow_id(self) -> str:
        return "managed-recovery-" + digest([self.game, self.source_epoch, self.target_epoch])[:48]
