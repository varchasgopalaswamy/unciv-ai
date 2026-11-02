"""Whitespace failures stop before a bounded, receipt-preserving continuation."""

import asyncio
import copy
import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from civ_agent.agent_service import CodexProcess, ServiceError
from civ_agent.civilization_state import StateConflict
from civ_agent.economy_runtime import run_role
from civ_agent.provider_usage import reconcile_provider_usage
from civ_agent.structured_decisions import DecisionLoop, GenerationRecoveryError, generation_event
from tests.test_codex_process import Socket
from tests.test_decision_recovery import begin, command, saved_decision
from tests.test_economy_optimization import managed
from tests.test_economy_roles import run_async


async def fail_generation(m, loop, turn="stalled", *, stopped=True):
    params = {"threadId": loop.record.get("threadId"), "turnId": turn, "reason": "REVIEW_OUTPUT_STALLED"}
    await generation_event(m.gateway, loop.record, "service/generationFailed", params)
    # Exercise failure arriving before turn/start's acknowledgment is bound.
    await loop.bind(turn)
    if stopped:
        await generation_event(
            m.gateway,
            loop.record,
            "turn/completed",
            {"threadId": params["threadId"], "turn": {"id": turn, "status": "interrupted"}},
        )


@pytest.mark.parametrize("terminal_status", ["interrupted", "completed"])
@pytest.mark.parametrize("phase,output", [(None, " " * 4097), ("commentary", '\\"}' * 2000)])
@run_async
async def test_stalled_interrupt_discards_late_output_and_fences_next_turn(tmp_path, terminal_status, phase, output):
    seen = []
    failed, completed = asyncio.Event(), asyncio.Event()

    async def notification(method, params):
        seen.append((method, params))
        if method == "service/generationFailed":
            failed.set()
        if method == "turn/completed":
            completed.set()

    process = CodexProcess("codex", tmp_path, "", None, notification)
    socket = Socket()
    process.socket = cast(aiohttp.ClientWebSocketResponse, socket)
    process.reader_task = asyncio.create_task(process._read())
    options = {"threadId": "thread", "outputSchema": {"type": "object"}, "input": []}
    common = {"threadId": "thread", "turnId": "first"}
    try:
        start = asyncio.create_task(process.request("turn/start", options))
        rpc = await asyncio.wait_for(socket.outgoing.get(), 1)
        socket.feed({"id": rpc["id"], "result": {"turn": {"id": "first"}}})
        await start
        socket.feed(
            {
                "method": "item/started",
                "params": {**common, "item": {"id": "answer", "type": "agentMessage", "phase": phase}},
            }
        )
        socket.feed(
            {
                "method": "item/agentMessage/delta",
                "params": {**common, "itemId": "answer", "delta": '{"decision":{' + output},
            }
        )
        interrupt = await asyncio.wait_for(socket.outgoing.get(), 1)
        assert interrupt["method"] == "turn/interrupt"
        assert interrupt["params"] == common
        await asyncio.wait_for(failed.wait(), 1)
        socket.feed(
            {
                "method": "item/completed",
                "params": {
                    **common,
                    "item": {"id": "answer", "type": "agentMessage", "text": '{"decision":{"kind":"finish"}}'},
                },
            }
        )
        socket.feed(
            {
                "method": "turn/completed",
                "params": {"threadId": "thread", "turn": {"id": "first", "status": terminal_status}},
            }
        )
        await asyncio.wait_for(process.generation_stopped.wait(), 1)
        assert not completed.is_set()
        next_turn = asyncio.create_task(process.request("turn/start", options))
        await asyncio.sleep(0)
        assert socket.outgoing.empty() and not next_turn.done()
        socket.feed({"id": interrupt["id"], "result": {}})
        await asyncio.wait_for(completed.wait(), 1)
        rpc = await asyncio.wait_for(socket.outgoing.get(), 1)
        assert rpc["method"] == "turn/start" and rpc["params"]["outputSchema"] == options["outputSchema"]
        socket.feed({"id": rpc["id"], "result": {"turn": {"id": "second"}}})
        await next_turn
        # Old frames remain discarded even after a new guard has been installed.
        socket.feed({"method": "item/agentMessage/delta", "params": {**common, "itemId": "answer", "delta": "late"}})
    finally:
        await process.close()
    assert not process.transport_error
    assert not any(method in {"item/agentMessage/delta", "item/completed"} for method, _ in seen)
    assert [method for method, _ in seen].count("service/generationFailed") == 1
    assert [method for method, _ in seen].count("turn/completed") == 1


