import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from civ_agent.decisions_broker import DecisionsBroker, RoutingClient
from civ_agent.decisions_ledger import accounts, canonical
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.diplomacy_decisions import DecisionsClient, parse_answer
from civ_agent.tls import write_private_json


@asynccontextmanager
async def server(app, *, socket=None):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, str(socket)) if socket else web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield "http://localhost" if socket else "http://127.0.0.1:" + str(runner.addresses[0][1])
    finally:
        await runner.cleanup()
        if socket:
            socket.unlink(missing_ok=True)


def test_client_request_no_retry_and_temporary_circuit():
    async def run():
        calls, clock = [], [0.0]

        async def classify(request):
            calls.append((request.headers.get("Authorization"), await request.json()))
            if len(calls) <= 3:
                raise web.HTTPServiceUnavailable()
            return web.json_response(
                {
                    "answers": [
                        {
                            "name": "delivery_needed",
                            "type": "choice",
                            "choice": "needed",
                            "confidence": 0.98,
                            "probabilities": [
                                {"value": "needed", "probability": 0.99},
                                {"value": "not_needed", "probability": 0.01},
                            ],
                        },
                        {
                            "name": "delivery_timing",
                            "type": "choice",
                            "choice": "can_wait",
                            "confidence": 0.98,
                            "probabilities": [
                                {"value": "can_wait", "probability": 0.99},
                                {"value": "now", "probability": 0.01},
                            ],
                        },
                    ],
                    "usage": {"input_tokens": 15, "output_tokens": 1},
                }
            )

        app = web.Application()
        app.router.add_post("/v1/decisions", classify)
        async with server(app) as url:
            client = DecisionsClient("separate-test-key", endpoint=url + "/v1/decisions", clock=lambda: clock[0])
            for _ in range(3):
                assert (await client.classify("outgoing", {}))["fallback"] == "service_failure"
            assert len(calls) == 3  # one HTTP request per classification, never automatic retries
            assert (await client.classify("outgoing", {}))["fallback"] == "circuit_open"
            assert len(calls) == 3
            clock[0] = 31
            result = await client.classify("outgoing", {"text": "a claim"})
            assert result["choice"] == "batch" and result["usage"]["input_tokens"] == 15
            assert calls[-1][0] == "Bearer separate-test-key"
            assert calls[-1][1]["model"] == "gpt-6-luna"
            assert isinstance(calls[-1][1]["input"], str)

    asyncio.run(run())


def test_broker_auth_schema_and_single_inflight_per_credential(tmp_path):
    async def run():
        write_private_json(tmp_path / "decisions-clients.json", {"seat": hashlib.sha256(b"routing-token").hexdigest()})
        active, peak = 0, 0

        async def classify(direction, bundle, **options):
            nonlocal active, peak
            assert options["policy_version"] == "diplomacy-routing-v4"
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(0.02)
            active -= 1
            return {"choice": "send_now", "confidence": 1}

        client = AsyncMock()
        client.classify.side_effect = classify
        broker = DecisionsBroker(tmp_path, client)
        broker.ledger.register("seat", "game", "Rome", DecisionsSettings(decisions_enabled=True).model_dump())
        async with server(broker.application()) as url, aiohttp.ClientSession() as session:
            body = {"direction": "outgoing", "bundle": {"text": "private claim"}}
            for token in (None, "Bearer ", "Bearer provider-token", "routing-token"):
                async with session.post(
                    url + "/classify", json=body, headers={} if token is None else {"Authorization": token}
                ) as response:
                    assert response.status == 401
            headers = {"Authorization": "Bearer routing-token"}
            for invalid in ({**body, "api_key": "injection"}, {**body, "direction": []}, {**body, "bundle": []}):
                async with session.post(url + "/classify", json=invalid, headers=headers) as response:
                    assert response.status == 400
            client.classify.assert_not_awaited()

            async def call():
                async with session.post(url + "/classify", json=body, headers=headers) as response:
                    assert response.status == 200
                    return await response.json()

            results = await asyncio.gather(call(), call())
            assert peak == 1 and client.classify.await_count == 2
            assert all("key" not in json.dumps(result) and "token" not in json.dumps(result) for result in results)

    asyncio.run(run())


