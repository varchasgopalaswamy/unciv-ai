"""Independent frozen review cycles use the canonical native accounting wire."""

import asyncio
from typing import cast

import pytest
from pydantic import ValidationError

from civ_agent.orchestration.job_admission import JobAdmission
from civ_agent.orchestration.job_contracts import NativeJob, NativeReviewBinding, host_job_id
from civ_agent.proposal_gateway import PlayerConnection
from tests.test_native_job_admission import Host, job


@pytest.mark.parametrize("source,target", [(0, 0), (0, 1), (5, 6)])
def test_independent_review_binding_reaches_native_claim_without_gameplay_parent(source, target):
    async def run():
        host = Host()

        async def connection(_scope):
            return cast(PlayerConnection, host)

        admission = JobAdmission(game="game", player="p0", civilization="Rome", connection=connection)
        legacy = job("review", name="review")
        binding = NativeReviewBinding(cycle_id="a" * 64, source_turn=source, target_turn=target)
        independent = NativeJob.model_validate({**legacy.model_dump(), "review": binding.model_dump()})
        assert (await admission.claim(independent)).granted
        assert host.calls[-1] == {
            "op": "job_claim",
            "epoch": "e1",
            "job": host_job_id(independent),
            "purpose": "review",
            "review": {"cycleId": "a" * 64, "sourceTurn": source, "targetTurn": target},
        }
        assert independent.parent is None and host_job_id(independent) != host_job_id(legacy)

    asyncio.run(run())


@pytest.mark.parametrize("source,target", [(-1, 0), (0, 2), (1, 1), (2, 1)])
def test_review_binding_rejects_invalid_turn_relation(source, target):
    with pytest.raises(ValidationError):
        NativeReviewBinding(cycle_id="a" * 64, source_turn=source, target_turn=target)


def test_review_binding_cannot_be_used_on_another_role_or_with_gameplay_parent():
    binding = NativeReviewBinding(cycle_id="a" * 64, source_turn=0, target_turn=1)
    for identity in (job(), job("review", parent=job())):
        with pytest.raises(ValidationError, match="NATIVE_REVIEW_BINDING_CONFLICT"):
            NativeJob.model_validate({**identity.model_dump(), "review": binding.model_dump()})


def test_legacy_review_serialization_omits_new_optional_binding():
    legacy = job("review", parent=job())
    assert "review" not in legacy.model_dump(mode="json")
    assert NativeJob.model_validate_json(legacy.model_dump_json()) == legacy