@pytest.mark.parametrize("terminal_failure", [False, True])
@run_async
async def test_failed_or_unconfirmed_interrupt_forbids_another_generation(tmp_path, monkeypatch, terminal_failure):
    monkeypatch.setattr("civ_agent.agent_service.MODEL_INTERRUPT_TIMEOUT", 0.02)
    process = CodexProcess("codex", tmp_path, "", None, Mock())

    async def notification(*args):
        pass

    process.notification = notification
    socket = Socket()
    process.socket = cast(aiohttp.ClientWebSocketResponse, socket)
    process.reader_task = asyncio.create_task(process._read())
    options = {"threadId": "thread", "outputSchema": {"type": "object"}, "input": []}
    try:
        start = asyncio.create_task(process.request("turn/start", options))
        rpc = await asyncio.wait_for(socket.outgoing.get(), 1)
        socket.feed({"id": rpc["id"], "result": {"turn": {"id": "first"}}})
        await start
        socket.feed(
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread", "turnId": "first", "itemId": "answer", "delta": " " * 4097},
            }
        )
        interrupt = await asyncio.wait_for(socket.outgoing.get(), 1)
        socket.feed({"id": interrupt["id"], "result": {}})
        if terminal_failure:
            socket.feed(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread", "turn": {"id": "first", "status": "failed"}},
                }
            )
        # An interrupt acknowledgment alone does not confirm generation exit.
        with pytest.raises(
            ServiceError, match="GENERATION_RECOVERY_UNSAFE" if terminal_failure else "GENERATION_STOP_UNCONFIRMED"
        ):
            await process.request("turn/start", options)
        assert socket.outgoing.empty()
    finally:
        await process.close()


@run_async
async def test_whitespace_interrupt_does_not_wait_for_blocked_history(tmp_path):
    blocked, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen = []

    async def notification(method, params):
        if method == "item/started":
            blocked.set()
            await release.wait()
        seen.append(method)
        if method == "turn/completed":
            completed.set()

    process = CodexProcess("codex", tmp_path, "", None, notification)
    socket = Socket()
    process.socket = cast(aiohttp.ClientWebSocketResponse, socket)
    process.reader_task = asyncio.create_task(process._read())
    common = {"threadId": "thread", "turnId": "first"}
    try:
        start = asyncio.create_task(
            process.request("turn/start", {"threadId": "thread", "outputSchema": {"type": "object"}})
        )
        rpc = await asyncio.wait_for(socket.outgoing.get(), 1)
        socket.feed({"id": rpc["id"], "result": {"turn": {"id": "first"}}})
        await start
        socket.feed(
            {
                "method": "item/started",
                "params": {**common, "item": {"id": "answer", "type": "agentMessage", "phase": "final_answer"}},
            }
        )
        await asyncio.wait_for(blocked.wait(), 1)
        for _ in range(100):
            socket.feed(
                {"method": "item/agentMessage/delta", "params": {**common, "itemId": "answer", "delta": " " * 1000}}
            )
        interrupt = await asyncio.wait_for(socket.outgoing.get(), 1)
        assert interrupt["method"] == "turn/interrupt" and not seen
        socket.feed({"id": interrupt["id"], "result": {}})
        socket.feed(
            {
                "method": "turn/completed",
                "params": {"threadId": "thread", "turn": {"id": "first", "status": "interrupted"}},
            }
        )
        assert process.generation_stop_task is not None
        await asyncio.wait_for(asyncio.shield(process.generation_stop_task), 1)
        assert not completed.is_set() and process.notification_peak_count < 10
        release.set()
        await asyncio.wait_for(completed.wait(), 1)
        assert seen.index("service/generationFailed") < seen.index("turn/completed")
    finally:
        release.set()
        await process.close()


