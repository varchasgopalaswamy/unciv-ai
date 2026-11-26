"""Immutable routing dispatch and receipt recovery independent of advisory budgets."""

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from civ_agent.container_supervisor import authorize_agent
from civ_agent.decisions_backends import BACKENDS, DecisionBackend, provider_answers
from civ_agent.decisions_broker import DecisionsBroker, RoutingClient
from civ_agent.decisions_ledger import DecisionsLedger
from civ_agent.decisions_policy import DecisionsSettings, routing_thresholds
from civ_agent.diplomacy_decisions import DecisionsClient, fallback, parse_answer
from civ_agent.diplomacy_decisions import request_body as routing_body
from civ_agent.orchestration.routing_contracts import (
    routing_metadata,
    routing_submission,
    validate_routing_reply,
)
from civ_agent.protocol import JsonObject
from tests.test_decision_provider import response_body
from tests.test_decisions_broker import server


def configured(tmp_path, backend: DecisionBackend = "luna", **overrides):
    settings = DecisionsSettings(
        decisions_enabled=True, decisions_backend=backend, additional_decisions_budget_usd=0, **overrides
    )
    auth, seat = tmp_path / "auth", tmp_path / "seat"
    seat.mkdir()
    authorize_agent(auth, "game", 0, seat, decisions=True, settings=settings.model_dump(), player="Rome")
    return settings, auth, seat


def bundle():
    return {"schemaVersion": 2, "player": "Rome", "pendingMessages": [{"id": 1, "text": "Private message."}]}


def reply(backend, direction):
    names = (
        ("response_needed", "response_timing") if direction == "incoming" else ("delivery_needed", "delivery_timing")
    )
    rows = [
        {
            "name": name,
            "type": "choice",
            "choice": selected,
            "confidence": 0.4,
            "probabilities": [{"value": selected, "probability": 0.99}, {"value": other, "probability": 0.01}],
        }
        for name, selected, other in zip(names, ("needed", "can_wait"), ("not_needed", "now"), strict=True)
    ]
    return response_body(backend, rows)


def request(seat, submission):
    token = json.loads((seat / "decisions-client.json").read_text())["token"]
    return SimpleNamespace(
        headers={"Authorization": "Bearer " + token},
        json=AsyncMock(return_value=submission.model_dump(mode="json")),
    )


@pytest.mark.parametrize("backend", ["luna", "jev"])
@pytest.mark.parametrize("direction", ["incoming", "outgoing"])
def test_native_routing_cached_across_restart_accounts_once_without_spending_advisory_allowance(
    tmp_path, backend, direction
):
    async def run():
        settings, auth, seat = configured(tmp_path, backend)
        socket, calls = tmp_path / "broker.sock", []
        submission = routing_submission("native-operation", direction, bundle(), backend)

        async def provider(incoming):
            calls.append(await incoming.json())
            assert incoming.headers.get("Authorization") == (
                "Bearer synthetic-provider-key" if backend == "luna" else None
            )
            return web.json_response(reply(backend, direction))

        app = web.Application()
        endpoint = "/v1/decisions" if backend == "luna" else "/v1/systemone"
        app.router.add_post(endpoint, provider)
        async with server(app) as url:
            responses = []
            for _ in range(2):
                native = DecisionsClient(
                    "synthetic-provider-key" if backend == "luna" else None, backend=backend, endpoint=url + endpoint
                )
                broker = DecisionsBroker(auth, native)
                remote = RoutingClient(socket, seat / "decisions-client.json", backend=backend)
                async with server(broker.application(), socket=socket):
                    if responses:
                        assert await remote.lookup_route(submission.requestId) == responses[0]
                    first, duplicate = await asyncio.gather(
                        remote.route(submission.operationId, direction, submission.bundle),
                        remote.route(submission.operationId, direction, submission.bundle),
                    )
                    assert first == duplicate
                    assert validate_routing_reply(submission, first, settings) == first
                    assert first["choice"] == "batch" and first["confidence"] == 0.99
                    assert first["rawAnswers"][0]["providerConfidence"] == 0.4
                    assert first["routingAccounting"]["requests"] == 1
                    assert first["routingAccounting"]["unknownCompletions"] == 0
                    assert first["routingAccounting"]["unknownUsage"] == 0
                    assert first["routingAccounting"]["inputTokens"] == 125
                    assert first["routingAccounting"]["spentNanodollars"] == (
                        125 * BACKENDS[backend].nanodollars_per_input_token
                    )
                    assert broker.ledger.status("game-0")["requests"] == 0
                    assert broker.ledger.status("game-0")["spentNanodollars"] == 0
                    responses.append(first)
                broker.ledger.engine.dispose()
            assert responses[0] == responses[1] and len(calls) == 1

    asyncio.run(run())


