"""Real HTTPS/MCP framing, connection ownership, and uncertain action boundaries."""

import asyncio
import json
import ssl
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from civ_agent.civilization_state import StateConflict
from civ_agent.diagnostics import Diagnostics
from civ_agent.host_connection import HostConnection
from civ_agent.tls import ensure_server_certificate


@pytest.fixture(scope="module")
def material(tmp_path_factory):
    return ensure_server_certificate(tmp_path_factory.mktemp("sdk-tls"), "server", ["localhost"])


@asynccontextmanager
async def endpoint(material, call):
    requests = []

    async def handle(request):
        assert request.headers["Authorization"] == "Bearer " + "P" * 43
        if request.method == "GET":
            return web.Response(status=405)
        if request.method == "DELETE":
            assert request.headers["Mcp-Session-Id"] == "test-session"
            requests.append({"method": "DELETE"})
            return web.Response(status=204)
        body = await request.json()
        requests.append(body)
        method = body["method"]
        if method == "initialize":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "serverInfo": {"name": "test-host", "version": "1"},
                        "capabilities": {"tools": {}},
                    },
                },
                headers={"Mcp-Session-Id": "test-session"},
            )
        assert request.headers["MCP-Protocol-Version"] == "2025-06-18"
        assert request.headers["Mcp-Session-Id"] == "test-session"
        if method.startswith("notifications/"):
            return web.Response(status=202)
        if method == "tools/list":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "tools": [{"name": name, "inputSchema": {"type": "object"}} for name in ("read", "act")]
                    },
                }
            )
        assert method == "tools/call"
        return await call(request, body)

    app = web.Application()
    app.router.add_route("*", "/mcp", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(material["cert_file"], material["key_file"])
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    try:
        yield f"https://localhost:{runner.addresses[0][1]}/mcp", requests
    finally:
        await runner.cleanup()


def result(body, value):
    return web.json_response(
        {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {
                "content": [{"type": "text", "text": json.dumps(value)}],
                "structuredContent": value,
            },
        }
    )


def test_json_and_multiline_sse_share_one_session_and_close_from_another_task(material):
    async def run():
        release = asyncio.Event()

        async def call(request, body):
            if not body["params"]["arguments"].get("sse"):
                return result(body, {"ok": True})
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            # One JSON document split across SSE data lines, on a still-open stream.
            value = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": '{"sse":true}'}],
                    },
                },
                indent=2,
            )
            await response.write(
                ("event: message\n" + "".join("data: " + line + "\n" for line in value.splitlines()) + "\n").encode()
            )
            await release.wait()
            return response

        async with endpoint(material, call) as (url, requests):
            host = HostConnection(url, "P" * 43, material["ca_file"])
            try:
                assert await asyncio.create_task(host.tool("read", {})) == {"ok": True}
                async with asyncio.timeout(3):
                    assert await host.tool("read", {"sse": True}) == {"sse": True}
                assert [tool["name"] for tool in await host.list_tools()] == ["read", "act"]
            finally:
                release.set()
                await asyncio.create_task(host.close())
            assert sum(request["method"] == "initialize" for request in requests) == 1
            assert requests[-1]["method"] == "DELETE"
            with pytest.raises(StateConflict, match="HOST_CONNECTION_CLOSED"):
                await host.tool("read", {})

    asyncio.run(run())


def test_mailbox_wait_does_not_block_actions_and_cancellation_keeps_session_usable(material):
    async def run():
        waiting, release = asyncio.Event(), asyncio.Event()

        async def call(request, body):
            if body["params"]["arguments"].get("wait"):
                waiting.set()
                await release.wait()
            return result(body, {"ok": True})

        async with endpoint(material, call) as (url, requests):
            async with HostConnection(url, "P" * 43, material["ca_file"]) as host:
                pending = asyncio.create_task(host.tool("read", {"wait": True}))
                try:
                    async with asyncio.timeout(3):
                        await waiting.wait()
                        assert await host.tool("act", {"requestId": "stable-action"}) == {"ok": True}
                        pending.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await pending
                        assert await host.tool("read", {}) == {"ok": True}
                finally:
                    release.set()
                    if not pending.done():
                        pending.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await pending
            assert sum(item["method"] == "initialize" for item in requests) == 1

    asyncio.run(run())


def test_lost_command_response_is_not_retried_and_receipt_can_be_read(material, tmp_path):
    async def run():
        enacted = []
        diagnostics = Diagnostics(tmp_path, "France")

        async def call(request, body):
            args = body["params"]["arguments"]
            if body["params"]["name"] == "act":
                enacted.append(args["requestId"])
                request.transport.close()
                return web.Response()
            return result(body, {"receipt": enacted[0]})

        async with endpoint(material, call) as (url, _):
            async with HostConnection(
                url,
                "P" * 43,
                material["ca_file"],
                diagnostics=diagnostics,
                context={"purpose": "gameplay", "activationId": "france-turn-61", "gameTurn": 61},
            ) as host:
                async with asyncio.timeout(5):
                    with pytest.raises(StateConflict, match="^HOST_UNAVAILABLE$"):
                        await host.tool("act", {"requestId": "durable-1"})
                    assert enacted == ["durable-1"]
                    assert await host.tool("read", {}) == {"receipt": "durable-1"}
                assert enacted == ["durable-1"]
        diagnostics.close()
        events = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
        failed = next(event for event in events if event["event"] == "host_call_failed")
        assert failed["requestId"] == "durable-1"
        assert failed["activationId"] == "france-turn-61" and failed["gameTurn"] == 61
        assert failed["stage"] == "call_tool" and failed["exception"]["type"]
        assert failed["elapsedMs"] >= 0
        assert any(e["event"] == "host_call_completed" and e.get("tool") == "read" for e in events)
        assert any(e["event"] == "host_session_ready" and e["sessionAttempt"] == 2 for e in events)

    asyncio.run(run())