@run_async
async def test_retry_preserves_receipts_and_limit_survives_controller_restart(tmp_path):
    async with managed(tmp_path) as m:
        record = {"threadId": "thread"}
        loop = await begin(m, record=record)
        result = await loop.complete(
            loop.contract.encode(
                {"kind": "act", "operations": [command("SET_CITY_FOCUS", target="city2", selection="FoodFocus")]}
            )
        )
        loop.delivered(result)
        confirmed = copy.deepcopy(saved_decision(m.memory, result["decisionId"]))
        await loop.begin(result)
        assert loop.current is not None
        failed_id = loop.current["id"]
        await fail_generation(m, loop)
        failed = saved_decision(m.memory, failed_id)
        assert failed["status"] == "generation_failed" and failed["generationStopped"]
        assert failed["error"] == "REVIEW_OUTPUT_STALLED"
        with pytest.raises(GenerationRecoveryError, match="GENERATION_RESPONSE_DISCARDED"):
            await loop.complete(loop.contract.encode({"kind": "finish", "arguments": {}}))
        record.pop("generationFailure")  # Journal commit survived; lifecycle save did not.
        restarted = DecisionLoop(m.gateway, m.economy, loop.contract, record)
        response = await restarted.begin(result)
        feedback = json.loads(response["toolOutput"]["output"])
        assert feedback["context"] == result and feedback["applied"] is False
        assert response["outputSchema"] == loop.contract.schema
        assert restarted.current is not None
        assert restarted.current["retryOf"] == failed_id and restarted.current["repair"]
        assert len(m.host.executed) == 1
        assert saved_decision(m.memory, result["decisionId"]) == confirmed
        await fail_generation(m, restarted, "stalled-again")
        restarted = DecisionLoop(m.gateway, m.economy, loop.contract, record)
        with pytest.raises(GenerationRecoveryError, match="STRUCTURED_GENERATION_RECOVERY_LIMIT"):
            await restarted.begin("Continue")
        assert record["generationRetries"] == 1 and record["structuredRequests"] == 3
        assert len(m.host.executed) == 1


@pytest.mark.parametrize("unsafe", ["not_stopped", "transport", "subsequent_transport", "has_decision", "rewound"])
@run_async
async def test_retry_requires_stopped_generation_and_current_epoch(tmp_path, unsafe):
    async with managed(tmp_path) as m:
        loop = await begin(m, record={"threadId": "thread"})
        await fail_generation(m, loop, stopped=unsafe != "not_stopped")
        if unsafe == "subsequent_transport":
            await generation_event(
                m.gateway,
                loop.record,
                "service/requestFailed",
                {"threadId": "thread", "turnId": "stalled", "reason": "connectionError"},
            )
        if unsafe in {"transport", "has_decision"}:
            assert loop.current is not None
            loop.current = saved_decision(m.memory, loop.current["id"])
            if unsafe == "transport":
                loop.current["error"] = "CODEX_DISCONNECTED"
            else:
                loop.current["decision"] = {"kind": "finish"}
            await loop.save()
        if unsafe == "rewound":
            checkpoint = tmp_path / "checkpoint.json"
            digest = m.memory.backup(checkpoint)
            m.memory.restore(checkpoint, digest, "g2")
        with pytest.raises(
            (GenerationRecoveryError, StateConflict), match="GENERATION_RECOVERY_UNSAFE|DECISION_CONTEXT_EXPIRED"
        ):
            await loop.begin("Continue")
        assert loop.record["structuredRequests"] == 1
        assert not m.host.executed


@pytest.mark.parametrize("known_requests,complete", [(1, False), (2, True)])
@pytest.mark.parametrize("failure_field", ["generationFailures", "providerRejections"])
def test_successful_retry_does_not_hide_missing_failed_request_usage(known_requests, complete, failure_field):
    record = {
        failure_field: {"decision:first": {"reason": "REVIEW_OUTPUT_STALLED"}},
        "providerRequests": {"r1": "generation", "r2": "generation"},
    }
    reader = Mock()
    reader.read.return_value = {
        "modelRequests": known_requests,
        "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 4, "reasoning_output_tokens": 0},
    }
    assert reconcile_provider_usage(record, reader) is complete
    assert record["providerUsageReconciled"] is complete
    assert record["modelRequests"] == 2


@pytest.mark.parametrize("role", ["economy", "military"])
@pytest.mark.parametrize("code", ["STRUCTURED_GENERATION_RECOVERY_LIMIT", "PROMPT_POLICY_REJECTED"])
@run_async
async def test_specialist_retry_loop_cannot_multiply_generation_recovery(tmp_path, role, code):
    runtime = SimpleNamespace(process=None, run=AsyncMock(side_effect=GenerationRecoveryError(code)))
    service = SimpleNamespace(**{role + "_runtime": runtime}, failure=None, stop_event=asyncio.Event())
    coordinator = SimpleNamespace(save=AsyncMock(), committed=False)
    with pytest.raises(GenerationRecoveryError, match=code):
        await run_role(service, coordinator, {}, {}, role)
    assert runtime.run.await_count == 1 and not coordinator.save.called
    assert service.failure == code and service.stop_event.is_set()
