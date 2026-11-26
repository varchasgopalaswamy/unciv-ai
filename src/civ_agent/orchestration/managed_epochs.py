"""Frozen epoch inputs published only under a trusted completed-restore permit."""

import hashlib
import os
import tempfile
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

from civ_agent.container_support import locked
from civ_agent.orchestration.contracts import Digest, OpaqueId, canonical, digest
from civ_agent.orchestration.managed_game_contracts import ManagedGameRequest
from civ_agent.orchestration.managed_host import ManagedHostStatus, validate_limit_increase
from civ_agent.orchestration.store import JournalConflict
from civ_agent.schemas import BoundaryModel, NonnegativeInt
from civ_agent.tls import private_directory, regular_file, write_private


class EpochPublicationPermit(BoundaryModel):
    game: OpaqueId
    source_epoch: OpaqueId
    target_epoch: OpaqueId
    checkpoint_sha256: Digest
    initial_request_sha256: Digest
    source_request_sha256: Digest
    previous_publication_sha256: Digest
    new_request_sha256: Digest
    player_install_receipts: Annotated[dict[OpaqueId, Digest], Field(min_length=1, max_length=64)]
    credential_rotation_sha256: Digest
    restored_host_sha256: Digest
    initial_started_at: AwareDatetime
    deadline: AwareDatetime
    initial_turn: NonnegativeInt
    initial_handoff: NonnegativeInt
    minimum_model_invocations: NonnegativeInt

    @model_validator(mode="after")
    def different_epoch(self):
        if self.source_epoch == self.target_epoch:
            raise ValueError("MANAGED_EPOCH_REPLACEMENT_REQUIRED")
        return self


class EpochPublicationReady(EpochPublicationPermit):
    phase: Literal["READY"]


class EpochPublicationBasis(BoundaryModel):
    game: OpaqueId
    epoch: OpaqueId
    initial_request_sha256: Digest
    request_sha256: Digest
    publication_sha256: Digest


class ManagedEpochPointer(BoundaryModel):
    version: Literal[1] = 1
    game: OpaqueId
    epoch: OpaqueId
    initial_request_sha256: Digest
    request_sha256: Digest
    ready_sha256: Digest


@dataclass(frozen=True)
class RetainedManagedEpoch:
    request: ManagedGameRequest = field(repr=False)
    request_sha256: str
    ready: EpochPublicationReady | None


type EpochPublicationGate = Callable[[], AbstractAsyncContextManager[EpochPublicationReady]]
_EPOCH = TypeAdapter(OpaqueId)


def _bytes(value: BoundaryModel) -> bytes:
    return (canonical(value.model_dump(mode="json")) + "\n").encode()


def epoch_request_sha256(request: ManagedGameRequest) -> str:
    """Hash the exact canonical bytes stored for a replacement epoch."""
    return hashlib.sha256(_bytes(request)).hexdigest()


def _initial(root: Path):
    path = Path(root) / "native-runtime/game.json"
    if not path.exists() and not path.is_symlink():
        return None
    raw = regular_file(path, private=True).read_bytes()
    return ManagedGameRequest.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


def _directory(root: Path, game: str, epoch: str):
    return Path(root) / "native-runtime/epochs" / digest([game, epoch])


def _pointer_path(root: Path):
    return Path(root) / "native-runtime/current-epoch.json"