def test_unix_client_outage_and_malformed_choices_fail_open(tmp_path):
    async def run():
        write_private_json(tmp_path / "token.json", {"token": "test"})
        client = RoutingClient(tmp_path / "absent.sock", tmp_path / "token.json")
        result = await client.classify("incoming", {})
        assert result["choice"] == "respond_now" and result["fallback"] == "broker_unavailable"

    asyncio.run(run())
    for choice in ([], {}, None, True):
        result = parse_answer(
            "outgoing", {"answers": [{"name": "delivery", "type": "choice", "choice": choice, "confidence": 1}]}
        )
        assert result["choice"] == "send_now" and result["fallback"] == "malformed"


def test_authenticated_unix_client_preserves_bundle_and_reconnects_after_restart(tmp_path):
    async def run():
        token = "separate-routing-credential"
        write_private_json(tmp_path / "decisions-clients.json", {"Rome": hashlib.sha256(token.encode()).hexdigest()})
        write_private_json(tmp_path / "credential.json", {"token": token})
        write_private_json(tmp_path / "wrong.json", {"token": "wrong-routing-credential"})
        socket = tmp_path / "broker.sock"
        routing = RoutingClient(socket, tmp_path / "credential.json")
        rejected = RoutingClient(socket, tmp_path / "wrong.json")
        bundle = {
            "audience": ["Greece", "Rome"],
            "text": "You said ‘next turn’. Do you still intend to send the offer?",
            "history": [{"speaker": "Greece", "text": "I will send it next turn."}],
        }
        calls = []

        class Upstream:
            @staticmethod
            def health_snapshot():
                return {
                    "upstreamStatus": "unchecked",
                    "circuit": {"state": "closed", "consecutiveFailures": 0, "retryAfterSeconds": 0},
                    "lastFailure": None,
                }

            async def classify(self, direction, supplied, **options):
                assert options["policy_version"] == "diplomacy-routing-v4"
                calls.append((direction, supplied))
                return {"choice": "batch", "confidence": 0.99}

        upstream = Upstream()
        for _ in range(2):
            broker = DecisionsBroker(tmp_path, upstream)
            broker.ledger.register("Rome", "game", "Rome", DecisionsSettings(decisions_enabled=True).model_dump())
            async with server(broker.application(), socket=socket):
                assert (await routing.health())["status"] == "ready"
                before = len(calls)
                assert (await rejected.health())["reason"] == "broker_access_denied"
                rejected_result = await rejected.classify("outgoing", bundle)
                assert rejected_result["choice"] == "send_now"
                assert rejected_result["fallback"] == "broker_access_denied"
                assert len(calls) == before
                assert await routing.classify("outgoing", bundle) == {"choice": "batch", "confidence": 0.99}
                assert calls[-1] == ("outgoing", bundle)
            assert (await routing.health())["reason"] == "broker_unavailable"
        assert calls == [("outgoing", bundle)] * 2

    asyncio.run(run())


@pytest.mark.parametrize("account", ["unregistered", "disabled", "old_policy", "incomplete"])
def test_classify_requires_registered_enabled_current_account_without_default_policy(tmp_path, account):
    async def run():
        write_private_json(tmp_path / "decisions-clients.json", {"seat": hashlib.sha256(b"routing-token").hexdigest()})
        client = AsyncMock()
        broker = DecisionsBroker(tmp_path, client)
        if account != "unregistered":
            settings = DecisionsSettings(decisions_enabled=account != "disabled").model_dump()
            broker.ledger.register("seat", "game", "Rome", settings)
            if account == "old_policy":
                settings["decisions_routing_policy"] = "diplomacy-routing-v1"
            if account == "incomplete":
                del settings["decisions_incoming_batch_probability"]
            with broker.ledger.engine.begin() as db:
                db.execute(accounts.update().where(accounts.c.principal == "seat").values(settings=canonical(settings)))
        async with server(broker.application()) as url, aiohttp.ClientSession() as session:
            async with session.post(
                url + "/classify",
                headers={"Authorization": "Bearer routing-token"},
                json={"direction": "incoming", "bundle": {"text": "private claim"}},
            ) as response:
                assert response.status == 403
            client.classify.assert_not_awaited()

    asyncio.run(run())
