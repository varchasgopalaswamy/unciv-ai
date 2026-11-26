"""Original auxiliary reservations survive epoch lineage without answer rebinding."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import aiohttp
import pytest
from sqlalchemy import select

from civ_agent.decisions_broker import DecisionsBroker
from civ_agent.decisions_ledger import DecisionsLedger, NativeAuxiliaryTaskUnavailable, canonical, requests
from civ_agent.decisions_tasks import request_body, request_metadata
from civ_agent.native_broker_contracts import NativeBrokerScope
from civ_agent.orchestration.communication_store import CommunicationStore, communication_scope, notifications
from civ_agent.orchestration.contracts import NativeAuxiliaryTask, digest
from civ_agent.orchestration.incoming_routing import IncomingBatch
from civ_agent.orchestration.routing import NativeRouting
from civ_agent.orchestration.routing_contracts import routing_submission
from tests.domain_fixtures import managed
from tests.test_decisions_broker import server
from tests.test_incoming_routing import event, setup
from tests.test_native_broker_fence import (
    close_command,
    configured,
    decoded,
    evidence,
    incoming,
    native_request,
    provider,
    scope,
)


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("kind", ["advisory", "routing"])
def test_changed_inputs_do_not_renew_original_task_allowance_after_epoch_close(tmp_path, kind, unknown):
    async def run():
        _, ledger = configured(tmp_path)
        upstream = provider()
        method = getattr(upstream, "evaluate" if kind == "advisory" else "classify")
        if unknown:
            method.side_effect = asyncio.CancelledError
        broker = DecisionsBroker(tmp_path, upstream)
        submit = broker.native_batch if kind == "advisory" else broker.native_route
        original = native_request(kind)
        if unknown:
            with pytest.raises(asyncio.CancelledError):
                await submit(incoming(original))
        else:
            assert decoded(await submit(incoming(original)))["fallback"] == "service_failure"
        before = ledger.status("seat") if kind == "advisory" else ledger.routing_status("seat")
        await broker.close_native_epoch(incoming(close_command().model_dump(mode="json")))
        changed = native_request(kind, index=2)
        changed["scope"]["epoch"] = "e2"
        changed["task"] = original["task"]
        if kind == "routing":
            # Incoming protocol6 derives one operation identity from the exact
            # logical batch; a changed context does not change that identity.
            changed["submission"] = routing_submission(
                original["submission"]["operationId"],
                "outgoing",
                {**original["submission"]["bundle"], "context": "changed"},
                "luna",
            ).model_dump(mode="json")
        result = decoded(await submit(incoming(changed)))
        assert result["fallback"] == "task_allowance_exhausted"
        assert not result.get("answers") and not result.get("rawAnswers")
        assert "nativeTask" not in result and "nativeScope" not in result
        if kind == "advisory":
            assert result["inputHash"] != decoded(await submit(incoming(original)))["inputHash"]
        assert (ledger.status("seat") if kind == "advisory" else ledger.routing_status("seat")) == before
        assert method.await_count == 1

    asyncio.run(run())


def test_ledger_retains_original_task_and_receipt_through_multiple_closed_ancestors(tmp_path):
    async def run():
        _, ledger = configured(tmp_path)
        upstream = provider()
        broker = DecisionsBroker(tmp_path, upstream)
        original = native_request("advisory")
        first = decoded(await broker.native_batch(incoming(original)))
        await broker.close_native_epoch(incoming(close_command().model_dump(mode="json")))
        second_close = close_command()
        second_close = second_close.model_copy(
            update={
                "scope": second_close.scope.model_copy(update={"epoch": "e2"}),
                "owner": second_close.owner.model_copy(
                    update={
                        "source_epoch": "e2",
                        "target_epoch": "e3",
                        "checkpoint": second_close.owner.checkpoint.model_copy(
                            update={"request": second_close.owner.checkpoint.request.model_copy(update={"epoch": "e2"})}
                        ),
                    }
                ),
            }
        )
        await broker.close_native_epoch(incoming(second_close.model_dump(mode="json")))
        target = native_request("advisory", index=2)
        target["scope"]["epoch"] = "e3"
        target["task"] = original["task"]
        assert decoded(await broker.native_batch(incoming(target)))["fallback"] == "task_allowance_exhausted"
        # Exact receipt reuse remains free even when a genuinely new task asks
        # the identical question. It never rewrites original provenance.
        exact = {**target, "requestId": original["requestId"], "evidence": original["evidence"]}
        exact["task"] = NativeAuxiliaryTask(kind="effort_selection", key=digest("another-real-task")).model_dump(
            mode="json"
        )
        assert decoded(await broker.native_batch(incoming(exact))) == first
        raw = ledger.lookup("seat", original["requestId"])
        assert (
            raw["nativeTask"] == original["task"] and NativeBrokerScope.model_validate(raw["nativeScope"]).epoch == "e1"
        )
        assert upstream.evaluate.await_count == 1

    asyncio.run(run())


def test_untagged_ancestor_is_explicitly_unsupported_instead_of_guessed(tmp_path):
    async def run():
        _, ledger = configured(tmp_path)
        broker = DecisionsBroker(tmp_path, provider())
        original = native_request("advisory")
        await broker.native_batch(incoming(original))
        with ledger.engine.begin() as db:
            saved = json.loads(db.scalar(select(requests.c.result).where(requests.c.id == original["requestId"])))
            saved.pop("nativeTask")  # A persisted pre-protocol6 native receipt.
            db.execute(requests.update().values(result=canonical(saved)))
        closed = decoded(await broker.close_native_epoch(incoming(close_command().model_dump(mode="json"))))
        assert closed["unattributed_requests"] == 1
        target = native_request("advisory", index=2)
        target["scope"]["epoch"] = "e2"
        assert decoded(await broker.native_batch(incoming(target)))["fallback"] == "task_attribution_unavailable"
        assert ledger.status("seat")["requests"] == 1

    asyncio.run(run())


def test_native_paid_endpoints_require_a_nonnull_task_tag(tmp_path):
    async def run():
        configured(tmp_path)
        upstream = provider()
        broker = DecisionsBroker(tmp_path, upstream)
        async with server(broker.application()) as url, aiohttp.ClientSession() as session:
            for kind, endpoint in (("advisory", "/native/batch"), ("routing", "/native/routing")):
                for missing in (False, True):
                    value = native_request(kind)
                    value.pop("task") if missing else value.update(task=None)
                    async with session.post(
                        url + endpoint, json=value, headers={"Authorization": "Bearer secret"}
                    ) as response:
                        assert response.status == 400
        upstream.evaluate.assert_not_awaited()
        upstream.classify.assert_not_awaited()

    asyncio.run(run())


def test_same_task_different_inputs_reserve_once_across_connections(tmp_path):
    settings, ledger = configured(tmp_path)
    task = NativeAuxiliaryTask(kind="effort_selection", key=digest("actual-one-role-bootstrap"))

    def reserve(index):
        other = DecisionsLedger(tmp_path)
        supplied = evidence(index)
        body = request_body(supplied, settings)
        metadata = request_metadata(body, supplied)
        try:
            try:
                other.reserve(
                    "seat",
                    metadata["requestId"],
                    metadata["inputHash"],
                    body,
                    native_scope=NativeBrokerScope.from_scope(scope()),
                    native_task=task,
                )
                return "reserved"
            except NativeAuxiliaryTaskUnavailable as error:
                return error.reason
        finally:
            other.engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(reserve, (1, 2)))
    assert sorted(outcomes) == ["reserved", "task_allowance_exhausted"]
    assert ledger.status("seat")["requests"] == 1


def test_explicit_new_draft_is_a_new_task_even_with_identical_message_text(tmp_path):
    async def run():
        configured(tmp_path)
        upstream = provider()
        broker = DecisionsBroker(tmp_path, upstream)
        first = native_request("routing")
        first["task"] = NativeAuxiliaryTask(kind="outgoing_routing", key=digest("delivery-original")).model_dump(
            mode="json"
        )
        await broker.native_route(incoming(first))
        await broker.close_native_epoch(incoming(close_command().model_dump(mode="json")))
        second = native_request("routing", index=2)
        second["scope"]["epoch"] = "e2"
        second["task"] = NativeAuxiliaryTask(kind="outgoing_routing", key=digest("delivery-explicitly-new")).model_dump(
            mode="json"
        )
        assert first["submission"]["bundle"] == second["submission"]["bundle"]
        assert decoded(await broker.native_route(incoming(second)))["fallback"] == "service_failure"
        assert upstream.classify.await_count == 2

    asyncio.run(run())


def test_military_task_uses_existing_material_signature_not_transport_or_turn_counter(tmp_path):
    from civ_agent.military_schedule import triage_signature
    from tests.test_military_schedule import fixture

    schedule, view, state = fixture(tmp_path)
    try:
        assert schedule.complete(view, 4, state)
        view["economy"]["resources"] = [{"name": "Iron", "type": "Strategic", "available": 1}]
        status = schedule.assess(view, 5, state)
        original = schedule.triage_input(view, 5, state, status)
        assert original is not None
        material = original["militaryReview"]
        key = triage_signature(material["baseline"], material["current"], material["nextReviewTurn"])
        assert key == schedule.saved()["triage"]["signature"]
        view.update(epoch="replacement-transport", revision="new-CAS-revision", turn=6)
        assert schedule.triage_input(view, 6, state, status) == original
        assert schedule.saved()["triage"]["signature"] == key
        view["economy"]["resources"][0]["available"] = 2
        changed = schedule.triage_input(view, 6, state, schedule.assess(view, 6, state))
        assert changed is not None and changed != original
        assert schedule.saved()["triage"]["signature"] != key
    finally:
        schedule.memory.engine.dispose()


def test_authenticated_incoming_identity_survives_real_domain_notification_rebase(tmp_path):
    async def run():
        async with managed(tmp_path / "domain") as domain:
            _, artifacts, store, native, run, routing, _, _, ingest = await setup(domain, tmp_path)
            try:
                ingest([event(1), event(2)])
                plan = await native.plan(IncomingBatch(run=run, notices=store.pending()))
                original = plan.classifications[0]
                assert original.task is not None
                router = NativeRouting(
                    native.root, SimpleNamespace(backend=routing.settings.decisions_backend), routing.settings
                )
                original_bundle = artifacts.get(original.scope, original.inputs)["bundle"]
                source = router.freeze(
                    original.scope, original.operation_id, "incoming", original_bundle, task=original.task
                )
                domain.memory.pause(True)
                path = tmp_path / "checkpoint.json"
                checksum = domain.memory.backup(path)
                artifacts.bind_epoch("game", "g2", previous="g1")
                domain.memory.restore(path, checksum, "g2", notification_artifacts=artifacts)
                target_scope = communication_scope("game", "p0", "g2")
                target_store = CommunicationStore(domain.memory, artifacts, target_scope)
                native.communication.store = lambda _: target_store
                native.communication.check = native.root.check_owner = lambda _: None
                target = (
                    await native.plan(
                        IncomingBatch(
                            run=run.model_copy(update={"scope": target_scope}), notices=target_store.pending()
                        )
                    )
                ).classifications[0]
                assert target.scope.epoch == "g2" and target.operation_id != original.operation_id
                assert target.task == original.task
                with domain.memory.transaction() as db:
                    origins = (
                        db.execute(select(notifications.c.source).order_by(notifications.c.source)).scalars().all()
                    )
                assert origins == ["g1:1", "g1:2"]
                target_bundle = artifacts.get(target.scope, target.inputs)["bundle"]
                frozen = router.freeze(target.scope, target.operation_id, "incoming", target_bundle, task=target.task)
                assert frozen.operation_id == source.operation_id == "incoming-routing-" + original.task.key
            finally:
                artifacts.close()

    asyncio.run(run())
