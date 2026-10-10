#!/usr/bin/env python3
"""Unbilled Codex app-server stand-in, speaking WebSocket over a private Unix socket."""

import asyncio
import hashlib
import json
import os
import re
import ssl
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, web


async def exercise_trade(civilization, communication, view, call):
    """Script reasoning choices, while the real gateway owns all concurrency checks."""
    silk = {"type": "Luxury_Resource", "name": "Silk", "amount": 1, "duration": 25}
    gpt = {"type": "Gold_Per_Turn", "name": "Gold per turn", "amount": 2, "duration": 25}
    gold = {"type": "Gold", "name": "Gold", "amount": 20, "duration": -1}
    peace = (
        [{"type": "Treaty", "name": "Peace Treaty", "amount": 1, "duration": 10}]
        if (view.get("scenario") or view.get("context", {}).get("scenario", {})).get("peace")
        else []
    )
    counter = {
        "counterofferTo": "d1",
        "partner": "Rome",
        "give": peace + [gpt, gold],
        "receive": peace + [silk],
        "intent": {"reason": "Advance the test objective"},
    }
    if communication:
        await call("memory_search", {"type": "strategy"})
        await call("trade_read", {"proposalId": "d1"})
        if civilization == "Greece":
            await call(
                "memory_update",
                {
                    "changes": [
                        {
                            "op": "create",
                            "content": {
                                "type": "strategy",
                                "assessment": "Counteroffer prepared; gameplay resumes after settlement.",
                                "reasons": ["Carry out the stated objective"],
                                "reconsiderWhen": ["Objective achieved"],
                                "subjects": ["Rome"],
                            },
                        }
                    ]
                },
            )
            await call("trade_offer", counter)
            await call("chat_send", {"to": "Rome", "text": "Counteroffer prepared"})
        else:
            accepted = await call(
                "trade_decide",
                {"proposalId": "d1", "decision": "accept", "intent": {"reason": "Advance the test objective"}},
            )
            assert accepted["trade"]["status"] == "ENACTED"
        return {"behavior": "complete"}
    if view["turn"] == 0:
        if civilization == "Rome":
            settled = await call(
                "trade_offer",
                {
                    "partner": "Greece",
                    "give": peace + [silk],
                    "receive": peace + [{**gpt, "amount": 4}],
                    "intent": {"reason": "Advance the test objective"},
                },
            )
            assert settled["trade"]["status"] == "ENACTED"
        else:
            settled = await call("trade_read", {"proposalId": "d1"})
            assert settled["trade"]["status"] == "ENACTED"
    await call("player_turn", {})
    return await call("player_command", {"kind": "END_TURN"})


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
        value = dict(
            method=method,
            params=params,
            argv=arguments,
            tokenHash=hashlib.sha256(token.encode()).hexdigest(),
            operatorTokenPresent="UNCIV_OPERATOR_TOKEN" in os.environ,
            pid=os.getpid(),
        )
        with trace.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")

    async def connection(request):
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        current: dict[str, Any] = {"thread": None, "turn": None, "task": None, "rpcId": 0}

        async def complete(status):
            await websocket.send_json(
                dict(
                    method="turn/completed",
                    params=dict(
                        threadId=current["thread"],
                        turn=dict(id=current["turn"], status=status, items=[]),
                    ),
                )
            )

        async def report_usage():
            usage = dict(inputTokens=10, cachedInputTokens=2, outputTokens=3, reasoningOutputTokens=0, totalTokens=13)
            turns = sum(
                row.get("method") == "turn/start" and row.get("params", {}).get("threadId") == current["thread"]
                for row in (json.loads(line) for line in trace.read_text().splitlines())
            )
            total = {key: value * turns for key, value in usage.items()}
            await websocket.send_json(
                dict(
                    method="thread/tokenUsage/updated",
                    params=dict(
                        threadId=current["thread"],
                        turnId=current["turn"],
                        tokenUsage=dict(last=usage, total=total),
                    ),
                )
            )

        async def publish_history(common):
            message_id = "message-" + current["turn"]
            for delta in ("Settled ", "and chose research."):
                await websocket.send_json(
                    dict(method="item/agentMessage/delta", params=dict(**common, itemId=message_id, delta=delta))
                )
            await websocket.send_json(
                dict(
                    method="item/completed",
                    params=dict(
                        **common,
                        item=dict(
                            id=message_id,
                            type="agentMessage",
                            text="Settled and chose research.",
                            phase="final_answer",
                        ),
                    ),
                )
            )
            await websocket.send_json(
                dict(
                    method="item/completed",
                    params=dict(
                        **common,
                        item=dict(
                            id="summary-" + current["turn"],
                            type="reasoning",
                            summary=["Visible strategy summary."],
                            content=["RAW_REASONING_SENTINEL"],
                        ),
                    ),
                )
            )
            for number in range(130):
                await websocket.send_json(
                    dict(
                        method="item/completed",
                        params=dict(
                            **common,
                            item=dict(
                                id=f"tool-{number}",
                                type="mcpToolCall",
                                status="completed",
                                result={"position": number},
                                error=None,
                            ),
                        ),
                    )
                )
            await websocket.send_json(
                dict(
                    method="item/completed",
                    params=dict(
                        **common,
                        item=dict(
                            id="large-tool",
                            type="mcpToolCall",
                            status="completed",
                            result={"largePayload": "z" * 200000},
                            error=None,
                        ),
                    ),
                )
            )

        async def play(params):
            if params.get("outputSchema", {}).get("title") == "StrategicAdvice":
                report = {
                    "assessment": "Develop the starting position before choosing a victory path.",
                    "review": {
                        "horizonTurns": 5,
                        "outcomeAssessment": "No previous choices at opening.",
                        "plans": [
                            {
                                "name": "Explore",
                                "objective": "Learn about the starting area.",
                                "investment": "Use the starting units to scout before committing production.",
                                "expectedObservations": ["Find a possible settlement site within five turns."],
                                "firstActions": ["Explore the starting area."],
                                "relativeOutcome": "Better information could help claim useful land before rivals.",
                                "risks": "Explorers may encounter hostile units.",
                            }
                        ],
                        "preferredPlan": 1,
                        "diplomacy": {"assessment": "Reassess after scouting.", "initiatives": []},
                        "reconsiderWhen": "Reassess after scouting.",
                    },
                }
                await websocket.send_json(
                    dict(
                        method="item/completed",
                        params=dict(
                            threadId=current["thread"],
                            turnId=current["turn"],
                            item=dict(id="advice", type="agentMessage", phase="final_answer", text=json.dumps(report)),
                        ),
                    )
                )
                await report_usage()
                await complete("completed")
                return
            text = " ".join(item.get("text", "") for item in params["input"])
            continuation = text.startswith("The negotiation has finished.")
            if continuation:
                results = json.loads(text.split("\n", 1)[1])["toolResults"]
                assert results[0]["tool"] == "trade_offer"
                assert results[0]["result"]["trade"]["status"] == "ENACTED"
                record("test/negotiation_result", results[0])
                text = current["gameplayPrompt"]
            match = re.search(r"Play (.*?)'s current game turn", text)
            communication = match is None
            if not communication:
                current["gameplayPrompt"] = text
            if communication:
                match = re.search(
                    r"Pursue (.*?)'s diplomatic objectives in light of newly received communication", text
                )
            assert match is not None
            common = dict(threadId=current["thread"], turnId=current["turn"])
            await websocket.send_json(
                dict(
                    method="item/agentMessage/delta",
                    params=dict(
                        **common,
                        itemId="before-" + current["turn"],
                        delta="Checking current position.",
                    ),
                )
            )
            await websocket.send_json(
                dict(
                    method="item/completed",
                    params=dict(
                        **common,
                        item=dict(
                            id="before-" + current["turn"],
                            type="agentMessage",
                            text="Checking current position.",
                            phase="commentary",
                        ),
                    ),
                )
            )
            await report_usage()
            if os.environ.get("CIV_TEST_MANY_EVENTS"):
                await publish_history(common)
            mcp = next(argument for argument in arguments if argument.startswith("mcp_servers"))
            gateway = tomllib.loads(mcp)["mcp_servers"]["civ"]
            gateway_url = gateway["url"]
            async with ClientSession() as client:

                async def call(name, arguments_value):
                    current["rpcId"] += 1
                    async with client.post(
                        gateway_url,
                        ssl=context,
                        headers={**gateway.get("http_headers", {}), "Authorization": "Bearer " + token},
                        json={
                            "jsonrpc": "2.0",
                            "id": current["rpcId"],
                            "method": "tools/call",
                            "params": {"name": name, "arguments": arguments_value},
                        },
                    ) as response:
                        response.raise_for_status()
                        result = (await response.json())["result"]
                        assert result["content"] == [], "The configured model must receive each payload once"
                        value = result["structuredContent"]
                        record(
                            "test/tool",
                            {
                                "civilization": match.group(1),
                                "purpose": "communication" if communication else "gameplay",
                                "tool": name,
                                "result": value,
                            },
                        )
                        return value

                view = (
                    await call("chat_read", {"includeContext": True})
                    if communication
                    else await call("player_turn", {})
                )
                scenario = view.get("scenario") or view.get("context", {}).get("scenario", {})
                current["slowCompletion"] = scenario.get("testSlowCompletion", False)
                if scenario.get("testModelRetry"):
                    # Match the real warning which isn't emitted as a protocol event
                    # on the first automatic retry. Extra stderr must stay private.
                    print("PRIVATE_PROVIDER_HEADER=" + "x" * 80000, file=sys.stderr, flush=True)
                    print(
                        "WARN codex_core::responses_retry: stream disconnected - retrying sampling request "
                        f"(1/5 in 184ms)... turn_id={current['turn']} retries=1 max_retries=5 "
                        "sampling_error=stream disconnected before completion: idle timeout waiting for websocket "
                        "cookie=PRIVATE_COOKIE",
                        file=sys.stderr,
                        flush=True,
                    )
                    await asyncio.sleep(0.15)
                    await report_usage()  # Usage/heartbeats do not prove recovery.
                    await websocket.send_json(
                        dict(
                            method="item/agentMessage/delta",
                            params=dict(**common, itemId="recovered", delta="Continuing."),
                        )
                    )
                if scenario.get("testManyEvents") and not os.environ.get("CIV_TEST_MANY_EVENTS"):
                    await publish_history(common)
                if continuation:
                    result = await call("player_command", {"kind": "END_TURN"})
                elif scenario.get("name") == "CONCURRENT_TRADE_TEST":
                    assert "recovery.reads" in current["instructions"]
                    result = await exercise_trade(match.group(1), communication, view, call)
                else:
                    assert not communication
                    result = await call("player_command", {"kind": "END_TURN", "deferReason": current["thread"]})
                if result.get("behavior") == "hang":
                    await asyncio.Event().wait()
                if "error" in result:
                    raise RuntimeError("Gateway rejected fake gameplay")
                behavior = result["behavior"]
                if behavior == "slow_report":
                    assert "memoryPhase" not in result
                    await asyncio.sleep(4)
            if behavior in {"hang", "after_handoff_hang"}:
                await asyncio.Event().wait()
            if behavior == "crash":
                os._exit(1)
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
                current["instructions"] = params["developerInstructions"]
                result = {
                    "thread": {"id": current["thread"], "turns": []},
                    "model": params.get("model", "fake-configured-model"),
                    "modelProvider": params["modelProvider"],
                    "reasoningEffort": params.get("config", {}).get("model_reasoning_effort", "medium"),
                }
            elif method == "turn/start":
                if params["threadId"] != current["thread"]:
                    raise RuntimeError("Wrong conversation")
                current["turn"] = str(uuid.uuid4())
                result = {"turn": {"id": current["turn"], "status": "inProgress", "items": []}}
            elif method == "turn/interrupt":
                if current["task"]:
                    current["task"].cancel()
                    await asyncio.gather(current["task"], return_exceptions=True)
                if current.get("slowCompletion"):
                    await asyncio.sleep(4)
                await complete("interrupted")
            elif method not in {"thread/unsubscribe", "thread/archive"}:
                await websocket.send_json(
                    dict(id=data["id"], error=dict(code=-32601, message="Unsupported test method"))
                )
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