def _transition(source, request, proof, initial_sha):
    authority, previous = request.authority, source.authority
    state = request.continuation
    if (
        (proof.game, proof.source_epoch, proof.target_epoch) != (authority.game, previous.epoch, authority.epoch)
        or proof.initial_request_sha256 != initial_sha
        or proof.new_request_sha256 != epoch_request_sha256(request)
        or authority.model_copy(update={"epoch": previous.epoch}) != previous
        or request.model_copy(update={"authority": previous, "continuation": source.continuation}) != source
        or state is None
        or state.mode != "PAUSED"
        or state.active_turn_id is not None
        or state.active_preparation is not None
        or state.communications_started
        or state.retirement is not None
        or state.host.phase != "PAUSED"
        or not state.host.scheduled
        or state.host.leaseId not in {None, authority.lease_id}
        or digest(state.host.model_dump(mode="json")) != proof.restored_host_sha256
        or set(proof.player_install_receipts) != {seat.session.player for seat in request.seats}
    ):
        raise JournalConflict("MANAGED_EPOCH_PUBLICATION_CONFLICT")
    if (state.initial_started_at, state.deadline, state.initial_turn, state.initial_handoff) != (
        proof.initial_started_at,
        proof.deadline,
        proof.initial_turn,
        proof.initial_handoff,
    ) or state.model_invocations < proof.minimum_model_invocations:
        raise JournalConflict("MANAGED_EPOCH_BUDGET_CONFLICT")
    if source.continuation is not None:
        prior = source.continuation
        validate_limit_increase(prior.effective_limits or previous.limits, state.effective_limits or authority.limits)
        if (
            (state.initial_started_at, state.initial_turn, state.initial_handoff)
            != (prior.initial_started_at, prior.initial_turn, prior.initial_handoff)
            or state.deadline < prior.deadline
            or proof.minimum_model_invocations < prior.model_invocations
            or state.host.modelInvocations < prior.host.modelInvocations
        ):
            raise JournalConflict("MANAGED_EPOCH_BUDGET_CONFLICT")


def _read_epoch(root, game, epoch, initial_sha):
    directory = _directory(root, game, epoch)
    raw = regular_file(directory / "game.json", private=True).read_bytes()
    request = ManagedGameRequest.model_validate_json(raw)
    ready_raw = regular_file(directory / "ready.json", private=True).read_bytes()
    ready = EpochPublicationReady.model_validate_json(ready_raw)
    if (
        raw != _bytes(request)
        or ready_raw != _bytes(ready)
        or (request.authority.game, request.authority.epoch) != (game, epoch)
    ):
        raise JournalConflict("MANAGED_EPOCH_CONTENT_CONFLICT")
    pointer = ManagedEpochPointer(
        game=game,
        epoch=epoch,
        initial_request_sha256=initial_sha,
        request_sha256=hashlib.sha256(raw).hexdigest(),
        ready_sha256=hashlib.sha256(ready_raw).hexdigest(),
    )
    return request, ready, pointer


def _active(root, initial, initial_sha, *, lineage=None):
    seen = lineage if lineage is not None else set()
    seen.add(initial.authority.epoch)
    path = _pointer_path(root)
    if not path.exists() and not path.is_symlink():
        return initial, initial_sha, initial_sha, None
    raw = regular_file(path, private=True).read_bytes()
    pointer = ManagedEpochPointer.model_validate_json(raw)
    if (
        pointer.game != initial.authority.game
        or pointer.initial_request_sha256 != initial_sha
        or raw != _bytes(pointer)
    ):
        raise JournalConflict("MANAGED_EPOCH_POINTER_CONFLICT")
    request, ready, stored = _read_epoch(root, pointer.game, pointer.epoch, initial_sha)
    if pointer != stored:
        raise JournalConflict("MANAGED_EPOCH_POINTER_CONFLICT")
    active = request
    child, proof = request, ready
    while True:
        if child.authority.epoch in seen:
            raise JournalConflict("MANAGED_EPOCH_LINEAGE_CONFLICT")
        seen.add(child.authority.epoch)
        if proof.source_epoch == initial.authority.epoch:
            source, source_sha, previous_sha = initial, initial_sha, initial_sha
            previous_ready = None
        else:
            source, previous_ready, previous = _read_epoch(root, pointer.game, proof.source_epoch, initial_sha)
            source_sha, previous_sha = previous.request_sha256, hashlib.sha256(_bytes(previous)).hexdigest()
        if (proof.source_request_sha256, proof.previous_publication_sha256) != (source_sha, previous_sha):
            raise JournalConflict("MANAGED_EPOCH_LINEAGE_CONFLICT")
        _transition(source, child, proof, initial_sha)
        if previous_ready is None:
            break
        child, proof = source, previous_ready
    return active, pointer.request_sha256, hashlib.sha256(raw).hexdigest(), pointer


