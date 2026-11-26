"""Local callback draining fences domain writes, preserving broker-owned receipts."""

import asyncio
import copy
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_agent.advisory_policy import AdvisoryPolicy
from civ_agent.decisions_broker import DecisionsBroker, RoutingClient
from civ_agent.orchestration.advisories import NativeAdvisories
from civ_agent.orchestration.contracts import (
    NativeAuxiliaryTask,
    digest,
)
from civ_agent.orchestration.managed_player import NativePlayerSettings
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from tests.test_advisory_broker_v3 import evidence, registered
from tests.test_decisions_advisories import setup as domain_setup
from tests.test_decisions_broker import server


class FixtureAdvisories(NativeAdvisories):
    """Tag synthetic receipt evidence without claiming a production task producer."""

    async def evaluate(self, evidence, *, task=None):
        return await super().evaluate(
            evidence,
            task=task or NativeAuxiliaryTask(kind="military_triage", key=digest(["suspension-fixture", evidence])),
        )


@asynccontextmanager
async def fixture(tmp_path):
    settings, ledger = registered(tmp_path / "broker", native_player="p0")
    provider = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "answers": [
                    {"name": "preservation:0", "type": "choice", "choice": "consider", "confidence": 0.99},
                    {"name": "preservation:1", "type": "choice", "choice": "skip", "confidence": 0.99},
                ],
                "usage": {"input_tokens": 100},
            }
        )
    )
    broker = DecisionsBroker(tmp_path / "broker", provider)
    socket = tmp_path / "broker.sock"
    workflow_store = WorkflowStore(tmp_path / "workflow", player="p0")
    workflow_store.bind_epoch("game", "g1")
    try:
        async with domain_setup(tmp_path / "domain", native_enabled=False) as (memory, gateway, _, _):
            async with server(broker.application(), socket=socket):
                remote = RoutingClient(socket, tmp_path / "broker/token.json")
                root = SimpleNamespace(
                    game="game", player="p0", civilization="Rome", memory=memory, gateway=gateway, store=workflow_store
                )
                native = FixtureAdvisories(root, remote, settings)
                completed = SimpleNamespace(results={}, returned=asyncio.Event(), hook=None)
                original_finish = native.services.finish

                async def finish(finished):
                    result = await original_finish(finished)
                    completed.results[result.scope.activation] = result
                    completed.returned.set()
                    if completed.hook is not None:
                        await completed.hook()
                    return result

                native.services.finish = finish
                gateway.advisories = native
                try:
                    yield SimpleNamespace(
                        native=native,
                        memory=memory,
                        gateway=gateway,
                        ledger=ledger,
                        provider=provider,
                        workflow_store=workflow_store,
                        completed=completed,
                    )
                finally:
                    await native.drain()
    finally:
        workflow_store.close()
        ledger.engine.dispose()
        broker.ledger.engine.dispose()


def install_target_domain_epoch(case):
    """Advance the domain only; this is not a complete restored epoch install."""
    with case.memory.transaction() as db:
        case.memory._set(db, "epoch", "g2")
        case.memory._set(db, "advisories", {"targetEpochMarker": "fresh"})


def test_full_native_player_settings_can_complete_an_advisory_without_extra_policy_fields(tmp_path):
    async def run():
        async with fixture(tmp_path) as case:
            full = NativePlayerSettings.model_validate(
                {
                    **case.native.settings.model_dump(),
                    "economy_model": {"model": "gpt-6-luna", "provider": "openai", "reasoning_effort": "medium"},
                    "military_model": {"model": "gpt-6-luna", "provider": "openai", "reasoning_effort": "medium"},
                }
            )
            native = FixtureAdvisories(case.native.root, case.native.services.broker, full)
            try:
                result = await native.evaluate(evidence())
                assert result["answers"] and result.get("fallback") is None
                assert case.provider.evaluate.await_count == 1
                assert "review_interval" not in native.settings.model_dump()
            finally:
                await native.drain()

    asyncio.run(run())


