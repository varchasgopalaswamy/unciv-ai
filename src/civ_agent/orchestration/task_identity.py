"""Logical paid-task identities, independent of any execution or recovery system."""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from civ_agent.orchestration.contracts import Digest, OpaqueId, digest
from civ_agent.schemas import BoundaryModel, NonnegativeInt


class TaskOwner(BoundaryModel):
    model_config = ConfigDict(frozen=True)
    game: OpaqueId
    player: OpaqueId
    civilization: Annotated[str, Field(min_length=1, max_length=128)]


class _Task(BoundaryModel):
    model_config = ConfigDict(frozen=True)
    owner: TaskOwner


class TurnRoleTask(_Task):
    kind: Literal["turn_role"] = "turn_role"
    turn: NonnegativeInt
    purpose: Literal["gameplay", "economy", "military"]
    pass_number: NonnegativeInt


class ReviewTask(_Task):
    kind: Literal["review"] = "review"
    source_turn: NonnegativeInt


class CommunicationTask(_Task):
    kind: Literal["communication"] = "communication"
    batch_sha256: Digest
    inputs_sha256: Digest


class PeacePhaseTask(_Task):
    kind: Literal["peace_phase"] = "peace_phase"
    basis: Digest
    phase: Literal["military", "gameplay"]
    inputs_sha256: Digest
    military_sha256: Digest | None

    @model_validator(mode="after")
    def prerequisite(self):
        if (self.phase == "gameplay") != (self.military_sha256 is not None):
            raise ValueError("RECOVERY_PEACE_PREREQUISITE_CONFLICT")
        return self


type TaskIdentity = Annotated[
    TurnRoleTask | ReviewTask | CommunicationTask | PeacePhaseTask, Field(discriminator="kind")
]


def task_identity_sha256(task: TaskIdentity) -> str:
    """Input hashes remain provenance; paid work is keyed by its native logical task."""
    owner = task.owner.model_dump(mode="json")
    if isinstance(task, TurnRoleTask):
        key = [task.turn, task.purpose, task.pass_number]
    elif isinstance(task, ReviewTask):
        key = [task.source_turn]
    elif isinstance(task, CommunicationTask):
        key = [task.batch_sha256]
    else:
        key = [task.basis, task.phase]
    return digest([owner, task.kind, key])
