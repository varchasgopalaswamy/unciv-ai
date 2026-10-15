import asyncio
import hashlib
import json
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from civ_agent.decisions_broker import BROKER_PROTOCOL_VERSION, DecisionsBroker, RoutingClient
from civ_agent.decisions_ledger import RATE_VERSION, DecisionsLedger
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import TASK_VERSION
from civ_agent.diplomacy_decisions import MODEL, POLICY_VERSION, DecisionsClient
from civ_agent.tls import write_private_json


@asynccontextmanager
async def serve(app, *, socket=None):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, str(socket)) if socket else web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield "http://localhost" if socket else "http://127.0.0.1:" + str(runner.addresses[0][1])
    finally:
        await runner.cleanup()


def health_body():
    return {
        "protocolVersion": BROKER_PROTOCOL_VERSION,
        "classifierModel": MODEL,
        "routingPolicyVersion": POLICY_VERSION,
        "advisoryPolicyVersion": TASK_VERSION,
        "accountingRateVersion": RATE_VERSION,
        "status": "ready",
        "upstreamStatus": "unchecked",
        "circuit": {"state": "closed", "consecutiveFailures": 0, "retryAfterSeconds": 0},
        "lastFailure": None,
    }


def test_health_is_authenticated_free_and_reports_failure_circuit_recovery(tmp_path):
    async def run():
        calls, clock = [], [0.0]

        async def provider(request):
            calls.append(await request.json())
            if len(calls) <= 3:
                return web.Response(status=401, text="provider secret: forbidden-api-key and private player text")
            return web.json_response(
                {"answers": [{"name": "delivery", "type": "choice", "choice": "batch", "confidence": 0.99}]}
            )

        provider_app = web.Application()
        provider_app.router.add_post("/decisions", provider)
        write_private_json(tmp_path / "decisions-clients.json", {"seat": hashlib.sha256(b"player-secret").hexdigest()})
        write_private_json(tmp_path / "credential.json", {"token": "player-secret"})
        DecisionsLedger(tmp_path).register(
            "seat", "game", "Rome", DecisionsSettings(decisions_enabled=True).model_dump()
        )
        socket = tmp_path / "broker.sock"
        async with serve(provider_app) as upstream:
            decisions = DecisionsClient("forbidden-api-key", endpoint=upstream + "/decisions", clock=lambda: clock[0])
            async with serve(DecisionsBroker(tmp_path, decisions).application(), socket=socket):
                routing = RoutingClient(socket, tmp_path / "credential.json")
                assert await routing.health() == health_body()
                assert calls == []  # preflight must never spend a request or submit player information
                async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket))) as session:
                    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer "}):
                        async with session.get("http://localhost/health", headers=headers) as response:
                            assert response.status == 401
                assert calls == []
                for index in range(3):
                    result = await routing.classify("incoming", {"text": "private player text"})
                    assert result["choice"] == "respond_now" and result["fallback"] == "service_failure"
                    assert result["providerError"] == {"kind": "authentication", "httpStatus": 401}
                    status = await routing.health()
                    assert status["status"] == "ready" and status["upstreamStatus"] == "unavailable"
                    assert status["circuit"]["consecutiveFailures"] == index + 1
                    assert status["lastFailure"] == {"kind": "authentication", "httpStatus": 401}
                    serialized = json.dumps(status)
                    assert all(secret not in serialized for secret in ("forbidden-api-key", "player-secret", "private"))
                assert status["circuit"] == {"state": "open", "consecutiveFailures": 3, "retryAfterSeconds": 30}
                assert (await routing.classify("outgoing", {}))["fallback"] == "circuit_open"
                assert len(calls) == 3
                clock[0] = 31
                assert (await routing.health())["circuit"]["state"] == "closed"
                assert len(calls) == 3
                assert (await routing.classify("outgoing", {}))["providerError"] is None
                status = await routing.health()
                assert status["upstreamStatus"] == "available"
                assert status["circuit"] == {"state": "closed", "consecutiveFailures": 0, "retryAfterSeconds": 0}
                assert len(calls) == 4

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "kind"),
    [(401, "authentication"), (403, "access_denied"), (429, "rate_limited"), (503, "upstream_unavailable")],
)
def test_provider_http_failures_have_safe_distinct_diagnostics(status, kind):
    async def run():
        async def provider(_):
            return web.Response(status=status, text="secret provider body")

        app = web.Application()
        app.router.add_post("/decisions", provider)
        async with serve(app) as url:
            client = DecisionsClient("secret-key", endpoint=url + "/decisions")
            result = await client.classify("outgoing", {})
            assert result["fallback"] == "service_failure"
            assert result["providerError"] == {"kind": kind, "httpStatus": status}
            assert "secret" not in json.dumps(client.health_snapshot())

    asyncio.run(run())


