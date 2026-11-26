"""Private native host checkpoints; durable workflows carry bounded references."""

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from civ_agent.orchestration.contracts import Digest, OpaqueId, Scope, digest
from civ_agent.orchestration.store import JournalConflict
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, NonnegativeInt
from civ_agent.tls import private_directory, regular_file, write_private_json


class HostCheckpointRequest(BoundaryModel):
    game: OpaqueId
    epoch: OpaqueId
    id: OpaqueId
    turn: NonnegativeInt
    handoff: NonnegativeInt


class HostCheckpointReference(BoundaryModel):
    version: Literal[1] = 1
    request: HostCheckpointRequest
    sha256: Digest


class GameCheckpointReference(BoundaryModel):
    version: Literal[1] = 1
    request: HostCheckpointRequest
    sha256: Digest


class NativeRunBinding(BoundaryModel):
    scope: Scope
    kind: Literal["role", "review", "peace_phase"]
    run_id: Annotated[str, Field(min_length=1, max_length=1024)]

    @property
    def workflow_id(self) -> str:
        return self.scope.identity("workflow", self.kind)


class ManagedTurnCheckpointState(BoundaryModel):
    scope: Scope | None
    paused: bool
    quiesced: bool
    committed: bool
    runs: Annotated[list[NativeRunBinding], Field(max_length=1024)]

    @model_validator(mode="after")
    def ownership(self):
        if len({run.workflow_id for run in self.runs}) != len(self.runs) or any(
            self.scope is None
            or (run.scope.game, run.scope.player, run.scope.epoch)
            != (self.scope.game, self.scope.player, self.scope.epoch)
            for run in self.runs
        ):
            raise ValueError("MANAGED_CHECKPOINT_RUN_CONFLICT")
        return self


class ManagedGameCheckpointState(BoundaryModel):
    game: OpaqueId
    epoch: OpaqueId
    mode: Literal["RUNNING", "PAUSED", "STOPPED"]
    quiesced: bool
    turn: NonnegativeInt
    handoff: NonnegativeInt
    active_civilization: str = Field(min_length=1, max_length=128)
    active_controller: Literal["AGENT", "HUMAN", "BUILT_IN_AI"]
    active_turn_id: OpaqueId | None
    active_turn_player: OpaqueId | None = None

    @model_validator(mode="after")
    def active_owner(self):
        if (self.active_turn_id is None) != (self.active_turn_player is None):
            raise ValueError("MANAGED_CHECKPOINT_TURN_OWNER_REQUIRED")
        return self


class _NativeBoundary(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    formatVersion: Literal[9]
    epoch: OpaqueId
    handoff: NonnegativeInt
    paused: bool
    communicationPaused: bool
    scheduled: bool
    gameJson: str = Field(min_length=1, repr=False)


class _SavedHostCheckpoint(BoundaryModel):
    version: Literal[1] = 1
    request: HostCheckpointRequest
    checkpoint_sha256: Digest
    checkpoint: JsonObject = Field(repr=False)

    @model_validator(mode="after")
    def boundary(self):
        boundary = _NativeBoundary.model_validate(self.checkpoint)
        if not (boundary.paused and boundary.communicationPaused and boundary.scheduled):
            raise ValueError("HOST_CHECKPOINT_BARRIER_REQUIRED")
        if (boundary.epoch, boundary.handoff) != (self.request.epoch, self.request.handoff):
            raise ValueError("HOST_CHECKPOINT_BOUNDARY_CONFLICT")
        if digest(self.checkpoint) != self.checkpoint_sha256:
            raise ValueError("HOST_CHECKPOINT_CONTENT_CONFLICT")
        return self


class HostCheckpoints:
    """Controller-private immutable files; never expose their content to players."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, request: HostCheckpointRequest) -> Path:
        private_directory(self.root)
        return self.root / (digest([request.game, request.epoch, request.id]) + ".json")

    @staticmethod
    def validate_boundary(request: HostCheckpointRequest, checkpoint: JsonObject) -> None:
        _SavedHostCheckpoint(request=request, checkpoint=checkpoint, checkpoint_sha256=digest(checkpoint))

    def existing(self, request: HostCheckpointRequest) -> HostCheckpointReference | None:
        path = self._path(request)
        if not path.exists() and not path.is_symlink():
            return None
        regular_file(path, private=True)
        raw = path.read_bytes()
        saved = _SavedHostCheckpoint.model_validate_json(raw)
        if saved.request != request:
            raise JournalConflict("HOST_CHECKPOINT_REQUEST_CONFLICT")
        return HostCheckpointReference(request=request, sha256=hashlib.sha256(raw).hexdigest())

    def save(self, request: HostCheckpointRequest, checkpoint: JsonObject) -> HostCheckpointReference:
        request = HostCheckpointRequest.model_validate_json(request.model_dump_json())
        saved = _SavedHostCheckpoint(request=request, checkpoint=checkpoint, checkpoint_sha256=digest(checkpoint))
        existing = self.existing(request)
        if existing is not None:
            if self.load(existing) != checkpoint:
                raise JournalConflict("HOST_CHECKPOINT_REPLACEMENT_REJECTED")
            return existing
        path = self._path(request)
        descriptor, temporary = tempfile.mkstemp(prefix=".host-checkpoint-", dir=self.root)
        os.close(descriptor)
        temporary = Path(temporary)
        try:
            write_private_json(temporary, saved.model_dump(mode="json"))
            try:
                # Retry workers can overlap after an activity timeout. Publish
                # once, so a late worker cannot replace the accepted boundary.
                os.link(temporary, path)
            except FileExistsError:
                regular_file(path, private=True)
                if path.read_bytes() != temporary.read_bytes():
                    raise JournalConflict("HOST_CHECKPOINT_REPLACEMENT_REJECTED") from None
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        result = self.existing(request)
        assert result is not None
        return result

    def load(self, reference: HostCheckpointReference) -> JsonObject:
        reference = HostCheckpointReference.model_validate_json(reference.model_dump_json())
        path = regular_file(self._path(reference.request), private=True)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise JournalConflict("HOST_CHECKPOINT_HASH_CONFLICT")
        saved = _SavedHostCheckpoint.model_validate_json(raw)
        if saved.request != reference.request:
            raise JournalConflict("HOST_CHECKPOINT_REQUEST_CONFLICT")
        return saved.checkpoint
