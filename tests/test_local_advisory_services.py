"""Instance-local broker reconciliation preserves request receipts and scope."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_agent.advisory_answers import failed_answers, parse_answers
from civ_agent.decisions_tasks import Evidence, request_body, request_id, request_metadata
from civ_agent.orchestration.advisory_activities import AdvisoryServices
from civ_agent.orchestration.contracts import (
    AdvisoryExchange,
    AdvisoryRequest,
    FinishAdvisory,
    NativeAuxiliaryTask,
    Scope,
    digest,
)
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from tests.test_advisory_broker_v3 import evidence, registered


def setup(tmp_path):
    settings, ledger = registered(tmp_path / "broker", native_player="p0")
    store = WorkflowStore(tmp_path / "worker", player="p0")
    scope = Scope(game="game", player="p0", epoch="e1", activation="advisory1")
    store.bind_epoch("game", "e1")
    frozen = Evidence.model_validate(evidence()).model_dump(exclude_none=True)
    body = request_body(frozen, settings)
    request = AdvisoryRequest(
        scope=scope,
        evidence=store.put(scope, frozen),
        policy=store.put(scope, settings.model_dump()),
        request_id=request_id(frozen),
        timeout_seconds=settings.decisions_timeout_seconds,
        task=NativeAuxiliaryTask(kind="military_triage", key=digest(["synthetic-receipt-test", request_id(frozen)])),
    )
    raw = {
        **request_metadata(body, frozen),
        **parse_answers(
            body,
            {
                "answers": [
                    {"name": "preservation:0", "type": "choice", "choice": "consider", "confidence": 0.99},
                    {"name": "preservation:1", "type": "choice", "choice": "skip", "confidence": 0.7},
                ]
            },
            settings.decisions_acceptance_probability,
        ),
    }
    broker = SimpleNamespace(
        backend=settings.decisions_backend, batch=AsyncMock(return_value=raw), lookup=AsyncMock(return_value=raw)
    )
    services = AdvisoryServices(store, broker, settings, civilization="Rome")
    return store, ledger, broker, request, raw, body, settings, services


def test_interrupted_submission_looks_up_original_receipt_and_retains_raw_probabilities(tmp_path):
    async def run():
        store, ledger, broker, request, _, _, _, services = setup(tmp_path)
        broker.batch.side_effect = asyncio.CancelledError()
        try:
            operation = AdvisoryExchange(request=request)
            with pytest.raises(asyncio.CancelledError):
                await services.exchange(operation)
            reply = await services.exchange(operation)
            outcome = store.get(request.scope, reply.outcome)
            assert not reply.unresolved and len(outcome["answers"]) == 1
            assert len(outcome["rawAnswers"]) == 2 and outcome["rawAnswers"][1]["confidence"] == 0.7
            assert broker.batch.await_count == broker.lookup.await_count == 1
            assert await services.exchange(operation) == reply
            assert broker.batch.await_count == 1
            broker.lookup.assert_awaited_once_with(request.request_id, timeout=5)
        finally:
            store.close()
            ledger.engine.dispose()

    asyncio.run(run())


def test_uncertain_broker_completion_remains_unavailable_without_another_submission(tmp_path):
    async def run():
        store, ledger, broker, request, _, body, settings, services = setup(tmp_path)
        pending = {
            **request_metadata(body, store.get(request.scope, request.evidence)),
            **failed_answers(body, settings.decisions_acceptance_probability, "completion_unknown"),
        }
        broker.batch.return_value = pending
        broker.lookup.return_value = pending
        try:
            first = await services.exchange(AdvisoryExchange(request=request))
            assert first.unresolved
            second = await services.exchange(AdvisoryExchange(request=request, reconcile=True))
            assert second.unresolved
            result = await services.finish(FinishAdvisory(request=request, reply=second, reconciled=True))
            assert store.get(request.scope, result.outcome)["fallback"] == "completion_unknown"
            assert broker.batch.await_count == broker.lookup.await_count == 1
            assert store.receipt(request.scope, "advisory-result") == result.model_dump(mode="json")
        finally:
            store.close()
            ledger.engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["policy", "player", "epoch", "identity"])
def test_broker_activity_rejects_changed_operator_policy_or_scoped_input(tmp_path, damage):
    async def run():
        store, ledger, broker, request, _, _, settings, services = setup(tmp_path)
        try:
            if damage == "policy":
                policy = settings.model_dump()
                policy["decisions_acceptance_probability"] = 0.0
                request = request.model_copy(update={"policy": store.put(request.scope, policy)})
            elif damage == "player":
                frozen = store.get(request.scope, request.evidence)
                frozen["player"] = "Greece"
                request = request.model_copy(update={"evidence": store.put(request.scope, frozen)})
            elif damage == "identity":
                request = request.model_copy(update={"request_id": "0" * 64})
            else:
                request = request.model_copy(update={"scope": request.scope.model_copy(update={"epoch": "e2"})})
            with pytest.raises(JournalConflict):
                await services.exchange(AdvisoryExchange(request=request))
            assert broker.batch.await_count == broker.lookup.await_count == 0
        finally:
            store.close()
            ledger.engine.dispose()

    asyncio.run(run())