def test_lost_routing_acknowledgement_recovers_the_committed_original_reply(tmp_path):
    class LostReplyBroker(DecisionsBroker):
        def _routing_reply(self, principal, result):
            raise web.HTTPServiceUnavailable()

    async def run():
        settings, auth, seat = configured(tmp_path)
        socket = tmp_path / "broker.sock"
        native = DecisionsClient("synthetic-provider-key")
        native.classify = AsyncMock(
            return_value={**fallback("outgoing", "service_failure"), "usage": {"input_tokens": 10}}
        )
        first = LostReplyBroker(auth, native)
        remote = RoutingClient(socket, seat / "decisions-client.json", backend="luna")
        submission = routing_submission("delivery-operation", "outgoing", bundle(), "luna")
        async with server(first.application(), socket=socket):
            lost = await remote.route(submission.operationId, submission.direction, submission.bundle)
            assert lost["fallback"] == "broker_unavailable"
        first.ledger.engine.dispose()
        restarted = DecisionsBroker(auth, native)
        async with server(restarted.application(), socket=socket):
            recovered = await remote.lookup_route(submission.requestId)
            assert recovered["fallback"] == "service_failure" and recovered["choice"] == "send_now"
            assert recovered["routingAccounting"]["inputTokens"] == 10
            assert validate_routing_reply(submission, recovered, settings) == recovered
            assert await remote.route(submission.operationId, submission.direction, submission.bundle) == recovered
        assert native.classify.await_count == 1

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["luna", "jev"])
def test_cancelled_or_crashed_provider_attempt_keeps_unknown_receipt_and_never_dispatches_again(tmp_path, backend):
    async def run():
        settings, auth, seat = configured(tmp_path, backend)
        native = AsyncMock()
        native.classify.side_effect = asyncio.CancelledError
        submission = routing_submission("interrupted-operation", "incoming", bundle(), backend)
        original = DecisionsBroker(auth, native)
        with pytest.raises(asyncio.CancelledError):
            await original.route(request(seat, submission))
        original.ledger.engine.dispose()
        replacement = DecisionsBroker(auth, native)
        socket = tmp_path / "broker.sock"
        remote = RoutingClient(socket, seat / "decisions-client.json", backend=backend)
        async with server(replacement.application(), socket=socket):
            recovered = await remote.lookup_route(submission.requestId)
            duplicate = await remote.route(submission.operationId, submission.direction, submission.bundle)
        assert recovered == duplicate
        assert recovered["fallback"] == "completion_unknown" and recovered["choice"] == "respond_now"
        assert recovered["routingAccounting"]["unknownCompletions"] == 1
        assert recovered["routingAccounting"]["unknownUsage"] == 1
        assert validate_routing_reply(submission, recovered, settings) == recovered
        assert native.classify.await_count == 1

    asyncio.run(run())


