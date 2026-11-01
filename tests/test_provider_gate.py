"""Request ceilings act before inference, independently of Codex notifications."""

import asyncio
import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from civ_agent.protocol import JsonObject
from civ_agent.provider_gate import ProviderGate, RequestBudget

SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False, "required": []}
BODY: JsonObject = {"input": [], "text": {"format": {"type": "json_schema", "strict": True, "schema": SCHEMA}}}
POLICY_ERROR = {
    "error": {
        "code": "invalid_prompt",
        "message": "Invalid prompt: your prompt was flagged as potentially violating our usage policy. PRIVATE",
    }
}


def test_policy_retry_reservation_is_atomic_durable_and_counts_against_budget(tmp_path):
    path = tmp_path / "budget.sqlite"
    first, second = RequestBudget(path, 3), RequestBudget(path, 3)
    assert first.reserve("original", "generation")
    with ThreadPoolExecutor(max_workers=8) as executor:
        accepted = list(
            executor.map(
                lambda i: (first if i % 2 else second).reserve(str(i), "generation", retry_scope="activation"),
                range(20),
            )
        )
    assert sum(accepted) == 1 and first.used() == 2
    resumed = RequestBudget(path, 3)
    assert not resumed.reserve("restart", "generation", retry_scope="activation")
    assert resumed.reserve("other", "generation", retry_scope="other-activation")
    assert not resumed.reserve("over-budget", "generation", retry_scope="third-activation")


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "repeated",
        "generic_invalid",
        "authored_refusal",
        "other_status",
        "malformed",
        "large",
        "budget_exhausted",
        "no_budget",
        "compaction",
        "expired",
        "reservation_failed",
    ],
)
def test_only_prompt_filter_rejection_gets_one_identical_budgeted_retry(tmp_path, monkeypatch, case):
    async def run():
        received, notices = [], []
        original = json.dumps(POLICY_ERROR).encode()
        status = 400
        if case == "generic_invalid":
            original = b'{"error":{"code":"invalid_prompt","message":"Bad input"}}'
        elif case == "authored_refusal":
            original, status = b'{"type":"refusal","refusal":"I cannot help with this."}', 200
        elif case == "other_status":
            status = 403
        elif case == "malformed":
            original = b'[{"not":"an error"}]'
        elif case == "large":
            original = json.dumps({**POLICY_ERROR, "padding": "x" * 20000}).encode()

        async def provider(request):
            received.append(await request.read())
            if case == "expired":
                gate.begin("different-thread", SCHEMA)
            if len(received) == 2 and case != "repeated":
                return web.json_response({"ok": True})
            return web.Response(body=original, status=status, content_type="application/json")

        async def notice(method, params):
            notices.append((method, params))

        budget = RequestBudget(tmp_path / "budget.sqlite", 1 if case == "budget_exhausted" else 5)
        if case == "reservation_failed":
            reserve = budget.reserve

            def broken_reservation(*args, **kwargs):
                if kwargs.get("retry_scope"):
                    raise sqlite3.OperationalError("PRIVATE")
                return reserve(*args, **kwargs)

            monkeypatch.setattr(budget, "reserve", broken_reservation)
        gate = ProviderGate(notice, None if case == "no_budget" else budget, retry_scope="activation")
        app = web.Application()
        app.router.add_post("/{tail:.*}", provider)
        async with TestServer(app) as server:
            await gate.start()
            gate.upstream = str(server.make_url("/")).rstrip("/")
            gate.begin("thread", SCHEMA)
            gate.bind("turn")
            assert gate.url is not None
            try:
                async with aiohttp.ClientSession() as client:
                    tail = "responses/compact" if case == "compaction" else "responses"
                    body = {**BODY, "input": [{"role": "user", "content": "Unchanged fictional game context"}]}
                    async with client.post(gate.url + "/" + tail, json=body) as response:
                        assert response.status == (200 if case == "success" else status)
                        assert await response.read() == (b'{"ok": true}' if case == "success" else original)
                    retries = case in {"success", "repeated"}
                    assert len(received) == (2 if retries else 1)
                    if retries:
                        assert received[0] == received[1]
                    if case != "compaction":
                        for raw in received:
                            actual = json.loads(raw)
                            assert actual["tools"] == [] and actual["tool_choice"] == "none"
                            assert actual["text"]["format"]["schema"] == SCHEMA
                    requests = [p for m, p in notices if m == "service/providerRequest"]
                    rejections = [p for m, p in notices if m == "service/providerRejection"]
                    assert len(requests) == len(received)
                    if retries:
                        assert rejections[0]["requestId"] == requests[0]["requestId"]
                        assert rejections[0]["retryRequestId"] == requests[1]["requestId"]
                        assert len(rejections) == (2 if case == "repeated" else 1)
                    assert "PRIVATE" not in json.dumps(notices)
                    assert "Unchanged fictional game context" not in json.dumps(notices)
                    if case != "no_budget":
                        assert budget.used() == len(received)
            finally:
                await gate.close()

    asyncio.run(run())


