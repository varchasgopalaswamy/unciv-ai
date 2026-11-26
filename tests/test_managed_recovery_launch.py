"""Explicit recovery selection is immutable input, never workflow progress."""

from civ_agent.orchestration.managed_checkpoints import GameCheckpointReference, HostCheckpointRequest
from civ_agent.orchestration.managed_epochs import (
    retained_managed_epoch,
)
from civ_agent.orchestration.managed_recovery_contracts import ManagedRecoveryRequest


def request(root, source, *, target="e2"):
    from civ_agent.orchestration.contracts import digest

    return ManagedRecoveryRequest(
        game=source.authority.game,
        source_epoch=source.authority.epoch,
        target_epoch=target,
        source_run_id="source-first",
        source_request_sha256=retained_managed_epoch(
            root, source.authority.game, source.authority.epoch
        ).request_sha256,
        checkpoint=GameCheckpointReference(
            request=HostCheckpointRequest(
                game=source.authority.game, epoch=source.authority.epoch, id="starting", turn=3, handoff=5
            ),
            sha256=digest("host checkpoint"),
        ),
    )
