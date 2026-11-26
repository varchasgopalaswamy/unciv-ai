"""Authenticated epoch admission and conservative recovery with synthetic providers."""

import asyncio
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from civ_agent.decisions_backends import BACKENDS
from civ_agent.decisions_broker import BROKER_PROTOCOL_VERSION, DecisionsBroker, RoutingClient
from civ_agent.decisions_ledger import DecisionsLedger, NativeBrokerEpochFenced, fingerprint
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import request_body, request_metadata
from civ_agent.decisions_tasks import request_id as task_request_id
from civ_agent.diplomacy_decisions import fallback
from civ_agent.native_broker_contracts import CloseNativeBrokerEpoch, NativeBrokerScope
from civ_agent.orchestration.contracts import NativeAuxiliaryTask, Scope, digest
from civ_agent.orchestration.managed_checkpoints import GameCheckpointReference, HostCheckpointRequest
from civ_agent.orchestration.managed_retirement import ManagedRetirementIntent
from civ_agent.orchestration.routing_contracts import routing_submission
from civ_agent.tls import write_private_json
from tests.test_decisions_broker import server


def configured(path, *, budget=1):
    settings = DecisionsSettings(
        decisions_enabled=True, decisions_backend="luna", additional_decisions_budget_usd=budget
    )
    ledger = DecisionsLedger(path)
    ledger.register("seat", "game", "Rome", settings.model_dump(), native_player="p0")
    ledger.register("legacy", "other-game", "Greece", settings.model_dump())
    write_private_json(
        path / "decisions-clients.json",
        {"seat": hashlib.sha256(b"secret").hexdigest(), "legacy": hashlib.sha256(b"legacy").hexdigest()},
    )
    write_private_json(path / "token.json", {"token": "secret"})
    return settings, ledger


def scope(epoch="e1"):
    return Scope(game="game", player="p0", epoch=epoch, activation="classifier")


def close_command():
    return CloseNativeBrokerEpoch(
        scope=NativeBrokerScope.from_scope(scope()),
        owner=ManagedRetirementIntent(
            game="game",
            source_epoch="e1",
            target_epoch="e2",
            source_run_id="source-first-run",
            recovery_run_id="recovery-first-run",
            checkpoint=GameCheckpointReference(
                request=HostCheckpointRequest(game="game", epoch="e1", id="starting", turn=0, handoff=0),
                sha256=digest("synthetic-checkpoint"),
            ),
        ),
    )


def evidence(index=1):
    return {"player": "Rome", "preservation": [{"key": f"event:{index}", "version": 1}]}


def request_id(value):
    return task_request_id(value, model=BACKENDS["luna"].model)


def auxiliary(kind="advisory", index=1):
    return NativeAuxiliaryTask(
        kind="military_triage" if kind == "advisory" else "outgoing_routing", key=digest([kind, index])
    )


def bundle():
    return {"schemaVersion": 2, "player": "Rome", "pendingMessages": [{"id": 1, "text": "Private claim."}]}


def incoming(payload, token="secret"):
    return SimpleNamespace(headers={"Authorization": "Bearer " + token}, json=AsyncMock(return_value=payload))


def native_request(kind, *, index=1) -> dict[str, Any]:
    trusted = NativeBrokerScope.from_scope(scope()).model_dump(mode="json")
    if kind == "advisory":
        return {
            "scope": trusted,
            "task": auxiliary(kind, index).model_dump(mode="json"),
            "requestId": request_id(evidence(index)),
            "evidence": evidence(index),
        }
    return {
        "scope": trusted,
        "task": auxiliary(kind, index).model_dump(mode="json"),
        "submission": routing_submission(f"operation-{index}", "outgoing", bundle(), "luna").model_dump(mode="json"),
    }


def decoded(response: web.Response):
    assert isinstance(response.body, bytes)
    return json.loads(response.body)


