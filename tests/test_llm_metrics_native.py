"""Opt-in real-runner timing check using a local, unbilled Responses stub."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from civ_agent.agent_service import CodexProcess
from civ_agent.agent_settings import player_tool_arguments, scheduled_prompt_arguments


@pytest.mark.skipif(not os.environ.get("CIV_TEST_NATIVE_CODEX"), reason="opt-in installed Codex timing check")
def test_native_runner_exports_actual_transport_and_turn_latency(tmp_path):
    async def run():
        completed = asyncio.Event()
        measured = asyncio.Event()
        metrics = {}

        async def respond(request):
            await request.json()
            await asyncio.sleep(0.2)  # Known upstream latency, independent of game work.
            events = [
                {"type": "response.created", "response": {"id": "resp_profile"}},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "id": "msg_profile",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Ready."}],
                    },
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_profile",
                        "status": "completed",
                        "output": [],
                        "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
                    },
                },
            ]
            return web.Response(
                text="".join("data: " + json.dumps(event) + "\n\n" for event in events),
                content_type="text/event-stream",
            )

        async def notification(method, params):
            if method == "turn/completed":
                assert params["turn"]["status"] == "completed"
                completed.set()
            elif method == "service/llmMetrics":
                metrics.update({row["metric"]: row for row in params["series"]})
                if {"codex.api_request.duration_ms", "codex.turn.e2e_duration_ms"} <= metrics.keys():
                    measured.set()

        app = web.Application()
        app.router.add_post("/v1/responses", respond)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        address = runner.addresses[0]
        assert isinstance(address, tuple)
        provider = (
            'model_providers.latency_probe={name="Latency probe",'
            f'base_url="http://127.0.0.1:{address[1]}/v1",wire_api="responses",'
            "requires_openai_auth=false,supports_websockets=false}"
        )
        process = CodexProcess(os.environ["CIV_TEST_NATIVE_CODEX"], tmp_path, "", None, notification)
        process.temporary = tempfile.TemporaryDirectory(prefix="civ-metrics-probe-")
        socket_path = Path(process.temporary.name) / "codex.sock"
        (tmp_path / "codex-home").mkdir()
        try:
            await process.metrics.start()
            process.process = await asyncio.create_subprocess_exec(
                process.executable,
                "-c",
                'model_provider="latency_probe"',
                "-c",
                provider,
                "-c",
                'model="gpt-5.6-luna"',
                *process.metrics.arguments(),
                *player_tool_arguments(),
                *scheduled_prompt_arguments(),
                "app-server",
                "--listen",
                "unix://" + str(socket_path),
                cwd=tmp_path,
                env={
                    **os.environ,
                    "CODEX_HOME": str(tmp_path / "codex-home"),
                    "RUST_LOG": "off",
                    "OTEL_METRIC_EXPORT_INTERVAL": "1000",
                },
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            async with asyncio.timeout(20):
                while not socket_path.exists():
                    if process.process.returncode is not None:
                        assert process.process.stderr is not None
                        raise AssertionError((await process.process.stderr.read()).decode(errors="replace"))
                    await asyncio.sleep(0.05)
            process.session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket_path)))
            process.socket = await process.session.ws_connect("http://localhost/")
            process.reader_task = asyncio.create_task(process._read())
            await process.request("initialize", {"clientInfo": {"name": "latency-probe", "version": "1"}})
            thread = await process.request(
                "thread/start",
                {
                    "cwd": str(tmp_path),
                    "approvalPolicy": "never",
                    "sandbox": "workspace-write",
                    "model": "gpt-5.6-luna",
                    "modelProvider": "latency_probe",
                    "ephemeral": True,
                },
            )
            await process.request(
                "turn/start",
                {"threadId": thread["thread"]["id"], "input": [{"type": "text", "text": "Reply briefly."}]},
            )
            await asyncio.wait_for(completed.wait(), 20)
            await asyncio.wait_for(measured.wait(), 10)
            assert metrics["codex.api_request.duration_ms"]["count"] == 1
            assert metrics["codex.api_request.duration_ms"]["sumMs"] >= 180
            assert metrics["codex.turn.e2e_duration_ms"]["sumMs"] >= 180
            assert "codex.responses_api_inference_time.duration_ms" not in metrics  # HTTP supplied no inference metric.
        finally:
            await process.close()
            await runner.cleanup()

    asyncio.run(run())
