"""Recovery fences supersede accepted controls without pretending work has retired."""

import pytest
from pydantic import ValidationError

from civ_agent.orchestration.managed_checkpoints import GameCheckpointReference, HostCheckpointRequest
from civ_agent.orchestration.managed_retirement import ManagedRetirementIntent


def intent(reference, first_run):
    return ManagedRetirementIntent(
        game="game",
        source_epoch="e1",
        target_epoch="e2",
        source_run_id=first_run,
        recovery_run_id="recovery-first-run",
        checkpoint=reference,
    )


def test_retirement_intent_has_exact_owner_and_fresh_epoch():
    reference = GameCheckpointReference(
        request=HostCheckpointRequest(game="game", epoch="e1", id="start", turn=10, handoff=0), sha256="a" * 64
    )
    original = intent(reference, "root-first-run")
    for field, value in (("target_epoch", "e1"), ("game", "other"), ("source_epoch", "other")):
        with pytest.raises(ValidationError, match="OWNER_CONFLICT"):
            ManagedRetirementIntent.model_validate({**original.model_dump(), field: value})
    assert len(original.model_dump_json().encode()) < 2048
