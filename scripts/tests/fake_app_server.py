#!/usr/bin/env python3
"""Unbilled Codex app-server stand-in, speaking WebSocket over a private Unix socket."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
import uuid

from aiohttp import ClientSession, web


async def main():
    arguments = sys.argv[1:]
    if "app-server" not in arguments:
        raise RuntimeError("Fake executable only supports app-server")
    listen = arguments[arguments.index("--listen") + 1]
    if not listen.startswith("unix://"):
        raise RuntimeError("App-server control must use a private Unix socket")
    socket_path = listen.removeprefix("unix://")
    trace = Path(os.environ["CIV_TEST_RPC_TRACE"])
    token = os.environ["UNCIV_PLAYER_TOKEN"]
    context = ssl.create_default_context(cafile=os.environ["CIV_TEST_CA"])

    def record(method, params):
        value = dict(method=method, params=params, argv=arguments,
                     tokenHash=hashlib.sha256(token.encode()).hexdigest(),
                     operatorTokenPresent="UNCIV_OPERATOR_TOKEN" in os.environ, pid=os.getpid())
        with trace.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")

    async def connection(request):
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        current = {"thread": None, "turn": None, "task": None}

        async def complete(status):
            await websocket.send_json(dict(method="turn/completed", params=dict(
                threadId=current["thread"], turn=dict(id=current["turn"], status=status, items=[]),
            )))

        async def play(params):
            text = " ".join(item.get("text", "") for item in params["input"])
            civilization = re.search(r"Play (.*?)'s current game turn", text).group(1)
            common = dict(threadId=current["thread"], turnId=current["turn"])
            await websocket.send_json(dict(method="item/agentMessage/delta", params=dict(
                **common, itemId="before-" + current["turn"], delta="Checking current position.",
            )))
            await websocket.send_json(dict(method="item/completed", params=dict(**common, item=dict(
                id="before-" + current["turn"], type="agentMessage", text="Checking current position.", phase="commentary",
            ))))
            async with ClientSession() as client:
                async with client.post(os.environ["CIV_TEST_HOST"] + "/simulate", ssl=context,
                                       headers={"Authorization": "Bearer " + token},
                                       json=dict(civilization=civilization, threadId=current["thread"])) as response:
                    response.raise_for_status()
                    behavior = (await response.json())["behavior"]
            if behavior in {"hang", "after_handoff_hang"}:
                await asyncio.Event().wait()
            if behavior == "slow_report":
                await asyncio.sleep(4)
            if behavior == "crash":
                os._exit(1)
            message_id = "message-" + current["turn"]
            for delta in ("Settled ", "and chose research."):
                await websocket.send_json(dict(method="item/agentMessage/delta", params=dict(**common, itemId=message_id, delta=delta)))
            await websocket.send_json(dict(method="item/completed", params=dict(**common, item=dict(
                id=message_id, type="agentMessage", text="Settled and chose research.", phase="final_answer",
            ))))
            await websocket.send_json(dict(method="item/completed", params=dict(**common, item=dict(
                id="summary-" + current["turn"], type="reasoning", summary=["Visible strategy summary."], content=["RAW_REASONING_SENTINEL"],
            ))))
            if behavior == "many_events" or os.environ.get("CIV_TEST_MANY_EVENTS"):
                for number in range(130):
                    await websocket.send_json(dict(method="item/completed", params=dict(**common, item=dict(
                        id=f"tool-{number}", type="mcpToolCall", status="completed", result={"position": number}, error=None,
                    ))))
                await websocket.send_json(dict(method="item/completed", params=dict(**common, item=dict(
                    id="large-tool", type="mcpToolCall", status="completed", result={"largePayload": "z" * 200000}, error=None,
                ))))
            usage = dict(inputTokens=10, cachedInputTokens=2, outputTokens=3, reasoningOutputTokens=0, totalTokens=13)
            turns = sum(
                row.get("method") == "turn/start" and row.get("params", {}).get("threadId") == current["thread"]
                for row in (json.loads(line) for line in trace.read_text().splitlines())
            )
            total = {key: value * turns for key, value in usage.items()}
            await websocket.send_json(dict(method="thread/tokenUsage/updated", params=dict(
                threadId=current["thread"], turnId=current["turn"], tokenUsage=dict(last=usage, total=total),
            )))
            await complete("completed")

        async for message in websocket:
            data = json.loads(message.data)
            method, params = data["method"], data.get("params", {})
            record(method, params)
            if "id" not in data:
                continue
            result = {}
            if method == "initialize":
                result = {"userAgent": "unciv-test-fake-app-server"}
            elif method in {"thread/start", "thread/resume"}:
                current["thread"] = params.get("threadId") or str(uuid.uuid4())
                result = {"thread": {"id": current["thread"], "turns": []},
                          "model": params.get("model", "fake-configured-model"),
                          "reasoningEffort": params.get("config", {}).get("model_reasoning_effort", "medium")}
            elif method == "turn/start":
                if params["threadId"] != current["thread"]:
                    raise RuntimeError("Wrong conversation")
                current["turn"] = str(uuid.uuid4())
                result = {"turn": {"id": current["turn"], "status": "inProgress", "items": []}}
            elif method == "turn/interrupt":
                if current["task"]:
                    current["task"].cancel()
                await complete("interrupted")
            elif method not in {"thread/unsubscribe", "thread/archive"}:
                await websocket.send_json(dict(id=data["id"], error=dict(code=-32601, message="Unsupported test method")))
                continue
            await websocket.send_json(dict(id=data["id"], result=result))
            if method == "turn/start":
                current["task"] = asyncio.create_task(play(params))
        if current["task"]:
            current["task"].cancel()
        return websocket

    application = web.Application()
    application.router.add_route("*", "/{path:.*}", connection)
    runner = web.AppRunner(application)
    await runner.setup()
    await web.UnixSite(runner, socket_path).start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
