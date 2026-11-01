"""Real Codex -> local Responses server; no paid model requests."""

import asyncio
import base64
import json
import os
import shutil
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from civ_agent import agent_service, providers
from civ_agent.agent_service import CodexProcess
from civ_agent.agent_settings import DECISION_BASE_INSTRUCTIONS, codex_thread_settings
from civ_agent.decision_contracts import DecisionContract
from civ_agent.peace_review import PeaceApproval, TacticalOverview
from civ_agent.protocol import JsonObject
from civ_agent.provider_gate import ProviderGate
from civ_agent.review_grounding import source_catalogue
from civ_agent.strategic_review import StrategicAdvice
from civ_agent.structured_decisions import continuation
from tests.test_economy_optimization import managed
from tests.test_structured_decisions import sample


@pytest.mark.skipif(not shutil.which(os.environ.get("CIV_TEST_CODEX", "codex")), reason="Codex executable unavailable")
@pytest.mark.parametrize(
    "purpose,resume,disabled_exec,stall",
    [
        (purpose, resume, False, False)
        for purpose in (
            "gameplay",
            "economy",
            "military",
            "communication",
            "review",
            "military_consultation",
            "main_consultation",
        )
        for resume in (False, True)
    ]
    + [("gameplay", False, True, False)]
    + [
        (purpose, False, False, True)
        for purpose in (
            "gameplay",
            "economy",
            "military",
            "communication",
            "review",
            "military_consultation",
            "main_consultation",
        )
    ],
)
@pytest.mark.parametrize(
    "provider,model",
    [("openrouter", "test/reasoner"), ("openai", "gpt-6-luna"), ("openai", "gpt-6-terra")],
)
def test_every_generation_is_strict_and_tool_free(
    tmp_path, monkeypatch, purpose, resume, disabled_exec, stall, provider, model
):
    async def run():
        monkeypatch.delenv("CIV_MODEL_REQUEST_BUDGET", raising=False)
        monkeypatch.delenv("CIV_MODEL_REQUEST_LIMIT", raising=False)
        requests, notices, mcp_requests = [], [], []
        stalled_release = asyncio.Event()
        rpc_errors = []
        describe_error = agent_service.codex_request_error

        def capture_error(method, identity, error):
            rpc_errors.append(error)
            return describe_error(method, identity, error)

        monkeypatch.setattr(agent_service, "codex_request_error", capture_error)
        gate_requests = []
        forward = ProviderGate.forward

        async def capture_forward(gate, request):
            gate_requests.append((request.method, request.match_info["tail"]))
            return await forward(gate, request)

        monkeypatch.setattr(ProviderGate, "forward", capture_forward)
        claims = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "exp": 4102444800,
                        "https://api.openai.com/auth": {
                            "chatgpt_account_id": "synthetic-account",
                            "chatgpt_plan_type": "plus",
                        },
                    }
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        access_token = "eyJhbGciOiJub25lIn0." + claims + ".synthetic"

        class FakeAuth:
            def __init__(self, *args):
                pass

            async def fetch(self, **kwargs):
                return {"accessToken": access_token, "chatgptAccountId": "synthetic-account", "chatgptPlanType": "plus"}

        monkeypatch.setattr("civ_agent.container_auth.TokenClient", FakeAuth)
        async with managed(tmp_path) as m:
            role = purpose if purpose in {"gameplay", "economy", "military", "communication"} else "gameplay"
            job = (
                m.main
                if role == "gameplay"
                else m.economy
                if role == "economy"
                else await m.gateway.open("capture", role, "g1", m.host, {}, 0)
            )
            catalogue = await m.gateway.tools(job)
            report = {
                "review": StrategicAdvice,
                "military_consultation": TacticalOverview,
                "main_consultation": PeaceApproval,
            }.get(purpose)
            if purpose == "review":
                catalogue = source_catalogue(catalogue)
            elif report:
                catalogue = []
            contract = DecisionContract(purpose, catalogue, report_model=report)
            variants = contract.codec.original["properties"]["decision"]["anyOf"]
            chosen = next(
                v for v in variants if v["properties"]["kind"]["const"] == ("submit_report" if report else "finish")
            )
            answer = contract.encode(sample(chosen, contract.codec.definitions))

        async def catalog(request):
            return web.json_response(
                {
                    "data": [
                        {
                            "id": "test/reasoner",
                            "context_length": 262144,
                            "supported_parameters": ["tools", "structured_outputs", "reasoning"],
                        }
                    ]
                }
            )

        async def mcp(request):
            mcp_requests.append(await request.json())
            return web.Response(status=500)

        async def responses(request):
            assert request.headers["Authorization"] == "Bearer " + (
                access_token if provider == "openai" else "synthetic-openrouter-key"
            )
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
            rejected_call = disabled_exec and index == 1
            item: JsonObject = (
                {
                    "type": "custom_tool_call",
                    "id": "disabled-exec",
                    "call_id": "disabled-call",
                    "name": "exec",
                    "input": 'text("must not execute")',
                }
                if rejected_call
                else {"type": "message", "id": f"msg_{index}", "role": "assistant", "content": []}
            )
            await event("response.output_item.added", output_index=0, item=item)
            if stall and index == 1:
                await event(
                    "response.output_text.delta",
                    item_id=item["id"],
                    output_index=0,
                    content_index=0,
                    delta='{"decision":{' + " " * 4097,
                )
                await stalled_release.wait()
                return response
            if not rejected_call:
                await event(
                    "response.output_text.delta", item_id=item["id"], output_index=0, content_index=0, delta=answer
                )
                item.update(content=[{"type": "output_text", "text": answer, "annotations": []}], status="completed")
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

        app = web.Application(client_max_size=4 * 1024 * 1024)
        app.router.add_get("/models", catalog)
        app.router.add_post("/responses", responses)
        app.router.add_post("/mcp", mcp)
        async with TestServer(app) as server:
            monkeypatch.setattr(providers, "OPENROUTER_URL", str(server.make_url("/")).rstrip("/"))
            home = tmp_path / "codex"
            home.mkdir()
            # Inherited settings must not re-enable model execution or MCP.
            (home / "config.toml").write_text(
                "openai_base_url="
                + json.dumps(str(server.make_url("/")).rstrip("/"))
                + "\n"
                + "[features]\ncode_mode=true\nshell_tool=true\n[mcp_servers.inherited]\nurl="
                + json.dumps(str(server.make_url("/mcp")))
                + "\n"
            )
            monkeypatch.setenv("CODEX_HOME", str(home))
            monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-openrouter-key")
            monkeypatch.setenv("CIV_AUTH_SOCKET", "must-not-connect")
            monkeypatch.setenv("CIV_AUTH_TOKEN_FILE", "must-not-read")
            monkeypatch.delenv("CIV_OPENROUTER_KEY_FILE", raising=False)
            complete = asyncio.Event()

            async def notification(method, params):
                notices.append((method, params))
                if method in ("turn/completed", "service/disconnected", "service/transportFailure"):
                    stalled_release.set()
                    complete.set()

            process = CodexProcess(
                os.environ.get("CIV_TEST_CODEX", "codex"), tmp_path, str(server.make_url("/mcp")), None, notification
            )
            settings = {"provider": provider, "model": model, "reasoningEffort": "high"}
            options = {
                "cwd": str(tmp_path),
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "dynamicTools": [],
                "developerInstructions": "Return the structured decision.",
                **codex_thread_settings(settings, fast=model == "gpt-6-luna"),
            }
            try:
                await process.start("synthetic-player-token", settings)
                try:
                    thread = await process.request("thread/start", options)
                except agent_service.ServiceError:
                    pytest.fail(str(rpc_errors))
                assert thread["modelProvider"] == provider
                assert thread["model"] == model
                assert thread["reasoningEffort"] == "high"
                if model == "gpt-6-luna":
                    assert thread["serviceTier"] == "priority"
                for index in range(1 if disabled_exec else 2):
                    if index and resume:
                        await process.close()
                        process = CodexProcess(
                            os.environ.get("CIV_TEST_CODEX", "codex"),
                            tmp_path,
                            str(server.make_url("/mcp")),
                            None,
                            notification,
                        )
                        await process.start("synthetic-player-token", settings)
                        resumed = await process.request(
                            "thread/resume", {**options, "threadId": thread["thread"]["id"]}
                        )
                        assert resumed["modelProvider"] == provider and resumed["model"] == model
                        assert resumed["reasoningEffort"] == "high"
                    complete.clear()
                    try:
                        await process.request(
                            "turn/start",
                            {
                                "threadId": thread["thread"]["id"],
                                "approvalPolicy": "never",
                                "effort": "high",
                                "summary": "auto",
                                "outputSchema": contract.schema,
                                **(
                                    continuation({"confirmed": "receipt-sentinel"})
                                    if index
                                    else {"input": [{"type": "text", "text": "Choose."}]}
                                ),
                            },
                        )
                    except agent_service.ServiceError:
                        pytest.fail(str({"errors": rpc_errors, "gateRequests": gate_requests}))
                    await asyncio.wait_for(complete.wait(), 30)
                request_count = 1 if disabled_exec else 2
                assert len(requests) == request_count, notices
                if disabled_exec:
                    assert any(
                        method == "service/transportFailure" and params["reason"] == "PROVIDER_DECISION_REQUEST_LIMIT"
                        for method, params in notices
                    )
                if stall:
                    assert [p["reason"] for method, p in notices if method == "service/generationFailed"] == [
                        "REVIEW_OUTPUT_STALLED"
                    ]
                    assert [p["turn"]["status"] for method, p in notices if method == "turn/completed"] == [
                        "interrupted",
                        "completed",
                    ]
                    assert not any(method == "service/transportFailure" for method, _ in notices)
                assert not mcp_requests
                assert all(r["tools"] == [] and r["tool_choice"] == "none" for r in requests)
                assert all(r["model"] == model and r["reasoning"]["effort"] == "high" for r in requests)
                if model == "gpt-6-luna":
                    assert all(r["service_tier"] == "priority" for r in requests)
                assert all(DECISION_BASE_INSTRUCTIONS in json.dumps(r) for r in requests)
                assert all("You are Codex" not in json.dumps(r) for r in requests)
                assert all(
                    r["text"]["format"]["schema"] == contract.schema and r["text"]["format"]["strict"] is True
                    for r in requests
                )
                if not disabled_exec:
                    assert "receipt-sentinel" in json.dumps(requests[-1]["input"])
                usages = [p["tokenUsage"] for method, p in notices if method == "thread/tokenUsage/updated"]
                assert usages[-1]["total"]["inputTokens"] == 100 * (request_count - int(stall))
                assert usages[-1]["total"]["cachedInputTokens"] == 40 * (request_count - int(stall))
                (tmp_path / "provider-capture.json").write_text(
                    json.dumps(
                        {
                            "provider": provider,
                            "model": model,
                            "purpose": purpose,
                            "resume": resume,
                            "disabledExec": disabled_exec,
                            "whitespaceRecovery": stall,
                            "requests": requests,
                            "mcpRequests": len(mcp_requests),
                            "gateRequests": gate_requests,
                            "modelStatus": [p for method, p in notices if method == "service/modelStatus"],
                        },
                        indent=2,
                    )
                )
                capture = os.environ.get("CIV_TEST_CAPTURE_DIR")
                if capture:
                    target = Path(capture)
                    target.mkdir(parents=True, exist_ok=True)
                    (target / f"{model.replace('/', '_')}-{purpose}-{resume}-{disabled_exec}-{stall}.json").write_text(
                        json.dumps(
                            {
                                "requests": requests,
                                "mcpRequests": len(mcp_requests),
                                "gateRequests": gate_requests,
                                "modelStatus": [p for method, p in notices if method == "service/modelStatus"],
                            },
                            indent=2,
                        )
                    )
                assert ("GET", "responses") not in gate_requests
                assert not any(
                    method == "service/modelStatus" and params["state"] == "retrying" for method, params in notices
                )
            finally:
                stalled_release.set()
                await process.close()

    asyncio.run(run())
