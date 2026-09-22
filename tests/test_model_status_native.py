"""Opt-in native Codex retry check against a local, unbilled Responses endpoint."""

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


@pytest.mark.skipif(not os.environ.get("CIV_TEST_NATIVE_CODEX"), reason="opt-in installed Codex retry check")
def test_native_idle_timeout_and_retry_reach_observer(tmp_path):
    async def run():
        attempts = 0
        release = asyncio.Event()
        done = asyncio.Event()
        notices = []

        async def respond(request):
            nonlocal attempts
            await request.json()
            attempts += 1
            if attempts == 1:
                # A live but quiet provider stream, like Rome's stalled request.
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                await response.write(b'data: {"type":"response.created","response":{"id":"resp_stalled"}}\n\n')
                await release.wait()
                return response
            events = [
                {"type": "response.created", "response": {"id": "resp_recovered"}},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "id": "msg_recovered",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Recovered."}],
                    },
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_recovered",
                        "status": "completed",
                        "output": [],
                        "usage": {"input_tokens": 100, "output_tokens": 1, "total_tokens": 101},
                    },
                },
            ]
            return web.Response(
                text="".join("data: " + json.dumps(event) + "\n\n" for event in events),
                content_type="text/event-stream",
            )

        async def notification(method, params):
            if method == "service/modelStatus":
                notices.append(params)
            elif method == "turn/completed":
                assert params["turn"]["status"] == "completed"
                done.set()

        app = web.Application()
        app.router.add_post("/v1/responses", respond)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        address = runner.addresses[0]
        assert isinstance(address, tuple)
        provider = (
            'model_providers.retry_probe={name="Local retry probe",'
            f'base_url="http://127.0.0.1:{address[1]}/v1",wire_api="responses",'
            "requires_openai_auth=false,stream_idle_timeout_ms=100,stream_max_retries=2}"
        )
        process = CodexProcess(os.environ["CIV_TEST_NATIVE_CODEX"], tmp_path, "", None, notification)
        process.temporary = tempfile.TemporaryDirectory(prefix="civ-retry-probe-")
        socket_path = Path(process.temporary.name) / "codex.sock"
        try:
            process.process = await asyncio.create_subprocess_exec(
                process.executable,
                "-c",
                'model_provider="retry_probe"',
                "-c",
                provider,
                "-c",
                'model="gpt-5.6-luna"',
                "-c",
                f"sqlite_home={json.dumps(str(tmp_path))}",
                "-c",
                f"log_dir={json.dumps(str(tmp_path))}",
                *player_tool_arguments(),
                *scheduled_prompt_arguments(),
                "app-server",
                "--listen",
                "unix://" + str(socket_path),
                cwd=tmp_path,
                env={**os.environ, "RUST_LOG": "off,codex_core::responses_retry=warn"},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            process.runtime_task = asyncio.create_task(process._runtime())
            async with asyncio.timeout(20):
                while not socket_path.exists():
                    assert process.process.returncode is None
                    await asyncio.sleep(0.05)
            process.session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket_path)))
            process.socket = await process.session.ws_connect("http://localhost/")
            process.reader_task = asyncio.create_task(process._read())
            await process.request(
                "initialize",
                {"clientInfo": {"name": "retry-probe", "version": "1"}, "capabilities": {"experimentalApi": True}},
            )
            thread = await process.request(
                "thread/start",
                {
                    "cwd": str(tmp_path),
                    "approvalPolicy": "never",
                    "sandbox": "workspace-write",
                    "model": "gpt-5.6-luna",
                    "modelProvider": "retry_probe",
                    "ephemeral": True,
                },
            )
            await process.request(
                "turn/start",
                {"threadId": thread["thread"]["id"], "input": [{"type": "text", "text": "Reply briefly."}]},
            )
            await asyncio.wait_for(done.wait(), 20)
            assert attempts == 2
            assert [item["state"] for item in notices] == ["retrying", "responding"]
            retry = notices[0]
            assert (
                retry["source"] == "stderr" and retry["attempt"] == 1 and retry["reason"] in {"timeout", "idle_timeout"}
            )
        finally:
            release.set()
            await process.close()
            await runner.cleanup()

    asyncio.run(run())
