import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from civ_agent.observations import DetailQuery
from civ_agent.orchestration.gateway_state import GatewayState, capture_job, restore_job
from civ_agent.orchestration.store import JournalConflict
from civ_agent.proposal_gateway import JobAccess
from civ_agent.review_grounding import ReviewEvidence
from tests.domain_fixtures import Host


def stateful_job():
    job = JobAccess("job", "military", "e1", Host(), {"records": {}})
    job.observation = {"revision": "hidden-refresh", "cities": []}
    job.details_read = [DetailQuery.model_validate({"tool": "unit_details", "arguments": {"target": "unit1"}})]
    job.detail_seen = {"unit1": {"stateId": "state1"}}
    job.memory_versions = {"strategy1": 3}
    job.war_versions = {"war1": 2}
    job.trades = {"offer1": {"proposalId": "offer1", "version": 2}}
    job.seen_messages = {"message1", "message2"}
    job.delivered_messages = {"message1"}
    job.negotiation_pending.set()
    job.gameplay_ended.set()
    job.role_finished.set()
    job.wait_started = 90
    job.completed_wait = 5.0
    job.handoff_at = 95
    return job


def snapshot(job):
    return capture_job(
        job,
        delivered_access={"revision": "actually-delivered", "details": []},
        military_review={"basis": "review1"},
        military_targets={"unit1"},
        monotonic=lambda: 100.0,
        wall=lambda: 1000.0,
    )


def restore(state):
    return restore_job(
        state, Host(), job_id="job", purpose="military", epoch="e1", monotonic=lambda: 50.0, wall=lambda: 1030.0
    )


def test_restart_preserves_delivery_evidence_without_promoting_cached_reads():
    original = stateful_job()
    original.verified = True
    original.owns_connection = True
    state = GatewayState.model_validate_json(snapshot(original).model_dump_json())
    recovered = restore(state)
    assert recovered.token != original.token
    assert original.token not in state.model_dump_json()
    assert recovered.connection is not original.connection
    assert not recovered.verified and not recovered.owns_connection
    assert recovered.details_read == original.details_read
    assert recovered.memory_versions == original.memory_versions
    assert recovered.war_versions == original.war_versions
    assert recovered.trades == original.trades
    assert recovered.delivered_messages == {"message1"} and recovered.seen_messages == {"message1", "message2"}
    assert recovered.observation is not None and recovered.observation["revision"] == "hidden-refresh"
    assert state.delivered_access == {"revision": "actually-delivered", "details": []}
    assert state.military_review == {"basis": "review1"} and state.military_targets == ["unit1"]
    assert (
        recovered.negotiation_pending.is_set()
        and recovered.gameplay_ended.is_set()
        and recovered.role_finished.is_set()
    )
    assert recovered.wait_started == 10.0 and recovered.handoff_at == 15.0
    recovered.trades["offer1"]["version"] = 9
    assert original.trades["offer1"]["version"] == 2


def test_restart_rejects_changed_identity_missing_fields_and_coerced_flags():
    state = snapshot(stateful_job())
    with pytest.raises(JournalConflict, match="IDENTITY_CONFLICT"):
        restore_job(state, Host(), job_id="other", purpose="military", epoch="e1")
    with pytest.raises(JournalConflict, match="IDENTITY_CONFLICT"):
        restore_job(state, Host(), job_id="job", purpose="military", epoch="e2")
    changed = state.model_copy(deep=True)
    changed.data.pop("memory_versions")
    with pytest.raises(JournalConflict, match="STATE_INCOMPLETE"):
        restore(changed)
    changed = state.model_copy(deep=True)
    changed.data["closed"] = "true"
    with pytest.raises(ValidationError):
        restore(changed)


def test_capture_refuses_in_flight_tasks_and_locks():
    async def run():
        job = stateful_job()
        task = asyncio.create_task(asyncio.Event().wait())
        job.pending_calls[task] = {"tool": "player_command"}
        try:
            with pytest.raises(JournalConflict, match="CAPTURE_IN_FLIGHT"):
                snapshot(job)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            job.pending_calls.clear()
        async with job.lock:
            with pytest.raises(JournalConflict, match="CAPTURE_IN_FLIGHT"):
                snapshot(job)

    asyncio.run(run())


def test_new_gateway_fields_require_an_explicit_persistence_decision(monkeypatch):
    from civ_agent.orchestration import gateway_state

    existing = gateway_state.fields(JobAccess)
    monkeypatch.setattr(gateway_state, "fields", lambda _: [*existing, SimpleNamespace(name="new_state")])
    with pytest.raises(JournalConflict, match="STATE_FIELDS_CHANGED"):
        snapshot(stateful_job())


def test_dynamic_job_attributes_cannot_be_silently_dropped():
    job = stateful_job()
    vars(job)["untracked"] = {"important": True}
    with pytest.raises(JournalConflict, match="STATE_FIELDS_CHANGED"):
        snapshot(job)


@pytest.mark.parametrize("value", [object(), b"not-json", float("nan")])
def test_snapshot_refuses_non_json_context_instead_of_coercing_or_erasing_it(value):
    job = stateful_job()
    job.memory["unexpected"] = value
    with pytest.raises((TypeError, ValueError)):
        snapshot(job)


def test_frozen_source_labels_and_records_survive_restore():
    job = stateful_job()
    job.review_inputs = {"observation": {"turn": 5}}
    job.review_evidence = ReviewEvidence(job.review_inputs)
    source = job.review_evidence.deliver("unit_details", {"unit": "unit1"})
    recovered = restore(snapshot(job))
    assert recovered.review_evidence is not None
    assert recovered.review_evidence.namespace == job.review_evidence.namespace
    assert recovered.review_evidence.deliver("unit_details", {"unit": "unit1"}) == source