def provider():
    return SimpleNamespace(
        evaluate=AsyncMock(return_value={"fallback": "service_failure", "usage": {"input_tokens": 100}}),
        classify=AsyncMock(return_value={**fallback("outgoing", "service_failure"), "usage": {"input_tokens": 100}}),
    )


async def wait_closed(ledger):
    async def observe():
        while True:
            try:
                ledger.native_dispatch_open("seat", NativeBrokerScope.from_scope(scope()))
            except NativeBrokerEpochFenced:
                return
            await asyncio.sleep(0)

    await asyncio.wait_for(observe(), 3)


def test_native_principal_cannot_use_legacy_or_foreign_scope(tmp_path):
    async def run():
        _, ledger = configured(tmp_path)
        upstream = provider()
        broker = DecisionsBroker(tmp_path, upstream)
        async with server(broker.application()) as url, aiohttp.ClientSession() as session:
            headers = {"Authorization": "Bearer secret"}
            for endpoint, payload in (
                ("/batch", {"requestId": request_id(evidence()), "evidence": evidence()}),
                ("/routing", native_request("routing")["submission"]),
                ("/classify", {"direction": "outgoing", "bundle": bundle()}),
            ):
                async with session.post(url + endpoint, json=payload, headers=headers) as response:
                    assert response.status == 403
            for mutation in ({"game": "foreign"}, {"player": "p1"}, {"player": "Rome"}):
                payload = native_request("advisory")
                payload["scope"].update(mutation)
                async with session.post(url + "/native/batch", json=payload, headers=headers) as response:
                    assert response.status == 400
            async with session.post(
                url + "/native/batch",
                json=native_request("advisory"),
                headers={"Authorization": "Bearer legacy"},
            ) as response:
                assert response.status == 400
        upstream.evaluate.assert_not_awaited()
        upstream.classify.assert_not_awaited()
        assert ledger.status("seat")["requests"] == 0
        with pytest.raises(ValueError, match="owner conflict"):
            ledger.register("seat", "game", "Rome", ledger.settings("seat").model_dump(), native_player="p1")
        # A later legacy registration cannot remove native ownership.
        ledger.register("seat", "game", "Rome", ledger.settings("seat").model_dump())
        owner = ledger.native_owner("seat")
        assert owner is not None and owner["player"] == "p0"
        assert BROKER_PROTOCOL_VERSION == 6

    asyncio.run(run())