@pytest.mark.parametrize(
    "body",
    [
        {**health_body(), "upstreamStatus": "verified"},
        {**health_body(), "circuit": {"state": "closed", "consecutiveFailures": 0, "retryAfterSeconds": float("nan")}},
        {**health_body(), "lastFailure": {"kind": "transport", "message": "private text"}},
        {**health_body(), "unexpectedInput": "private text"},
        {},
        [],
    ],
)
def test_routing_client_rejects_incompatible_health_without_exposing_body(tmp_path, body):
    async def run():
        write_private_json(tmp_path / "credential.json", {"token": "routing-token"})

        async def health(_):
            return web.json_response(body)

        app = web.Application()
        app.router.add_get("/health", health)
        socket = tmp_path / "broker.sock"
        async with serve(app, socket=socket):
            routing = RoutingClient(socket, tmp_path / "credential.json")
            expected = {
                "status": "incompatible",
                "reason": "invalid_health_contract",
                "action": "rebuild_runtime_image",
            }
            assert await routing.health() == expected
            assert routing.last_health == expected

    asyncio.run(run())


@pytest.mark.parametrize(
    ("field", "actual", "safe_actual"),
    [
        ("protocolVersion", 2, 2),
        ("protocolVersion", True, None),
        ("routingPolicyVersion", "diplomacy-routing-v1", "diplomacy-routing-v1"),
        ("advisoryPolicyVersion", "player-advisories-v2", "player-advisories-v2"),
        ("classifierModel", "secret-provider-key", None),
        ("accountingRateVersion", {"secret": "private player text"}, None),
    ],
)
def test_health_version_mismatches_are_specific_bounded_and_never_echo_arbitrary_values(
    tmp_path, field, actual, safe_actual
):
    async def run():
        write_private_json(tmp_path / "credential.json", {"token": "routing-token"})

        async def health(_):
            return web.json_response({**health_body(), field: actual})

        app = web.Application()
        app.router.add_get("/health", health)
        socket = tmp_path / "broker.sock"
        async with serve(app, socket=socket):
            routing = RoutingClient(socket, tmp_path / "credential.json")
            expected = {
                "status": "incompatible",
                "reason": "broker_version_mismatch",
                "action": "rebuild_runtime_image",
                "versionMismatches": {field: {"expected": health_body()[field], "actual": safe_actual}},
            }
            assert await routing.health() == expected
            assert routing.last_health == expected
            assert "secret" not in json.dumps(expected) and "private" not in json.dumps(expected)

    asyncio.run(run())


def test_routing_health_access_outage_malformed_and_timeout_are_bounded(tmp_path):
    async def run():
        write_private_json(tmp_path / "credential.json", {"token": "routing-token"})
        socket = tmp_path / "broker.sock"
        routing = RoutingClient(socket, tmp_path / "credential.json")
        assert await routing.health(timeout=0.02) == {"status": "unavailable", "reason": "broker_unavailable"}
        modes = ["access", "malformed", "timeout"]

        async def health(_):
            mode = modes.pop(0)
            if mode == "access":
                raise web.HTTPUnauthorized()
            if mode == "malformed":
                return web.Response(text="not JSON; secret-key")
            await asyncio.sleep(0.05)
            return web.json_response(health_body())

        app = web.Application()
        app.router.add_get("/health", health)
        async with serve(app, socket=socket):
            assert await routing.health(timeout=0.02) == {
                "status": "unavailable",
                "reason": "broker_access_denied",
                "httpStatus": 401,
            }
            assert await routing.health(timeout=0.02) == {
                "status": "incompatible",
                "reason": "invalid_health_contract",
                "action": "rebuild_runtime_image",
            }
            assert await routing.health(timeout=0.02) == {"status": "unavailable", "reason": "broker_timeout"}

    asyncio.run(run())


@pytest.mark.parametrize("timeout", [0, -1, 6, float("inf"), float("nan"), True])
def test_routing_health_rejects_unbounded_timeout(tmp_path, timeout):
    async def run():
        routing = RoutingClient(tmp_path / "absent.sock", tmp_path / "absent.json")
        with pytest.raises(ValueError, match="timeout must be positive"):
            await routing.health(timeout=timeout)

    asyncio.run(run())