def test_routing_lookup_is_free_and_identity_player_settings_and_auth_are_enforced(tmp_path):
    async def run():
        settings, auth, seat = configured(tmp_path, decisions_outgoing_batch_probability=0.999)
        native = AsyncMock()
        native.classify.return_value = {**fallback("outgoing", "timeout"), "usage": {}}
        broker = DecisionsBroker(auth, native)
        first = routing_submission("one-operation", "outgoing", bundle(), "luna")
        socket = tmp_path / "broker.sock"
        remote = RoutingClient(socket, seat / "decisions-client.json", backend="luna")
        async with server(broker.application(), socket=socket):
            assert (await remote.lookup_route(first.requestId))["fallback"] == "not_submitted"
            native.classify.assert_not_awaited()
            response = await broker.route(request(seat, first))
            assert isinstance(response.body, bytes)
            saved = json.loads(response.body)
            assert saved["thresholds"]["batch"] == 0.999
            assert native.classify.call_args.kwargs["thresholds"]["batch"] == 0.999
            assert native.classify.call_args.kwargs["timeout"] == settings.decisions_timeout_seconds
            changed = routing_submission("one-operation", "outgoing", {**bundle(), "new": True}, "luna")
            with pytest.raises(web.HTTPConflict):
                await broker.route(request(seat, changed))
            for invalid in (
                routing_submission("bad-player", "outgoing", {**bundle(), "player": "Greece"}, "luna"),
                first.model_copy(update={"requestId": "0" * 64}),
                first.model_copy(update={"version": "unsupported"}),
            ):
                with pytest.raises(web.HTTPBadRequest):
                    await broker.route(request(seat, invalid))
            unauthenticated = request(seat, first)
            unauthenticated.headers = {"Authorization": "Bearer wrong"}
            with pytest.raises(web.HTTPUnauthorized):
                await broker.route(unauthenticated)
        assert native.classify.await_count == 1
        assert broker.ledger.routing_status("game-0")["unknownUsage"] == 1

    asyncio.run(run())


def test_distinct_broker_instances_atomically_reserve_one_routing_dispatch(tmp_path):
    settings, auth, _ = configured(tmp_path)
    submission = routing_submission("shared-operation", "incoming", bundle(), "luna")
    body = routing_body(submission.direction, submission.bundle, backend="luna")
    pending = {**routing_metadata(submission, settings), **fallback("incoming", "completion_unknown")}

    def reserve(_):
        ledger = DecisionsLedger(auth)
        try:
            return ledger.routing_reserve("game-0", submission.operationId, submission.requestId, body, pending)
        finally:
            ledger.engine.dispose()

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(reserve, range(8)))
    assert sum(result is None for result in results) == 1
    assert all(result is None or result["fallback"] == "completion_unknown" for result in results)


@pytest.mark.parametrize("mutation", ["choice", "confidence", "thresholds", "model", "policyHash", "rawAnswers"])
def test_untrusted_routing_receipt_cannot_grant_delay_or_suppression(tmp_path, mutation):
    async def run():
        settings, auth, seat = configured(tmp_path)
        native = DecisionsClient("synthetic-provider-key")
        raw = reply("luna", "outgoing")

        normalized = provider_answers(routing_body("outgoing", bundle(), backend="luna"), raw, "luna")
        native.classify = AsyncMock(
            return_value={
                **parse_answer(
                    "outgoing", {"answers": normalized}, thresholds=routing_thresholds(settings, "outgoing")
                ),
                "usage": {},
            }
        )
        broker = DecisionsBroker(auth, native)
        submission = routing_submission("checked-operation", "outgoing", bundle(), "luna")
        response = await broker.route(request(seat, submission))
        assert isinstance(response.body, bytes)
        valid: JsonObject = json.loads(response.body)
        assert validate_routing_reply(submission, valid, settings)["choice"] == "batch"
        changed = copy.deepcopy(valid)
        changed[mutation] = {"choice": "omit", "confidence": 1, "thresholds": {}, "rawAnswers": []}.get(
            mutation, "wrong"
        )
        checked = validate_routing_reply(submission, changed, settings)
        assert checked["choice"] == "send_now" and checked["fallback"] == "malformed_broker_output"

    asyncio.run(run())
