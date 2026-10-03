"""Opt-in real Codex -> local Responses/MCP servers. No paid model calls."""

import asyncio
import json
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from civ_agent import providers
from civ_agent.agent_service import CodexProcess
from civ_agent.agent_settings import codex_thread_settings


@pytest.mark.skipif(not os.environ.get("CIV_TEST_CODEX"), reason="Set CIV_TEST_CODEX to the Codex executable")
@pytest.mark.parametrize("effort", ["high", "none"])
@pytest.mark.parametrize("structured", [False, True])
def test_codex_openrouter_tool_roundtrip_and_usage(tmp_path, monkeypatch, effort, structured):
    async def run():
        requests, calls, notices = [], [], []
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }

        async def catalog(request):
            return web.json_response(
                {
                    "data": [
                        {
                            "id": "test/reasoner",
                            "context_length": 64000,
                            "supported_parameters": ["tools", "structured_outputs", "reasoning"]
                            if effort == "high"
                            else ["tools", "structured_outputs"],
                        }
                    ]
                }
            )

        async def mcp(request):
            message = await request.json()
            method = message["method"]
            if "id" not in message:
                return web.Response(status=202)
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "benchmark", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "bench_echo",
                            "description": "Return the supplied text.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                                "additionalProperties": False,
                            },
                        }
                    ]
                }
            elif method == "tools/call":
                calls.append(message["params"])
                result = {"content": [{"type": "text", "text": "echo succeeded"}], "isError": False}
            else:
                result = {}
            return web.json_response({"jsonrpc": "2.0", "id": message["id"], "result": result})

        async def responses(request):
            assert request.headers["Authorization"] == "Bearer synthetic-openrouter-key"
            body = await request.json()
            requests.append(body)
            index = len(requests)
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)

            async def event(kind, **fields):
                await response.write(("data: " + json.dumps({"type": kind, **fields}) + "\n\n").encode())

            await event(
                "response.created", response={"id": f"resp_{index}", "object": "response", "status": "in_progress"}
            )
            if index == 1:
                namespace = next(t for t in body["tools"] if t["type"] == "namespace" and t["name"] == "mcp__civ")
                tool = next(t for t in namespace["tools"] if t["name"] == "bench_echo")
                item = {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": tool["name"],
                    "namespace": namespace["name"],
                    "arguments": "",
                }
                await event("response.output_item.added", output_index=0, item=item)
                item["arguments"] = '{"text":"hello"}'
                await event(
                    "response.function_call_arguments.delta", item_id="fc_1", output_index=0, delta=item["arguments"]
                )
                await event(
                    "response.function_call_arguments.done", item_id="fc_1", output_index=0, arguments=item["arguments"]
                )
            else:
                item = {"type": "message", "id": "msg_1", "role": "assistant", "content": []}
                await event("response.output_item.added", output_index=0, item=item)
                reply = '{"answer":"Done."}' if structured else "Done."
                await event("response.output_text.delta", item_id="msg_1", output_index=0, content_index=0, delta=reply)
                item["content"] = [{"type": "output_text", "text": reply, "annotations": []}]
            item["status"] = "completed"
            await event("response.output_item.done", output_index=0, item=item)
            await event(
                "response.completed",
                response={
                    "id": f"resp_{index}",
                    "object": "response",
                    "status": "completed",
                    "model": "test/reasoner",
                    "output": [item],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "total_tokens": 120,
                        "input_tokens_details": {"cached_tokens": 40},
                        "output_tokens_details": {"reasoning_tokens": 10},
                    },
                },
            )
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_get("/models", catalog)
        app.router.add_post("/responses", responses)
        app.router.add_post("/mcp", mcp)
        async with TestServer(app) as server:
            monkeypatch.setattr(providers, "OPENROUTER_URL", str(server.make_url("/")).rstrip("/"))
            home = tmp_path / "codex"
            home.mkdir()
            monkeypatch.setenv("CODEX_HOME", str(home))
            monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-openrouter-key")
            monkeypatch.setenv("CIV_AUTH_SOCKET", "must-not-connect")
            monkeypatch.setenv("CIV_AUTH_TOKEN_FILE", "must-not-read")
            monkeypatch.delenv("CIV_OPENROUTER_KEY_FILE", raising=False)
            complete = asyncio.Event()

            async def notification(method, params):
                notices.append((method, params))
                if method in ("turn/completed", "service/disconnected"):
                    complete.set()

            process = CodexProcess(
                os.environ["CIV_TEST_CODEX"], tmp_path, str(server.make_url("/mcp")), None, notification
            )
            settings = {"provider": "openrouter", "model": "test/reasoner", "reasoningEffort": effort}
            try:
                await process.start("synthetic-player-token", settings)
                thread = await process.request(
                    "thread/start",
                    {
                        "cwd": str(tmp_path),
                        "approvalPolicy": "never",
                        "sandbox": "workspace-write",
                        "developerInstructions": "Use the provided echo tool, then finish.",
                        **codex_thread_settings(settings, fast=True),
                    },
                )
                assert thread["modelProvider"] == "openrouter"
                assert thread["model"] == "test/reasoner" and thread["reasoningEffort"] == effort
                await process.request(
                    "turn/start",
                    {
                        "threadId": thread["thread"]["id"],
                        "input": [{"type": "text", "text": "Echo hello."}],
                        "approvalPolicy": "never",
                        "effort": effort,
                        "summary": "auto",
                        **({"outputSchema": schema} if structured else {}),
                    },
                )
                await asyncio.wait_for(complete.wait(), 30)
                assert len(requests) == 2, notices
                assert [{k: c[k] for k in ("name", "arguments")} for c in calls] == [
                    {"name": "bench_echo", "arguments": {"text": "hello"}}
                ]
                assert all(r["model"] == "test/reasoner" for r in requests)
                if effort == "high":
                    assert all(r["reasoning"]["effort"] == "high" for r in requests)
                else:
                    assert all((r.get("reasoning") or {}).get("effort", "none") == "none" for r in requests)
                assert all(r.get("service_tier") in (None, "default") for r in requests)
                if structured:
                    assert all(r["text"]["format"]["schema"] == schema for r in requests)
                assert any(item["type"] == "function_call_output" for item in requests[1]["input"])
                usages = [p["tokenUsage"] for m, p in notices if m == "thread/tokenUsage/updated"]
                assert 0 < usages[-1]["modelContextWindow"] <= 64000
                expected = {
                    "inputTokens": 200,
                    "cachedInputTokens": 80,
                    "outputTokens": 40,
                    "reasoningOutputTokens": 20,
                    "totalTokens": 240,
                }
                assert {key: usages[-1]["total"][key] for key in expected} == expected
            finally:
                await process.close()

    asyncio.run(run())
