"""A typed completed-restore gate publishes new epoch inputs before admission."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from civ_agent.orchestration.contracts import digest
from civ_agent.orchestration.managed_epochs import (
    EpochPublicationReady,
    epoch_publication_basis,
    epoch_request_sha256,
)
from civ_agent.orchestration.managed_game_contracts import ManagedGameContinuation
from civ_agent.orchestration.managed_host import ManagedHostStatus
from civ_agent.orchestration.store import JournalConflict


class Gate:
    """Explicit test authority; not an implementation of coordinated restore."""

    def __init__(self, ready, *, complete=True):
        self.ready, self.complete, self.active, self.entries = ready, complete, False, 0

    @asynccontextmanager
    async def __call__(self):
        self.entries += 1
        if not self.complete:
            raise JournalConflict("RESTORE_NOT_COMPLETE")
        self.active = True
        try:
            yield self.ready
        finally:
            self.active = False


def replacement(source, epoch="e2", *, invocations=12):
    start = datetime(2026, 11, 5, tzinfo=UTC)
    previous = source.continuation
    host = ManagedHostStatus(
        epoch=epoch,
        serverInstance="restored-server",
        phase="PAUSED",
        turn=3,
        handoff=5,
        activeCivilization="Rome",
        activeController="AGENT",
        scheduled=True,
        leaseId=source.authority.lease_id,
        leaseExpiresInMs=90000,
        modelInvocations=invocations,
    )
    state = ManagedGameContinuation(
        initial_started_at=previous.initial_started_at if previous else start,
        deadline=previous.deadline if previous else start + timedelta(seconds=source.authority.limits.max_seconds),
        initial_turn=previous.initial_turn if previous else 0,
        initial_handoff=previous.initial_handoff if previous else 0,
        host=host,
        model_invocations=invocations,
        mode="PAUSED",
    )
    request = source.model_copy(
        update={"authority": source.authority.model_copy(update={"epoch": epoch}), "continuation": state}
    )
    return type(source).model_validate_json(request.model_dump_json())


def ready(root, request):
    basis = epoch_publication_basis(root)
    state = request.continuation
    assert state is not None
    return EpochPublicationReady(
        phase="READY",
        game=basis.game,
        source_epoch=basis.epoch,
        target_epoch=request.authority.epoch,
        checkpoint_sha256=digest("host checkpoint"),
        initial_request_sha256=basis.initial_request_sha256,
        source_request_sha256=basis.request_sha256,
        previous_publication_sha256=basis.publication_sha256,
        new_request_sha256=epoch_request_sha256(request),
        player_install_receipts={
            seat.session.player: digest([seat.session.player, "installed"]) for seat in request.seats
        },
        credential_rotation_sha256=digest("trusted credential rotation receipt"),
        restored_host_sha256=digest(state.host.model_dump(mode="json")),
        initial_started_at=state.initial_started_at,
        deadline=state.deadline,
        initial_turn=state.initial_turn,
        initial_handoff=state.initial_handoff,
        minimum_model_invocations=state.model_invocations,
    )


def candidate(request):
    return request.model_copy(
        update={
            "continuation": None,
            "seats": [
                seat.model_copy(update={"session": seat.session.model_copy(update={"provider_contracts": {}})})
                for seat in request.seats
            ],
        }
    )


def epoch_status(request):
    from civ_agent.orchestration.managed_game_contracts import ManagedGameStatus

    state = request.continuation
    assert state is not None
    return ManagedGameStatus(
        game=request.authority.game,
        epoch=request.authority.epoch,
        phase="PAUSED",
        mode="PAUSED",
        reason="OPERATOR",
        quiesced=True,
        initial_started_at=state.initial_started_at,
        deadline=state.deadline,
        initial_turn=state.initial_turn,
        turn=state.host.turn,
        handoff=state.host.handoff,
        model_invocations=state.model_invocations,
        active_civilization=state.host.activeCivilization,
        active_controller=state.host.activeController,
        active_turn_id=None,
        rollovers=0,
        effective_limits=state.effective_limits,
    )