@pytest.mark.parametrize(("cancel_local", "cancel_drain"), [(False, False), (True, False), (True, True)])
def test_completed_waiter_cannot_commit_into_target_epoch_and_drain_waits_accepted_io(
    tmp_path, monkeypatch, cancel_local, cancel_drain
):
    async def run():
        async with fixture(tmp_path) as case:
            entered, release = threading.Event(), threading.Event()
            original = case.native.store.save_attempt

            def delayed(epoch, identifier, result):
                entered.set()
                assert release.wait(5), "The finite test must release its accepted service write"
                return original(epoch, identifier, result)

            monkeypatch.setattr(case.native.store, "save_attempt", delayed)
            callback = asyncio.create_task(case.native.evaluate(evidence()))
            draining = None
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                result = next(iter(case.completed.results.values()))
                assert case.completed.returned.is_set() and case.native.pending
                receipt = case.workflow_store.receipt(result.scope, "advisory-result")
                broker_receipt = copy.deepcopy(case.ledger.lookup("seat", result.request_id))
                accounting = case.ledger.status("seat")
                if cancel_local:
                    case.gateway.request_checkpoint()
                    draining = asyncio.create_task(case.native.drain())
                    await asyncio.sleep(0)
                    assert not draining.done(), "Drain joins the already accepted service write"
                    if cancel_drain:
                        draining.cancel()
                        await asyncio.sleep(0)
                        assert not draining.done(), "Cancelling a drain still joins its accepted callbacks"
                install_target_domain_epoch(case)
            finally:
                release.set()
            if cancel_local:
                assert draining is not None
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(callback, 5)
                if cancel_drain:
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(draining, 5)
                else:
                    await asyncio.wait_for(draining, 5)
            else:
                with pytest.raises(JournalConflict, match="ADVISORY_EPOCH_CONFLICT"):
                    await asyncio.wait_for(callback, 5)
            assert await case.native.state() == {"targetEpochMarker": "fresh"}
            assert case.workflow_store.receipt(result.scope, "advisory-result") == receipt
            assert case.ledger.lookup("seat", result.request_id) == broker_receipt
            assert case.ledger.status("seat") == accounting
            assert case.provider.evaluate.await_count == 1 and not case.native.pending
            await case.native.drain()
            await case.native.drain()
            assert case.native.suspended and not case.native.pending
            with pytest.raises(asyncio.CancelledError):
                await case.native.evaluate(evidence())
            assert case.provider.evaluate.await_count == 1

    asyncio.run(run())


def test_callback_continuing_after_evaluate_is_epoch_fenced_before_review_delivery(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path) as case:
            entered, release = threading.Event(), threading.Event()
            original = case.native.store.save

            async def triage(policy, turn, *, scheduled_soon, next_review_turn):
                result = await policy.evaluate(evidence())
                await policy.save(
                    reviewDeliveryTurn=turn,
                    reviewDelivery=[{"requestId": result["requestId"], "name": "early_review", "status": "delivered"}],
                )
                return {"earlyReview": True}

            def delayed(*, expected_epoch=None, **values):
                if "reviewDelivery" in values:
                    entered.set()
                    assert release.wait(5)
                return original(expected_epoch=expected_epoch, **values)

            monkeypatch.setattr(AdvisoryPolicy, "triage", triage)
            monkeypatch.setattr(case.native.store, "save", delayed)
            callback = asyncio.create_task(case.native.triage(40, scheduled_soon=False, next_review_turn=50))
            draining = None
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                assert case.completed.returned.is_set()
                case.native.suspend()
                draining = asyncio.create_task(case.native.drain())
                await asyncio.sleep(0)
                assert not draining.done()
                install_target_domain_epoch(case)
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(callback, 5)
            await asyncio.wait_for(draining, 5)
            assert await case.native.state() == {"targetEpochMarker": "fresh"}
            assert case.provider.evaluate.await_count == 1 and not case.native.pending

    asyncio.run(run())


def test_atomic_attempt_merge_preserves_intervening_attempts_reconciliations_and_fields(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path) as case:
            entered, release = threading.Event(), threading.Event()
            original = case.native.store.save_attempt

            def delayed(epoch, identifier, result):
                entered.set()
                assert release.wait(5)
                return original(epoch, identifier, result)

            monkeypatch.setattr(case.native.store, "save_attempt", delayed)
            callback = asyncio.create_task(case.native.evaluate(evidence()))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                case.native.store.save(
                    attempts={"newer": {"requestId": "newer"}},
                    reconciliations={"newer": 1},
                    unrelated="retained",
                )
            finally:
                release.set()
            result = await asyncio.wait_for(callback, 5)
            state = await case.native.state()
            assert state["attempts"]["newer"] == {"requestId": "newer"}
            assert state["attempts"][result["requestId"]] == result
            assert state["reconciliations"] == {"newer": 1} and state["unrelated"] == "retained"

    asyncio.run(run())


def test_checkpoint_release_restores_admission_without_resubmitting_completed_broker_request(tmp_path):
    async def run():
        async with fixture(tmp_path) as case:
            first = await case.native.evaluate(evidence())
            accounting = case.ledger.status("seat")
            case.gateway.request_checkpoint()
            await case.native.drain()
            with pytest.raises(asyncio.CancelledError):
                await case.native.evaluate(evidence())
            case.gateway.release_checkpoint()
            assert not case.native.suspended
            assert await case.native.evaluate(evidence()) == first
            assert case.provider.evaluate.await_count == 1 and case.ledger.status("seat") == accounting
            assert len(case.completed.results) == 1 and not case.native.pending

    asyncio.run(run())


def test_release_cannot_revive_an_old_callback_that_suppresses_local_cancellation(tmp_path):
    async def run():
        async with fixture(tmp_path) as case:
            entered, release = asyncio.Event(), asyncio.Event()

            async def resistant_waiter():
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()

            case.completed.hook = resistant_waiter
            callback = asyncio.create_task(case.native.evaluate(evidence()))
            await asyncio.wait_for(entered.wait(), 5)
            case.gateway.request_checkpoint()
            case.gateway.release_checkpoint()
            assert not case.native.suspended
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(callback, 5)
            assert "attempts" not in await case.native.state()
            assert case.provider.evaluate.await_count == 1 and not case.native.pending

    asyncio.run(run())