def select_managed_epoch(
    root: Path, candidate: ManagedGameRequest, *, host: ManagedHostStatus
) -> ManagedGameRequest | None:
    """Reject an unpublished host epoch before provider or worker configuration I/O."""
    if host.epoch != candidate.authority.epoch or host.leaseId not in {None, candidate.authority.lease_id}:
        raise JournalConflict("MANAGED_EPOCH_HOST_AUTHORITY_CONFLICT")
    initial = _initial(root)
    if initial is None:
        if _pointer_path(root).exists() or _pointer_path(root).is_symlink():
            raise JournalConflict("MANAGED_EPOCH_INITIAL_REQUIRED")
        return None
    original, initial_sha = initial
    active, _, _, _ = _active(root, original, initial_sha)
    if host.epoch != active.authority.epoch or host.leaseId not in {None, active.authority.lease_id}:
        raise JournalConflict("MANAGED_EPOCH_UNPUBLISHED")
    validate_limit_increase(
        active.continuation.effective_limits
        if active.continuation is not None and active.continuation.effective_limits is not None
        else active.authority.limits,
        candidate.authority.limits,
    )
    compared = candidate.model_copy(
        update={
            "authority": candidate.authority.model_copy(update={"limits": active.authority.limits}),
            "continuation": active.continuation,
        }
    )
    bare = active.model_copy(
        update={
            "seats": [
                seat.model_copy(update={"session": seat.session.model_copy(update={"provider_contracts": {}})})
                for seat in active.seats
            ]
        }
    )
    if compared != bare:
        raise ValueError("MANAGED_GAME_CONFIGURATION_CHANGED")
    if active.continuation is not None and (
        host.turn < active.continuation.host.turn or host.handoff < active.continuation.host.handoff
    ):
        raise JournalConflict("MANAGED_EPOCH_HOST_POSITION_CONFLICT")
    if active.continuation is not None and host.modelInvocations < active.continuation.host.modelInvocations:
        raise JournalConflict("MANAGED_EPOCH_HOST_ACCOUNTING_CONFLICT")
    return active


def epoch_publication_basis(root: Path) -> EpochPublicationBasis:
    initial = _initial(root)
    if initial is None:
        raise JournalConflict("MANAGED_EPOCH_INITIAL_REQUIRED")
    original, initial_sha = initial
    active, request_sha, publication_sha, _ = _active(root, original, initial_sha)
    return EpochPublicationBasis(
        game=active.authority.game,
        epoch=active.authority.epoch,
        initial_request_sha256=initial_sha,
        request_sha256=request_sha,
        publication_sha256=publication_sha,
    )


def retained_managed_epoch(root: Path, game: str, epoch: str) -> RetainedManagedEpoch:
    """Read a source only from the fully verified current publication lineage."""
    initial = _initial(root)
    if initial is None:
        raise JournalConflict("MANAGED_EPOCH_INITIAL_REQUIRED")
    original, initial_sha = initial
    lineage = set()
    _active(root, original, initial_sha, lineage=lineage)
    if game != original.authority.game or epoch not in lineage:
        raise JournalConflict("MANAGED_EPOCH_RETAINED_SOURCE_CONFLICT")
    if epoch == original.authority.epoch:
        return RetainedManagedEpoch(original, initial_sha, None)
    request, ready, pointer = _read_epoch(root, game, epoch, initial_sha)
    return RetainedManagedEpoch(request, pointer.request_sha256, ready)