def test_existing_legacy_account_cannot_be_promoted_around_inflight_provider(tmp_path):
    async def run():
        settings, ledger = configured(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        upstream = provider()

        async def accepted(*args, **kwargs):
            entered.set()
            await release.wait()
            return fallback("outgoing", "service_failure")

        upstream.classify.side_effect = accepted
        broker = DecisionsBroker(tmp_path, upstream)
        active = asyncio.create_task(broker.classify(incoming({"direction": "outgoing", "bundle": {}}, token="legacy")))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with pytest.raises(ValueError, match="cannot promote"):
                ledger.register("legacy", "other-game", "Greece", settings.model_dump(), native_player="p1")
            assert ledger.native_owner("legacy") is None
            release.set()
            assert decoded(await asyncio.wait_for(active, 3))["fallback"] == "service_failure"
        finally:
            release.set()
            await asyncio.gather(active, return_exceptions=True)

    asyncio.run(run())


def test_closed_epoch_keeps_free_original_receipts_and_original_admission_provenance(tmp_path):
    async def run():
        _, ledger = configured(tmp_path)
        upstream = provider()
        broker = DecisionsBroker(tmp_path, upstream)
        socket = tmp_path / "broker.sock"
        client = RoutingClient(socket, tmp_path / "token.json", backend="luna")
        async with server(broker.application(), socket=socket):
            first = await client.batch(evidence(), scope=scope(), task=auxiliary())
            route = await client.route("operation-1", "outgoing", bundle(), scope=scope(), task=auxiliary("routing"))
            assert first["fallback"] == route["fallback"] == "service_failure"
            assert "nativeScope" not in first and "nativeScope" not in route
            closed = await client.close_epoch(close_command())
            assert closed.owner_sha256 == digest(close_command().owner.model_dump(mode="json"))
            assert (closed.advisory_requests, closed.routing_requests) == (1, 1)
            assert closed.advisory_unknown_completions == closed.routing_unknown_completions == 0
            before = ledger.status("seat")
            assert await client.lookup(request_id(evidence())) == first
            assert await client.batch(evidence(), scope=scope(), task=auxiliary()) == first
            assert await client.batch(evidence(), scope=scope("e2"), task=auxiliary()) == first
            assert (
                await client.route("operation-1", "outgoing", bundle(), scope=scope("e2"), task=auxiliary("routing"))
                == route
            )
            assert ledger.status("seat") == before
            assert (await client.batch(evidence(2), scope=scope(), task=auxiliary(index=2)))[
                "fallback"
            ] == "source_epoch_closed"
            assert (
                await client.route("operation-2", "outgoing", bundle(), scope=scope(), task=auxiliary("routing", 2))
            )["fallback"] == "source_epoch_closed"
            assert ledger.status("seat") == before
            assert (await client.batch(evidence(2), scope=scope("e2"), task=auxiliary(index=2)))[
                "fallback"
            ] == "service_failure"
            assert (
                NativeBrokerScope.model_validate(ledger.lookup("seat", request_id(evidence()))["nativeScope"]).epoch
                == "e1"
            )
            route_id = routing_submission("operation-1", "outgoing", bundle(), "luna").requestId
            assert (
                NativeBrokerScope.model_validate(ledger.routing_lookup("seat", route_id)["nativeScope"]).epoch == "e1"
            )
        assert upstream.evaluate.await_count == 2 and upstream.classify.await_count == 1

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["advisory", "routing"])
def test_fence_persists_before_cross_instance_drain_then_settles_accepted_call(tmp_path, kind):
    async def run():
        _, ledger = configured(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        upstream = provider()

        async def accepted(*args, **kwargs):
            entered.set()
            await release.wait()
            return {**fallback("outgoing", "service_failure"), "usage": {"input_tokens": 100}}

        getattr(upstream, "evaluate" if kind == "advisory" else "classify").side_effect = accepted
        first, replacement = DecisionsBroker(tmp_path, upstream), DecisionsBroker(tmp_path, upstream)
        submit = first.native_batch if kind == "advisory" else first.native_route
        active = asyncio.create_task(submit(incoming(native_request(kind))))
        closing = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            closing = asyncio.create_task(
                replacement.close_native_epoch(incoming(close_command().model_dump(mode="json")))
            )
            await wait_closed(ledger)
            assert not active.done() and not closing.done()
            release.set()
            await asyncio.wait_for(active, 3)
            closed = decoded(await asyncio.wait_for(closing, 3))
            assert closed["local_drain_complete"] is True
            assert closed[f"{kind}_requests"] == 1
            assert closed[f"{kind}_unknown_completions"] == 0
            result = decoded(await submit(incoming(native_request(kind, index=2))))
            assert result["fallback"] == "source_epoch_closed"
            assert getattr(upstream, "evaluate" if kind == "advisory" else "classify").await_count == 1
        finally:
            release.set()
            await asyncio.gather(active, *([closing] if closing else []), return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["advisory", "routing"])
def test_cancelled_unknown_attempt_survives_reopen_and_close_without_redispatch(tmp_path, kind):
    async def run():
        _, ledger = configured(tmp_path)
        upstream = provider()
        method = getattr(upstream, "evaluate" if kind == "advisory" else "classify")
        method.side_effect = asyncio.CancelledError
        original = DecisionsBroker(tmp_path, upstream)
        with pytest.raises(asyncio.CancelledError):
            await (original.native_batch if kind == "advisory" else original.native_route)(
                incoming(native_request(kind))
            )
        original.ledger.engine.dispose()
        replacement = DecisionsBroker(tmp_path, upstream)
        closed = decoded(await replacement.close_native_epoch(incoming(close_command().model_dump(mode="json"))))
        assert closed["local_drain_complete"] is True
        assert closed[f"{kind}_unknown_completions"] == 1
        response = await (replacement.native_batch if kind == "advisory" else replacement.native_route)(
            incoming(native_request(kind))
        )
        assert decoded(response)["fallback"] == "completion_unknown"
        assert method.await_count == 1
        if kind == "advisory":
            assert ledger.status("seat")["reservedNanodollars"] > 0
        else:
            assert closed["routing_unknown_usage"] == 1

    asyncio.run(run())


def test_provider_boundary_rechecks_fence_after_reservation_without_inventing_usage(tmp_path, monkeypatch):
    async def run():
        _, ledger = configured(tmp_path)
        upstream = provider()
        broker = DecisionsBroker(tmp_path, upstream)
        real_check = broker.ledger.native_dispatch_open

        def close_before_dispatch(principal, trusted):
            ledger.close_native_epoch("seat", close_command())
            real_check(principal, trusted)

        monkeypatch.setattr(broker.ledger, "native_dispatch_open", close_before_dispatch)
        result = decoded(await broker.native_batch(incoming(native_request("advisory"))))
        assert result["fallback"] == "source_epoch_closed"
        upstream.evaluate.assert_not_awaited()
        assert ledger.status("seat")["reservedNanodollars"] > 0
        closed = ledger.native_epoch_status("seat", close_command())
        assert closed.advisory_unknown_completions == 1

    asyncio.run(run())


def test_cancelled_close_keeps_permanent_fence_and_retry_joins_original_provider(tmp_path):
    async def run():
        _, ledger = configured(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        upstream = provider()

        async def accepted(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"fallback": "service_failure", "usage": {"input_tokens": 100}}

        upstream.evaluate.side_effect = accepted
        broker, replacement = DecisionsBroker(tmp_path, upstream), DecisionsBroker(tmp_path, upstream)
        active = asyncio.create_task(broker.native_batch(incoming(native_request("advisory"))))
        closing = retry = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            closing = asyncio.create_task(broker.close_native_epoch(incoming(close_command().model_dump(mode="json"))))
            await wait_closed(ledger)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            with pytest.raises(NativeBrokerEpochFenced):
                ledger.native_dispatch_open("seat", NativeBrokerScope.from_scope(scope()))
            retry = asyncio.create_task(
                replacement.close_native_epoch(incoming(close_command().model_dump(mode="json")))
            )
            await asyncio.sleep(0)
            assert not retry.done()
            release.set()
            await asyncio.wait_for(active, 3)
            assert decoded(await asyncio.wait_for(retry, 3))["advisory_unknown_completions"] == 0
            assert upstream.evaluate.await_count == 1
        finally:
            release.set()
            await asyncio.gather(
                active, *[task for task in (closing, retry) if task is not None], return_exceptions=True
            )

    asyncio.run(run())


def test_native_advisory_ceiling_does_not_add_a_routing_ceiling(tmp_path):
    async def run():
        _, ledger = configured(tmp_path, budget=0)
        upstream = provider()
        broker = DecisionsBroker(tmp_path, upstream)
        assert (
            decoded(await broker.native_batch(incoming(native_request("advisory"))))["fallback"] == "budget_exhausted"
        )
        assert decoded(await broker.native_route(incoming(native_request("routing"))))["fallback"] == "service_failure"
        upstream.evaluate.assert_not_awaited()
        upstream.classify.assert_awaited_once()
        assert ledger.status("seat")["ceilingNanodollars"] == 0
        assert ledger.routing_status("seat")["inputTokens"] == 100

    asyncio.run(run())


def test_close_client_rejects_swapped_owner_and_never_returns_success_for_outage(tmp_path):
    async def run():
        _, ledger = configured(tmp_path)
        command = close_command()
        ledger.close_native_epoch("seat", command)
        proof = ledger.native_epoch_status("seat", command).model_dump(mode="json")
        proof["owner_sha256"] = "f" * 64

        async def wrong_owner(request):
            assert request.headers["Authorization"] == "Bearer secret"
            return web.json_response(proof)

        app = web.Application()
        app.router.add_post("/native/epochs/close", wrong_owner)
        socket = tmp_path / "broker.sock"
        client = RoutingClient(socket, tmp_path / "token.json", backend="luna")
        async with server(app, socket=socket):
            with pytest.raises(ValueError, match="close response conflict"):
                await client.close_epoch(command)
        with pytest.raises((aiohttp.ClientError, OSError)):
            await client.close_epoch(command)

    asyncio.run(run())


def test_reservation_and_close_are_atomic_across_connections(tmp_path):
    settings, ledger = configured(tmp_path)
    trusted = NativeBrokerScope.from_scope(scope())
    body = request_body(evidence(), settings)
    metadata = request_metadata(body, evidence())

    def reserve():
        other = DecisionsLedger(tmp_path)
        try:
            try:
                other.reserve(
                    "seat",
                    metadata["requestId"],
                    metadata["inputHash"],
                    body,
                    native_scope=trusted,
                    native_task=auxiliary(),
                )
                return "admitted"
            except NativeBrokerEpochFenced:
                return "closed"
        finally:
            other.engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reservation = executor.submit(reserve)
        fence = executor.submit(ledger.close_native_epoch, "seat", close_command())
        outcome, _ = reservation.result(timeout=5), fence.result(timeout=5)
    with pytest.raises(NativeBrokerEpochFenced):
        ledger.reserve("seat", "f" * 64, "e" * 64, body, native_scope=trusted, native_task=auxiliary())
    assert ledger.status("seat")["requests"] == (1 if outcome == "admitted" else 0)
    changed = close_command().model_copy(
        update={"owner": close_command().owner.model_copy(update={"recovery_run_id": "another"})}
    )
    with pytest.raises(ValueError, match="owner conflict"):
        ledger.close_native_epoch("seat", changed)


def test_cross_process_admission_lock_preserves_fence_before_waiting_request(tmp_path):
    async def run():
        _, ledger = configured(tmp_path)
        upstream = provider()
        broker = DecisionsBroker(tmp_path, upstream)
        trusted = NativeBrokerScope.from_scope(scope())
        directory = tmp_path / "native-epoch-locks"
        directory.mkdir(mode=0o700)
        path = directory / (fingerprint(["seat", trusted.model_dump(mode="json")]) + ".lock")
        code = "from filelock import FileLock; import sys; lock=FileLock(sys.argv[1],mode=0o600); lock.acquire(); print('LOCKED',flush=True); sys.stdin.readline(); lock.release()"
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, str(path), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
        )
        submit = closing = None
        try:
            assert process.stdout is not None and process.stdin is not None
            assert await asyncio.wait_for(process.stdout.readline(), 3) == b"LOCKED\n"
            submit = asyncio.create_task(broker.native_batch(incoming(native_request("advisory"))))
            closing = asyncio.create_task(broker.close_native_epoch(incoming(close_command().model_dump(mode="json"))))
            await wait_closed(ledger)
            assert not closing.done()
            process.stdin.write(b"release\n")
            await process.stdin.drain()
            assert await asyncio.wait_for(process.wait(), 3) == 0
            assert decoded(await asyncio.wait_for(submit, 3))["fallback"] == "source_epoch_closed"
            await asyncio.wait_for(closing, 3)
            upstream.evaluate.assert_not_awaited()
            assert ledger.status("seat")["requests"] == 0
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            for task in (submit, closing):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*[task for task in (submit, closing) if task is not None], return_exceptions=True)

    asyncio.run(run())