@pytest.mark.parametrize("code", [CONNECTION_CLOSED, REQUEST_TIMEOUT])
def test_mcp_transport_failure_reconnects_without_replaying_a_command(material, code):
    async def run():
        calls = []

        async def call(request, body):
            calls.append(body["params"]["name"])
            if body["params"]["name"] == "act":
                return web.json_response(
                    {"jsonrpc": "2.0", "id": body["id"], "error": {"code": code, "message": "Lost response"}}
                )
            return result(body, {"ok": True})

        async with endpoint(material, call) as (url, requests):
            async with HostConnection(url, "P" * 43, material["ca_file"]) as host:
                with pytest.raises(StateConflict, match="^HOST_UNAVAILABLE$"):
                    await host.tool("act", {"requestId": "one-attempt"})
                assert calls == ["act"]
                assert await host.tool("read", {}) == {"ok": True}
                assert calls == ["act", "read"]
                assert sum(request["method"] == "initialize" for request in requests) == 2

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["redirect", "malformed", "error", "http-error", "content-type"])
def test_host_errors_are_sanitized_and_redirects_do_not_resend_commands(material, failure, caplog, tmp_path):
    async def run():
        marker = "PRIVATE-RESPONSE-MUST-NOT-ESCAPE"
        calls = []
        diagnostics = Diagnostics(tmp_path, "France")

        async def call(request, body):
            calls.append(body)
            if failure == "redirect":
                return web.Response(status=307, headers={"Location": "/mcp?secret=" + marker})
            if failure == "malformed":
                return web.Response(text=marker, content_type="application/json")
            if failure == "http-error":
                return web.Response(status=503, text=marker)
            if failure == "content-type":
                return web.Response(text=marker, content_type="text/html")
            return web.json_response({"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32603, "message": marker}})

        async with endpoint(material, call) as (url, _):
            async with HostConnection(url, "P" * 43, material["ca_file"], diagnostics=diagnostics) as host:
                async with asyncio.timeout(5):
                    with pytest.raises(StateConflict) as error:
                        await host.tool("act", {"requestId": "one-attempt", "privatePayload": marker})
                assert str(error.value) in {"HOST_UNAVAILABLE", "HOST_PROTOCOL_ERROR"}
                assert marker not in str(error.value)
                assert marker not in caplog.text
                assert len(calls) == 1
        diagnostics.close()
        text = diagnostics.path.read_text()
        assert marker not in text and "P" * 43 not in text
        events = [json.loads(line) for line in text.splitlines()]
        responses = [e for e in events if e["event"] == "host_http_response" and e.get("requestId") == "one-attempt"]
        assert len(responses) == 1
        assert responses[0]["httpStatus"] == {"redirect": 307, "http-error": 503}.get(failure, 200)
        failed = next(e for e in events if e["event"] == "host_call_failed")
        assert failed["exception"]["stack"]
        if failure == "error":
            assert failed["exception"]["rpcCode"] == -32603
        if failure == "malformed":
            # The SDK converts the parsing exception into an MCP error. Preserve
            # the original exception from its logger before that conversion.
            original = next(e for e in events if e["event"] == "mcp_event" and e.get("exception", {}).get("validation"))
            assert original["function"] == "_handle_json_response"
            assert original["exception"]["validation"][0]["type"] == "json_invalid"
            assert failed["connectionId"] == original["connectionId"]
        if failure == "content-type":
            assert responses[0]["contentType"] == "text/html"

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["missing-ca", "wrong-hostname"])
def test_sdk_verifies_the_certificate_before_sending_credentials(material, failure, tmp_path):
    async def run():
        diagnostics = Diagnostics(tmp_path, "France")

        async def call(request, body):
            raise AssertionError("TLS verification must fail before calling a tool")

        async with endpoint(material, call) as (url, requests):
            ca = None if failure == "missing-ca" else material["ca_file"]
            if failure == "wrong-hostname":
                url = url.replace("localhost", "127.0.0.1")
            async with HostConnection(url, "P" * 43, ca, diagnostics=diagnostics) as host:
                async with asyncio.timeout(5):
                    with pytest.raises(StateConflict):
                        await host.tool("read", {})
                assert requests == []
        diagnostics.close()
        events = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
        failed_session = next(e for e in events if e["event"] == "host_session_failed")
        assert failed_session["stage"] == "connect" and failed_session["exception"]["type"]
        assert any(e["event"] == "host_call_failed" and e["stage"] == "connect" for e in events)

    asyncio.run(run())
