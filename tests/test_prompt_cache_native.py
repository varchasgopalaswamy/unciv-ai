"""Opt-in installed-Codex wire check, using a local fake Responses endpoint.

Run with CIV_TEST_NATIVE_CODEX=/path/to/codex pytest -q -s tests/test_prompt_cache_native.py.
No model requests are sent to OpenAI; the endpoint returns a canned response.
"""

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
from civ_agent.startup_context import startup_prompt


@pytest.mark.skipif(not os.environ.get("CIV_TEST_NATIVE_CODEX"), reason="opt-in installed Codex capture")
def test_native_prefix_is_stable_across_fresh_private_workspaces(tmp_path):
    async def run():
        captures = asyncio.Queue()

        async def capture(request):
            captures.put_nowait(await request.json())
            events = [
                {"type": "response.created", "response": {"id": "resp_probe"}},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "id": "msg_probe",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Done."}],
                    },
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_probe",
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

        app = web.Application()
        app.router.add_post("/v1/responses", capture)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        address = runner.addresses[0]
        assert isinstance(address, tuple)
        port = address[1]
        provider = (
            'model_providers.cache_probe={name="Local capture",'
            f'base_url="http://127.0.0.1:{port}/v1",wire_api="responses",requires_openai_auth=false}}'
        )
        wires = []
        try:
            # Separate app-server processes, directories, civilizations and turns.
            for index in range(2):
                workspace = tmp_path / f"job-{index}"
                workspace.mkdir()
                # A job-local file must not become context for the game model.
                (workspace / "AGENTS.md").write_text(f"LOCAL_FILE_SENTINEL_{index}")

                async def notification(method, params):
                    pass

                process = CodexProcess(os.environ["CIV_TEST_NATIVE_CODEX"], workspace, "", None, notification)
                process.temporary = tempfile.TemporaryDirectory(prefix="civ-cache-probe-")
                socket_path = Path(process.temporary.name) / "codex.sock"
                try:
                    # Same model-facing settings as production, with a fake
                    # provider and no Civ MCP connection needed for this probe.
                    process.process = await asyncio.create_subprocess_exec(
                        process.executable,
                        "-c",
                        'model_provider="cache_probe"',
                        "-c",
                        provider,
                        "-c",
                        'model="gpt-5.6-luna"',
                        "-c",
                        'model_reasoning_effort="medium"',
                        "-c",
                        f"sqlite_home={json.dumps(str(tmp_path))}",
                        "-c",
                        f"log_dir={json.dumps(str(tmp_path))}",
                        *player_tool_arguments(),
                        *scheduled_prompt_arguments(),
                        "app-server",
                        "--listen",
                        "unix://" + str(socket_path),
                        cwd=workspace,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    async with asyncio.timeout(20):
                        while not socket_path.exists():
                            assert process.process.returncode is None
                            await asyncio.sleep(0.05)
                    process.session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket_path)))
                    process.socket = await process.session.ws_connect("http://localhost/")
                    process.reader_task = asyncio.create_task(process._read())
                    await process.request(
                        "initialize",
                        {
                            "clientInfo": {"name": "cache-probe", "version": "1"},
                            "capabilities": {"experimentalApi": True},
                        },
                    )
                    prepared = {
                        "coreTools": [],
                        "readInstructions": {},
                        "reads": {"player_turn": {"turn": index, "civilization": ("Rome", "Greece")[index]}},
                    }
                    prompt = startup_prompt(prepared, "gameplay", f"PRIVATE_MEMORY_{index}")
                    thread = await process.request(
                        "thread/start",
                        {
                            "cwd": str(workspace),
                            "approvalPolicy": "never",
                            "sandbox": "workspace-write",
                            "model": "gpt-5.6-luna",
                            "modelProvider": "cache_probe",
                            "ephemeral": True,
                            "config": {"model_reasoning_effort": "medium"},
                            "developerInstructions": prompt.instructions,
                        },
                    )
                    assert thread["model"] == "gpt-5.6-luna"
                    assert thread["reasoningEffort"] == "medium"
                    assert thread["approvalPolicy"] == "never"
                    assert thread["sandbox"]["type"] == "workspaceWrite"
                    await process.request(
                        "turn/start",
                        {
                            "threadId": thread["thread"]["id"],
                            "input": [{"type": "text", "text": prompt.user}],
                        },
                    )
                    wire = await asyncio.wait_for(captures.get(), 30)
                    assert wire["model"] == "gpt-5.6-luna"
                    assert str(workspace) not in json.dumps(wire["input"])
                    assert "LOCAL_FILE_SENTINEL" not in json.dumps(wire["input"])
                    prefix = []
                    for item in wire["input"]:
                        if item.get("role") != "developer":
                            break
                        prefix.append({key: value for key, value in item.items() if key != "id"})
                    assert prompt.instructions in [
                        block.get("text") for item in prefix for block in item.get("content", [])
                    ]
                    assert "PRIVATE_MEMORY" not in json.dumps(prefix)
                    users = [item for item in wire["input"] if item.get("role") == "user"]
                    assert len(users) == 1
                    assert users[0]["content"] == [{"type": "input_text", "text": prompt.user}]
                    wires.append(prefix)
                finally:
                    await process.close()
            assert wires[0] == wires[1]
            print(f"Identical developer/tool prefix across two native jobs: {len(json.dumps(wires[0]))} characters")
        finally:
            await runner.cleanup()

    asyncio.run(run())