def test_policy_retry_limit_survives_gate_restart_and_different_thread(tmp_path):
    async def run():
        received, notices = [], []

        async def provider(request):
            received.append(await request.json())
            return web.json_response(POLICY_ERROR, status=400)

        async def notice(method, params):
            notices.append((method, params))

        app = web.Application()
        app.router.add_post("/responses", provider)
        async with TestServer(app) as server:
            for thread in ("first", "resumed"):
                gate = ProviderGate(notice, RequestBudget(tmp_path / "budget.sqlite", 5), retry_scope="activation")
                await gate.start()
                gate.upstream = str(server.make_url("/")).rstrip("/")
                gate.begin(thread, SCHEMA)
                assert gate.url is not None
                try:
                    async with aiohttp.ClientSession() as client:
                        async with client.post(gate.url + "/responses", json=BODY) as response:
                            assert response.status == 400
                            assert await response.json() == POLICY_ERROR
                finally:
                    await gate.close()
        assert len(received) == 3
        assert len([p for m, p in notices if m == "service/providerRejection" and p["retryRequestId"]]) == 1

    asyncio.run(run())


def test_reserved_policy_retry_never_forwards_after_window_expires(tmp_path):
    async def run():
        received = []

        async def provider(request):
            received.append(await request.json())
            return web.json_response(POLICY_ERROR, status=400)

        async def notice(method, params):
            if method == "service/providerRejection":
                gate.begin("replacement", SCHEMA)

        budget = RequestBudget(tmp_path / "budget.sqlite", 5)
        gate = ProviderGate(notice, budget, retry_scope="activation")
        app = web.Application()
        app.router.add_post("/responses", provider)
        async with TestServer(app) as server:
            await gate.start()
            gate.upstream = str(server.make_url("/")).rstrip("/")
            gate.begin("original", SCHEMA)
            assert gate.url is not None
            try:
                async with aiohttp.ClientSession() as client:
                    async with client.post(gate.url + "/responses", json=BODY) as response:
                        assert (await response.json())["error"]["code"] == "PROVIDER_DECISION_EXPIRED"
                assert len(received) == 1
                # A reservation is never refunded after a cancellation/race.
                assert budget.used() == 2
                assert not budget.reserve("another", "generation", retry_scope="activation")
            finally:
                await gate.close()

    asyncio.run(run())


def test_shared_budget_is_atomic_and_survives_restart(tmp_path):
    path = tmp_path / "budget.sqlite"
    first, second = RequestBudget(path, 7), RequestBudget(path, 7)
    with ThreadPoolExecutor(max_workers=8) as executor:
        accepted = list(executor.map(lambda i: (first if i % 2 else second).reserve(str(i), "generation"), range(30)))
    assert sum(accepted) == 7
    assert not RequestBudget(path, 7).reserve("after-restart", "generation")
    with pytest.raises(ValueError, match="BUDGET_CHANGED"):
        RequestBudget(path, 8)


