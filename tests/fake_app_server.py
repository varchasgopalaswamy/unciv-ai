#!/usr/bin/env python3
"""Unbilled Codex app-server stand-in, speaking WebSocket over a private Unix socket."""

import asyncio
import hashlib
import json
import os
import re
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

# The subprocess runs in an isolated role directory, outside the test package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def exercise_trade(civilization, communication, view, call, preloaded_trades):
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
        if "d1" not in preloaded_trades:
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
    await call("player_turn", {}, recover_baseline="missing")
    return await call("player_command", {"kind": "END_TURN"})


async def main():
    arguments = sys.argv[1:]
    economy = "economy" in Path.cwd().name
    if "app-server" not in arguments:
        raise RuntimeError("Fake executable only supports app-server")
    listen = arguments[arguments.index("--listen") + 1]
    if not listen.startswith("unix://"):
        raise RuntimeError("App-server control must use a private Unix socket")
    socket_path = listen.removeprefix("unix://")
    trace = Path(os.environ["CIV_TEST_RPC_TRACE"])
    token = os.environ["UNCIV_PLAYER_TOKEN"]

    def record(method, params):
        value = dict(
            method=("economy/" + method if economy else method),
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
                row.get("method") == ("economy/turn/start" if economy else "turn/start")
                and row.get("params", {}).get("threadId") == current["thread"]
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
                            phase="commentary",
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

        from civ_agent.decision_contracts import READS
        from tests.structured_helpers import decision_text

        choices = asyncio.Queue()
        pending_result = None
        script_task = None

        async def choose(name, arguments_value, *, recover_baseline=None):
            nonlocal pending_result
            future = asyncio.get_running_loop().create_future()
            if name == "finish_role" or name == "player_command" and arguments_value["kind"] == "END_TURN":
                decision = {"kind": "finish", "arguments": {k: v for k, v in arguments_value.items() if k != "kind"}}
            elif name == "economy_wait":
                decision = {"kind": "wait", "arguments": arguments_value}
            else:
                if name == "player_command":
                    arguments_value = {
                        "command": {k: v for k, v in arguments_value.items() if k not in {"intent", "reviewAfter"}},
                        **{k: v for k, v in arguments_value.items() if k in {"intent", "reviewAfter"}},
                    }
                decision = {
                    "kind": "inspect" if name in READS else "act",
                    "operations": [
                        {
                            "tool": name,
                            "arguments": arguments_value,
                            **({"recoverBaseline": recover_baseline} if recover_baseline else {}),
                        }
                    ],
                }
            await choices.put((decision, future))
            result = await future
            record(
                "test/tool",
                {
                    "civilization": current.get("civilization"),
                    "purpose": current.get("purpose"),
                    "tool": name,
                    "result": result,
                },
            )
            return result

        async def script(text):
            match = re.search(r"Play (.*?)'s current game turn", text)
            communication = match is None
            if communication:
                match = re.search(
                    r"Pursue (.*?)'s diplomatic objectives in light of newly received communication", text
                )
            assert match is not None
            current.update(civilization=match.group(1), purpose="communication" if communication else "gameplay")
            common = dict(threadId=current["thread"], turnId=current["turn"])
            await websocket.send_json(
                dict(
                    method="item/completed",
                    params=dict(
                        **common,
                        item=dict(
                            id="before-" + current["turn"],
                            type="agentMessage",
                            phase="commentary",
                            text="Checking current position.",
                        ),
                    ),
                )
            )
            if economy or "military" in Path.cwd().name:
                arguments = {}
                if economy and '"roleCompletion":' in text:
                    completion = json.JSONDecoder().raw_decode(text.split('"roleCompletion":', 1)[1])[0]
                    if workers := completion.get("idleWorkers", []):
                        arguments["workerSkips"] = [
                            {"targets": workers, "reason": "Deliberate hold in this lifecycle fixture."}
                        ]
                await choose("finish_role", arguments)
                return
            view = (
                await choose("chat_read", {"includeContext": True})
                if communication
                else await choose("player_turn", {}, recover_baseline="missing")
            )
            scenario = view.get("scenario") or view.get("context", {}).get("scenario", {})
            if scenario.get("testHangGameplay"):
                await choose("player_command", {"kind": "DISMISS_NOTIFICATIONS", "targets": ["n1"]})
                await asyncio.Event().wait()
            current["slowCompletion"] = scenario.get("testSlowCompletion", False)
            if current["slowCompletion"]:
                # Dispatch waits for the final response. Delay process shutdown
                # after completion to exercise concurrent host finalization.
                asyncio.get_running_loop().add_signal_handler(
                    signal.SIGTERM, lambda: asyncio.get_running_loop().call_later(1.8, os._exit, 0)
                )
            if scenario.get("testModelRetry"):
                print("PRIVATE_PROVIDER_HEADER=" + "x" * 80000, file=sys.stderr, flush=True)
                print(
                    "WARN codex_core::responses_retry: stream disconnected - retrying sampling request "
                    f"(1/5 in 184ms)... turn_id={current['turn']} retries=1 max_retries=5 "
                    "sampling_error=stream disconnected before completion: idle timeout waiting for websocket cookie=PRIVATE_COOKIE",
                    file=sys.stderr,
                    flush=True,
                )
                await asyncio.sleep(0.15)
                await websocket.send_json(
                    dict(
                        method="item/agentMessage/delta",
                        params=dict(
                            threadId=current["thread"], turnId=current["turn"], itemId="recovered", delta="Continuing."
                        ),
                    )
                )
            if scenario.get("testManyEvents") and not os.environ.get("CIV_TEST_MANY_EVENTS"):
                await publish_history(dict(threadId=current["thread"], turnId=current["turn"]))
            if scenario.get("name") == "CONCURRENT_TRADE_TEST":
                preloaded_trades = {}
                if communication and "Details already available:\n" in text:
                    details = json.JSONDecoder().raw_decode(text.split("Details already available:\n", 1)[1])[0]
                    preloaded_trades = {
                        detail["trade"]["proposalId"]: detail["trade"]
                        for detail in details
                        if "error" not in detail and (detail.get("query") or {}).get("tool") == "trade_read"
                    }
                    for proposal, trade in preloaded_trades.items():
                        record("test/preloaded_trade", {"proposalId": proposal, "version": trade["version"]})
                await exercise_trade(match.group(1), communication, view, choose, preloaded_trades)
                if communication:
                    await choose("finish_role", {})
            else:
                assert not communication
                await choose("finish_role", {"deferReason": current["thread"]})

        async def play(params):
            nonlocal script_task, pending_result
            schema = params["outputSchema"]
            common = dict(threadId=current["thread"], turnId=current["turn"])
            variants = schema["properties"]["decision"]["anyOf"]
            report = next(
                (v["properties"].get("report") for v in variants if "report" in v.get("properties", {})), None
            )
            if report is not None:
                if "review" in report["properties"]:
                    from tests.test_strategic_review import assessment

                    answer = assessment()
                else:
                    answer = {
                        name: True
                        if field.get("type") == "boolean"
                        else "Frozen tactical assessment and strategic approval."
                        for name, field in report["properties"].items()
                    }
                decision = {"kind": "submit_report", "report": answer}
            else:
                if script_task is None:
                    text = " ".join(item.get("text", "") for item in params["input"])
                    script_task = asyncio.create_task(script(text))
                elif "toolOutput" in params:
                    receipt = json.loads(params["toolOutput"]["output"])
                    assert pending_result is not None and not pending_result.done()
                    result = receipt.get("receipts", [{}])[0].get("result", receipt)
                    pending_result.set_result(result)
                take = asyncio.create_task(choices.get())
                done, _ = await asyncio.wait({take, script_task}, return_when=asyncio.FIRST_COMPLETED)
                if take not in done:
                    take.cancel()
                    await script_task
                    raise RuntimeError("Script ended without a role completion")
                decision, pending_result = take.result()
            if os.environ.get("CIV_TEST_MANY_EVENTS"):
                await publish_history(common)
            await websocket.send_json(
                dict(
                    method="item/completed",
                    params=dict(
                        **common,
                        item=dict(
                            id="decision-" + current["turn"],
                            type="agentMessage",
                            phase="final_answer",
                            text=decision_text(schema, decision),
                        ),
                    ),
                )
            )
            await report_usage()
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
            elif method == "config/read":
                result = {"config": {}}
            elif method == "account/read":
                result = {"account": {"type": "chatgpt"}}
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

                def failed(task):
                    if not task.cancelled() and task.exception() is not None:
                        record("test/error", {"error": str(task.exception())})
                        asyncio.create_task(complete("failed"))

                current["task"].add_done_callback(failed)
        if script_task is not None:
            script_task.cancel()
            await asyncio.gather(script_task, return_exceptions=True)
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
