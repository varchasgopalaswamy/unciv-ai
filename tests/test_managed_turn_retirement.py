"""A retired Turn keeps its exact SDK graph and accounting until explicit join."""

from datetime import UTC, datetime, timedelta

from civ_agent.orchestration.contracts import (
    Scope,
    digest,
)
from civ_agent.orchestration.managed_checkpoints import GameCheckpointReference, HostCheckpointRequest
from civ_agent.orchestration.managed_player import PrepareManagedTurn
from civ_agent.orchestration.managed_retirement import ManagedRetirementIntent
from civ_agent.orchestration.managed_turn_contracts import ManagedTurnRequest
from civ_agent.orchestration.recovery_task_contracts import RecoveryTaskInventoryReference
from tests.test_managed_player import session


def configuration():
    return ManagedTurnRequest(
        preparation=PrepareManagedTurn(session=session(), index=0, expected_epoch="e1", expected_handoff=10),
        communication_scopes=[],
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        turn_timeout_seconds=120,
        capacity_retries=2,
        capacity_retry_delay=1,
    )


def retirement(request):
    intent = ManagedRetirementIntent(
        game="game",
        source_epoch="e1",
        target_epoch="e2",
        source_run_id="root:first",
        recovery_run_id="recovery:first",
        checkpoint=GameCheckpointReference(
            request=HostCheckpointRequest(game="game", epoch="e1", id="starting", turn=0, handoff=10),
            sha256=digest("verified starting checkpoint"),
        ),
    )
    reference = RecoveryTaskInventoryReference(
        scope=Scope(game="game", player="p0", epoch="e1", activation="inventory"),
        request_sha256=digest(intent.model_dump(mode="json")),
        inventory_sha256=digest("private exact task inventory"),
        sha256=digest("immutable private inventory file"),
    )
    return intent, reference
