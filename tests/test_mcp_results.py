"""Single-payload delivery, standard MCP envelopes, and durable audit receipts."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager

import aiohttp
import httpx2
import pytest
from aiohttp import web
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from civ_agent.agent_service import server_ssl_context
from civ_agent.civilization_state import CivilizationState
from civ_agent.mcp_results import RESULT_FORMAT_HEADER, STRUCTURED_RESULT_FORMAT, canonical_tool_result
from civ_agent.proposal_gateway import ProposalGateway, public_result, tool_result
from civ_agent.service_io import ServiceIO
from civ_agent.tls import client_ssl_context, ensure_server_certificate
from tests.test_proposal_gateway import Host


@pytest.mark.parametrize("payload", [{}, {"city": "Róma", "choices": [True, 1, None]}, {"error": "DETAIL_REQUIRED"}])
def test_only_duplicate_json_is_removed_without_mutating_the_source(payload):
    duplicate = {"type": "text", "text": json.dumps(payload, indent=2)}
    extras = [
        {"type": "text", "text": "Additional confirmation required."},
        {**duplicate, "annotations": {"audience": ["user"]}},
        {**duplicate, "_meta": {"source": "preview"}},
        {"type": "image", "mimeType": "image/png", "data": "AA=="},
        {"type": "audio", "mimeType": "audio/wav", "data": "AA=="},
        {"type": "resource_link", "uri": "memory://note", "name": "note"},
    ]
    original = {**tool_result(payload), "content": [duplicate, *extras, duplicate], "_meta": {"audit": "retained"}}
    saved = copy.deepcopy(original)
    normalized = canonical_tool_result(original)
    assert normalized == {**original, "content": extras}
    assert original == saved
    assert canonical_tool_result(normalized) == normalized
    assert normalized["isError"] == ("error" in payload)


@pytest.mark.parametrize(
    "result",
    [
        {"content": [{"type": "text", "text": "Only text, including an error."}], "isError": True},
        {"structuredContent": None, "content": [{"type": "text", "text": "null"}]},
        {"structuredContent": {"value": True}, "content": [{"type": "text", "text": '{"value":1}'}]},
        {"structuredContent": {"value": 1}, "content": [{"type": "text", "text": '{"value":1.0}'}]},
        {"structuredContent": {"value": 1, "cost": 2}, "content": [{"type": "text", "text": '{"value":1}'}]},
        {"structuredContent": {}, "content": [{"type": "text", "text": "{invalid JSON"}]},
        {"structuredContent": {}, "content": []},
    ],
)
def test_fallback_and_distinct_content_are_preserved(result):
    assert canonical_tool_result(result) == result


def test_json_key_order_and_whitespace_do_not_prevent_deduplication():
    result = {
        "structuredContent": {"city": "Rome", "nested": {"b": 2, "a": 1}},
        "content": [{"type": "text", "text": '{ "nested": {"a":1,"b":2}, "city": "Rome" }'}],
        "isError": True,
    }
    assert canonical_tool_result(result) == {**result, "content": []}


@pytest.fixture(scope="module")
def material(tmp_path_factory):
    return ensure_server_certificate(tmp_path_factory.mktemp("result-tls"), "server", ["localhost"])


@asynccontextmanager
async def gateway_endpoint(tmp_path, material, purpose="gameplay"):
    io = ServiceIO()
    memory = CivilizationState(tmp_path / "private", "Rome")
    gateway = ProposalGateway(memory, io, tmp_path / "workspace")
    host = Host()
    job = await gateway.open("delivery", purpose, "g1", host, {})
    app = web.Application()
    app.router.add_post("/mcp", gateway.handle)
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        site = web.TCPSite(
            runner, "127.0.0.1", 0, ssl_context=server_ssl_context(material["cert_file"], material["key_file"])
        )
        await site.start()
        yield f"https://localhost:{runner.addresses[0][1]}/mcp", gateway, job, host
    finally:
        await runner.cleanup()
        await gateway.close(job, True)
        await io.close()


@pytest.mark.parametrize("purpose", ["gameplay", "communication"])
@pytest.mark.parametrize("result_format", [None, STRUCTURED_RESULT_FORMAT, "unsupported"])
def test_sdk_reads_schema_valid_results_in_both_formats(tmp_path, material, purpose, result_format):
    async def run():
        async with gateway_endpoint(tmp_path, material, purpose) as (url, gateway, job, _):
            headers = {"Authorization": "Bearer " + job.token}
            if result_format is not None:
                headers[RESULT_FORMAT_HEADER] = result_format
            async with httpx2.AsyncClient(
                headers=headers, verify=client_ssl_context(material["ca_file"]), trust_env=False
            ) as http:
                async with Client(streamable_http_client(url, http_client=http), mode="legacy", cache=None) as client:
                    assert any(tool.name == "memory_search" for tool in (await client.list_tools()).tools)
                    reply = await client.session.call_tool("memory_search", {})
                    assert reply.structured_content is not None and not reply.is_error
                    assert reply.structured_content["records"] == []
                    if result_format == STRUCTURED_RESULT_FORMAT:
                        assert reply.content == []
                    else:
                        assert isinstance(reply.content[0], TextContent)
                        assert json.loads(reply.content[0].text) == reply.structured_content
                    with gateway.memory.transaction() as db:
                        saved = db.exec_driver_sql("SELECT result FROM calls WHERE job=?", (job.id,)).scalar_one()
                    assert json.loads(saved) == reply.structured_content

    asyncio.run(run())


def test_opt_in_preserves_authentication_errors_receipts_and_cross_format_replay(tmp_path, material):
    async def run():
        async with gateway_endpoint(tmp_path, material) as (url, gateway, job, host):
            async with aiohttp.ClientSession() as client:

                async def call(id, name, arguments, *, structured=True, token=None, expected_status=200):
                    headers = {"Authorization": "Bearer " + (job.token if token is None else token)}
                    if structured:
                        headers[RESULT_FORMAT_HEADER] = STRUCTURED_RESULT_FORMAT
                    async with client.post(
                        url,
                        ssl=client_ssl_context(material["ca_file"]),
                        headers=headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": id,
                            "method": "tools/call",
                            "params": {"name": name, "arguments": arguments},
                        },
                    ) as response:
                        assert response.status == expected_status
                        if response.status != 200:
                            return {}
                        return (await response.json())["result"]

                await call("unauthorized", "player_turn", {}, token="wrong", expected_status=401)
                error = await call("invalid", "player_command", {"kind": "NOT_A_COMMAND"})
                assert error["isError"] and error["structuredContent"]["error"]
                assert error["content"] == []
                legacy_error = await call("invalid", "player_command", {"kind": "NOT_A_COMMAND"}, structured=False)
                assert (
                    legacy_error["isError"]
                    and json.loads(legacy_error["content"][0]["text"]) == error["structuredContent"]
                )
                await call("view", "player_turn", {})
                await call("detail", "unit_details", {"target": "unit1"})
                action = {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}}
                compact = await call("move", "player_command", action)
                assert not compact["isError"] and compact["content"] == []
                legacy = await call("move", "player_command", action, structured=False)
                assert json.loads(legacy["content"][0]["text"]) == compact["structuredContent"]
                assert compact == canonical_tool_result(legacy)
                assert len(host.executed) == 1
                with gateway.memory.transaction() as db:
                    saved = db.exec_driver_sql(
                        "SELECT result FROM calls WHERE job=? AND id='move'", (job.id,)
                    ).scalar_one()
                saved_result = json.loads(saved)
                assert saved_result["requestId"] == host.executed[0]["requestId"]
                assert saved_result["revision"] == host.revision
                assert public_result(saved_result) == compact["structuredContent"]
                gateway.paused = True
                paused = await call("paused", "player_turn", {})
                assert paused == {
                    "content": [],
                    "structuredContent": {"error": "JOB_PAUSED_OR_EXPIRED"},
                    "isError": True,
                }
                gateway.paused = False

    asyncio.run(run())