def validate_new_epoch(root: Path, target_epoch: OpaqueId) -> EpochPublicationBasis:
    """Validate freshness before a trusted controller mutates host/player epochs."""
    target_epoch = _EPOCH.validate_python(target_epoch, strict=True)
    initial = _initial(root)
    if initial is None:
        raise JournalConflict("MANAGED_EPOCH_INITIAL_REQUIRED")
    original, initial_sha = initial
    lineage = set()
    active, request_sha, publication_sha, _ = _active(root, original, initial_sha, lineage=lineage)
    if target_epoch in lineage:
        raise JournalConflict("MANAGED_EPOCH_REUSE_REJECTED")
    return EpochPublicationBasis(
        game=active.authority.game,
        epoch=active.authority.epoch,
        initial_request_sha256=initial_sha,
        request_sha256=request_sha,
        publication_sha256=publication_sha,
    )


def published_managed_epoch(root: Path) -> ManagedGameRequest:
    """Read the fully validated frozen input for trusted status/export readers."""
    initial = _initial(root)
    if initial is None:
        raise JournalConflict("MANAGED_EPOCH_INITIAL_REQUIRED")
    original, initial_sha = initial
    return _active(root, original, initial_sha)[0]


def _immutable(path: Path, raw: bytes):
    private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".epoch-", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        write_private(temporary, raw)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if regular_file(path, private=True).read_bytes() != raw:
                raise JournalConflict("MANAGED_EPOCH_REPLACEMENT_REJECTED") from None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


async def publish_managed_epoch(
    root: Path, request: ManagedGameRequest, *, permit: EpochPublicationGate
) -> ManagedEpochPointer:
    """Publish only while the trusted restore admission fence remains held."""
    request = ManagedGameRequest.model_validate_json(request.model_dump_json())
    initial = _initial(root)
    if initial is None:
        raise JournalConflict("MANAGED_EPOCH_INITIAL_REQUIRED")
    original, initial_sha = initial
    lineage = set()
    _, _, _, current = _active(root, original, initial_sha, lineage=lineage)
    if request.authority.epoch in lineage and (current is None or current.epoch != request.authority.epoch):
        raise JournalConflict("MANAGED_EPOCH_REUSE_REJECTED")
    async with permit() as supplied:
        ready = EpochPublicationReady.model_validate_json(supplied.model_dump_json())
        directory = private_directory(Path(root) / "native-runtime")
        with locked(directory / "epochs.lock"):
            if _initial(root) != initial:
                raise JournalConflict("MANAGED_EPOCH_INITIAL_CONFLICT")
            lineage = set()
            active, source_sha, previous_sha, current = _active(root, original, initial_sha, lineage=lineage)
            if current is not None and current.epoch == request.authority.epoch:
                existing, proof, pointer = _read_epoch(root, ready.game, ready.target_epoch, initial_sha)
                if existing != request or proof != ready or pointer != current:
                    raise JournalConflict("MANAGED_EPOCH_REPLACEMENT_REJECTED")
                return current
            if request.authority.epoch in lineage:
                raise JournalConflict("MANAGED_EPOCH_REUSE_REJECTED")
            if (ready.source_request_sha256, ready.previous_publication_sha256) != (source_sha, previous_sha):
                raise JournalConflict("MANAGED_EPOCH_STALE_PUBLICATION")
            _transition(active, request, ready, initial_sha)
            target = _directory(root, ready.game, ready.target_epoch)
            _immutable(target / "game.json", _bytes(request))
            _immutable(target / "ready.json", _bytes(ready))
            pointer = ManagedEpochPointer(
                game=ready.game,
                epoch=ready.target_epoch,
                initial_request_sha256=initial_sha,
                request_sha256=epoch_request_sha256(request),
                ready_sha256=hashlib.sha256(_bytes(ready)).hexdigest(),
            )
            write_private(_pointer_path(root), _bytes(pointer))
            return pointer