@pytest.mark.parametrize(
    "failure",
    ["retry", "schema", "malformed_format", "redirect", "interrupted", "shared_budget", "compaction", "concurrent"],
)
def test_gate_prevents_unbudgeted_requests_and_never_leaks_auth(tmp_path, failure, monkeypatch):
    async def run():
        received, events = [], []
        started, release = asyncio.Event(), asyncio.Event()

        async def provider(request):
            received.append((request.path, await request.json()))
            assert request.headers["Authorization"] == "Bearer synthetic-secret"
            started.set()
            if failure == "redirect":
                raise web.HTTPTemporaryRedirect(location=str(request.url.with_path("/unexpected")))
            if failure == "interrupted":
                await release.wait()
            return web.json_response({"ok": True})

        async def notice(method, params):
            events.append((method, params))

        app = web.Application()
        app.router.add_post("/{tail:.*}", provider)
        budget = RequestBudget(tmp_path / "budget.sqlite", 1 if failure == "shared_budget" else 5)
        gate = ProviderGate(notice, budget)
        if failure == "concurrent":
            request_json = web.Request.json
            arrivals, both_arrived = [], asyncio.Event()

            async def simultaneous(request, **kwargs):
                body = await request_json(request, **kwargs)
                if request.path.startswith(gate.route):
                    arrivals.append(request)
                    if len(arrivals) == 2:
                        both_arrived.set()
                    await asyncio.wait_for(both_arrived.wait(), 5)
                return body

            monkeypatch.setattr(web.Request, "json", simultaneous)
        async with TestServer(app) as server:
            await gate.start()
            gate.upstream = str(server.make_url("/")).rstrip("/")
            gate.begin("thread", SCHEMA)
            assert gate.url is not None
            try:
                async with aiohttp.ClientSession(headers={"Authorization": "Bearer synthetic-secret"}) as client:

                    async def send(body=BODY, tail="responses"):
                        return await client.post(gate.url + "/" + tail, json=body, allow_redirects=False)

                    if failure == "concurrent":
                        first, second = await asyncio.gather(send(), send())
                        assert sorted([first.status, second.status]) == [200, 400]
                        assert len(received) == 1
                    elif failure in {"schema", "malformed_format"}:
                        body = copy.deepcopy(BODY)
                        if failure == "malformed_format":
                            body["text"] = None
                        else:
                            body["text"]["format"]["strict"] = False
                        response = await send(body)
                        assert response.status == 400 and not received
                    elif failure == "interrupted":
                        pending = asyncio.create_task(send())
                        await asyncio.wait_for(started.wait(), 5)
                        pending.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await pending
                        release.set()
                        response = await send()
                        assert response.status == 400 and len(received) == 1
                    elif failure == "compaction":
                        assert (await send({"input": []}, "responses/compact")).status == 200
                        assert (await send()).status == 200
                        assert (await send({"input": []}, "responses/compact")).status == 400
                        assert [params["kind"] for method, params in events if method == "service/providerRequest"] == [
                            "compaction",
                            "generation",
                        ]
                    else:
                        response = await send()
                        assert response.status == (307 if failure == "redirect" else 200)
                        if failure == "shared_budget":
                            gate.begin("resumed-thread", SCHEMA)
                        response = await send()
                        assert response.status == 400 and len(received) == 1
                    for path, body in received:
                        if path == "/responses":
                            assert body["tools"] == [] and body["tool_choice"] == "none"
                            assert body["text"]["format"]["schema"] == SCHEMA
                    assert "synthetic-secret" not in json.dumps(events)
                    assert "input" not in json.dumps(events)
                    with sqlite3.connect(budget.path) as db:
                        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == len(received)
            finally:
                release.set()
                await gate.close()

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["downstream_headers", "downstream_body", "downstream_eof", "upstream_read"])
def test_forwarding_distinguishes_client_close_from_provider_failure(tmp_path, monkeypatch, stage):
    async def run():
        events, completed = [], asyncio.Event()

        async def notice(method, params):
            events.append((method, params))
            if method in {"service/transportStatus", "service/providerRequestBlocked"}:
                completed.set()

        async def provider(request):
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(b'{"complete":true}')
            if stage == "upstream_read":
                assert request.transport is not None
                request.transport.close()  # Missing HTTP chunk terminator is a real upstream failure.
                return response
            await response.write_eof()
            return response

        gate = ProviderGate(notice, RequestBudget(tmp_path / "budget.sqlite", 2))
        method = {
            "downstream_headers": "prepare",
            "downstream_body": "write",
            "downstream_eof": "write_eof",
        }.get(stage)
        if method:
            original = getattr(web.StreamResponse, method)
            failed = False

            async def disconnect(response, *args, **kwargs):
                nonlocal failed
                request = args[0] if method == "prepare" else response._req
                if not failed and request is not None and request.path.startswith(gate.route):
                    failed = True
                    # This is a local socket exception, not provider error text.
                    raise ConnectionResetError("synthetic-secret must never be logged")
                return await original(response, *args, **kwargs)

            monkeypatch.setattr(web.StreamResponse, method, disconnect)
        app = web.Application()
        app.router.add_post("/responses", provider)
        async with TestServer(app) as server:
            await gate.start()
            gate.upstream = str(server.make_url("/")).rstrip("/")
            gate.begin("thread", SCHEMA)
            gate.bind("turn")
            assert gate.url is not None
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as client:
                    try:
                        async with client.post(gate.url + "/responses", json=BODY) as response:
                            body = await response.read()
                    except aiohttp.ClientError:
                        body = None
                await asyncio.wait_for(completed.wait(), 5)
                failures = [p for m, p in events if m == "service/providerRequestBlocked"]
                closed = [p for m, p in events if m == "service/transportStatus"]
                if stage == "upstream_read":
                    assert len(failures) == 1 and not closed
                    detail = failures[0]
                    assert detail["reason"] == "PROVIDER_FORWARD_FAILED"
                else:
                    assert not failures and len(closed) == 1
                    detail = closed[0]
                    assert detail["reason"] == "PROVIDER_CLIENT_DISCONNECTED"
                    if stage == "downstream_eof":
                        # The completed payload was already delivered before EOF failed.
                        assert body is not None
                        assert json.loads(body) == {"complete": True}
                assert detail["stage"] == stage and detail["exceptionType"]
                assert detail["threadId"] == "thread" and detail["turnId"] == "turn"
                assert detail["requestId"].startswith("provider:")
                assert "synthetic-secret" not in json.dumps(events)
                assert gate.budget is not None and gate.budget.used() == 1
            finally:
                await gate.close()

    asyncio.run(run())
