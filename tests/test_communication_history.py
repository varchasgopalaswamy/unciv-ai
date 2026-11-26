"""Communication rollover retains native jobs and reads the same SDK execution."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from civ_agent.orchestration.communication_history import (
    CommunicationContinuation,
    CommunicationJob,
)
from civ_agent.orchestration.contracts import (
    CommunicationBatch,
    CommunicationRun,
    EventNotice,
    ModelSelection,
    RoleRequest,
    RoleResult,
    Scope,
    canonical,
    digest,
)
from civ_agent.orchestration.job_contracts import HostUsage, NativeJob
from civ_agent.orchestration.store import JournalConflict, WorkflowStore


def setup(tmp_path):
    store = WorkflowStore(tmp_path / "worker", player="p0")
    store.bind_epoch("game", "e1")
    scope = Scope(game="game", player="p0", epoch="e1", activation="communication-job")
    reference = store.put(scope, {"fixture": "private recorded input"})
    request = RoleRequest(
        scope=scope,
        job=scope.activation,
        purpose="communication",
        startup=reference,
        catalogue=reference,
        selection=ModelSelection(model="scripted-v1"),
        managed=True,
    )

    def check_owner(selected):
        if (selected.game, selected.player) != (scope.game, scope.player):
            raise JournalConflict("NATIVE_RESULT_FOREIGN_PLAYER")

    root = SimpleNamespace(store=store, check_owner=check_owner)
    result = RoleResult(
        scope=scope,
        job=request.job,
        purpose=request.purpose,
        status="COMPLETED",
        history=reference,
        usage={
            "requests": 2,
            "inputTokens": 20,
            "cachedInputTokens": 6,
            "outputTokens": 14,
            "reasoningOutputTokens": 4,
        },
    )
    return root, request, result


def continuation(request, reference):
    run = CommunicationRun(
        scope=request.scope.model_copy(update={"activation": "communication-actor"}),
        selection=request.selection,
        managed=True,
        model_recovery=True,
    )
    notice = EventNotice(scope=run.scope, id=digest("event"), native_id=1, content=reference)
    return CommunicationContinuation(
        run=run,
        held=True,
        failed=False,
        managed_quiesced=True,
        pending=[notice],
        duplicates=2,
        rejected=1,
        arrivals=4,
        completed_count=3,
        model_notice_sequence=7,
        role_notice_sequence=2,
        phase_notice_sequence=0,
        lease_loss_sequence=5,
        rollovers=1,
        active_batch=CommunicationBatch(run=run, notices=[notice]),
        active_request=request,
        active_job=CommunicationJob(
            NativeJob(scope=request.scope, job=request.job, purpose="communication"),
            request.selection,
            datetime(2026, 11, 5, tzinfo=UTC),
            usage=HostUsage(input_tokens=20, cached_input_tokens=6, output_tokens=14, reasoning_output_tokens=4),
            requests=2,
            sequence=8,
            reported=True,
        ),
        role_run_id="first-native-run",
    )


def test_continuation_retains_pending_identity_counters_original_start_and_all_four_usage(tmp_path):
    root, request, result = setup(tmp_path)
    try:
        saved = continuation(request, result.history)
        restored = CommunicationContinuation.model_validate_json(saved.model_dump_json())
        assert restored == saved
        assert restored.active_job is not None
        assert restored.active_job.started == datetime(2026, 11, 5, tzinfo=UTC)
        assert restored.active_job.sequence == 8 and restored.active_job.requests == 2
        assert restored.active_job.usage.model_dump() == {
            "input_tokens": 20,
            "cached_input_tokens": 6,
            "output_tokens": 14,
            "reasoning_output_tokens": 4,
        }
        assert restored.model_notice_sequence == 7 and restored.lease_loss_sequence == 5
    finally:
        root.store.close()


@pytest.mark.parametrize(
    "damage", ["owner", "selection", "duplicate_notice", "job", "ready", "batch", "missing_notice", "phase_index"]
)
def test_continuation_rejects_foreign_or_misattributed_state(tmp_path, damage):
    root, request, result = setup(tmp_path)
    try:
        body = continuation(request, result.history).model_dump(mode="json")
        if damage == "owner":
            body["pending"][0]["scope"]["player"] = "p1"
        elif damage == "selection":
            body["active_job"]["selection"]["reasoning_effort"] = "high"
        elif damage == "duplicate_notice":
            body["pending"].append(body["pending"][0])
        elif damage == "job":
            body["active_job"]["job"]["job"] = "unrelated"
        elif damage == "ready":
            body["peace_ready_pending"] = [digest("not-completed")]
        elif damage == "batch":
            body["active_batch"] = None
        elif damage == "missing_notice":
            body["pending"] = []
        else:
            body["peace_request_index"] = 1
        with pytest.raises(ValueError, match="COMMUNICATION_CONTINUATION"):
            CommunicationContinuation.model_validate_json(canonical(body))
    finally:
        root.store.close()
