"""Transient model failures retain their cause and never replay native actions."""

import asyncio
import copy
import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from sqlalchemy.exc import SQLAlchemyError

from civ_agent.agent_service import CodexProcess, ServiceError
from civ_agent.civilization_state import StateConflict
from civ_agent.container_supervisor import recoverable_model_failure
from civ_agent.decision_contracts import DecisionContract
from civ_agent.economy_roles import RequestDisposition
from civ_agent.economy_runtime import run_role
from civ_agent.monitoring import MonitorStore, path_for
from civ_agent.provider_usage import reconcile_provider_usage
from civ_agent.structured_decisions import GenerationRecoveryError, generation_event
from tests.structured_helpers import decision_text
from tests.test_agent_service import AgentServiceTestCase, FakeCodex
from tests.test_codex_process import Socket
from tests.test_decision_recovery import begin, command, saved_decision
from tests.test_economy_optimization import managed
from tests.test_economy_roles import run_async


@pytest.mark.parametrize("role", ["economy", "military"])
@pytest.mark.parametrize("reason", ["rateLimitExceeded", "serverOverloaded", "internalServerError"])
@run_async
async def test_specialist_capacity_failure_reaches_supervisor_without_immediate_retries(tmp_path, role, reason):
    notice = {"state": "failed", "reason": reason, "httpStatus": 429}
    runtime = SimpleNamespace(
        active_id="child", process=None, run=AsyncMock(side_effect=ServiceError("INCOMPLETE_GENERATION"))
    )
    parent = {"activationId": "parent", "epoch": "g1"}
    service = SimpleNamespace(
        **{role + "_runtime": runtime},
        records={"child": {"activationId": "child", "parentActivation": "parent", "modelStatus": notice}},
        failure=None,
        stop_event=asyncio.Event(),
    )
    coordinator = SimpleNamespace(committed=False, save=AsyncMock())
    with pytest.raises(ServiceError, match="MODEL_CAPACITY_UNAVAILABLE"):
        await run_role(service, coordinator, {}, parent, role)
    assert runtime.run.await_count == 1 and not coordinator.save.called
    assert parent["modelStatus"] == notice and parent["modelFailureJob"] == "child"
    assert service.stop_event.is_set()
    MonitorStore(path_for(tmp_path)).put(
        "state", "run", {"status": "paused", "inFlight": {"agentPhase": "FAILED", "modelStatus": parent["modelStatus"]}}
    )
    assert recoverable_model_failure(tmp_path) == reason


@pytest.mark.parametrize("error", [StateConflict("HOST_PROTOCOL_ERROR"), OSError("disk"), SQLAlchemyError("database")])
@run_async
async def test_committed_finished_role_distinguishes_failed_read_from_failed_write(error):
    runtime = SimpleNamespace(process=None, run=AsyncMock(side_effect=error))
    service = SimpleNamespace(
        military_runtime=runtime,
        gateway=SimpleNamespace(run_failure=None),
        memory=SimpleNamespace(pending=Mock(return_value=[])),
        io=SimpleNamespace(call=AsyncMock(return_value=[])),
        diagnostics=Mock(),
        failure=None,
        stop_event=asyncio.Event(),
    )
    coordinator = SimpleNamespace(committed=True, ready={"military": True})
    if isinstance(error, StateConflict):
        await run_role(service, coordinator, {}, {}, "military")
        assert service.failure is None and not service.stop_event.is_set()
    else:
        with pytest.raises(ServiceError, match="DURABLE_RECORD_FAILED"):
            await run_role(service, coordinator, {}, {}, "military")
    assert runtime.run.await_count == 1


@pytest.mark.parametrize("date", [None, 76])
@run_async
async def test_request_review_date_has_a_direct_nullable_wire_value(tmp_path, date):
    async with managed(tmp_path) as m:
        contract = DecisionContract("economy", await m.gateway.tools(m.economy))
        disposition = {
            "id": "request1",
            "status": "deferred",
            "reason": "Reassess after the worker finishes.",
            "reviewTurn": date,
        }
        wire = json.loads(contract.encode({"kind": "finish", "arguments": {"requests": [disposition]}}))
        assert wire["decision"]["arguments"]["requests"][0]["reviewTurn"] == date
        assert contract.parse(json.dumps(wire))["arguments"]["requests"][0]["reviewTurn"] == date
        # Existing direct callers may still omit the date.
        assert (
            RequestDisposition.model_validate({k: v for k, v in disposition.items() if k != "reviewTurn"}).reviewTurn
            is None
        )


@pytest.mark.parametrize("terminal", ["failed", "interrupted", "completed"])
@run_async
async def test_interrupted_provider_generation_is_stopped_before_continuing(tmp_path, terminal):
    seen = []

    async def notice(method, params):
        seen.append((method, params))

    process = CodexProcess("codex", tmp_path, "", None, notice)
    socket = Socket()
    process.socket = cast(aiohttp.ClientWebSocketResponse, socket)
    process.reader_task = asyncio.create_task(process._read())
    try:
        starting = asyncio.create_task(
            process.request("turn/start", {"threadId": "thread", "input": [], "outputSchema": {"type": "object"}})
        )
        rpc = await asyncio.wait_for(socket.outgoing.get(), 1)
        socket.feed({"id": rpc["id"], "result": {"turn": {"id": "turn"}}})
        await starting
        await process._provider_notice(
            "service/providerRequestBlocked",
            {
                "reason": "PROVIDER_FORWARD_FAILED",
                "threadId": "thread",
                "turnId": "turn",
                "stage": "upstream_read",
                "upstreamStatus": 200,
                "requestKind": "generation",
                "exceptionType": "ClientPayloadError",
                "forwardedBytes": 303498,
            },
        )
        interrupt = await asyncio.wait_for(socket.outgoing.get(), 1)
        assert interrupt["method"] == "turn/interrupt"
        socket.feed({"id": interrupt["id"], "result": {}})
        socket.feed(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "turnId": "turn",
                    "item": {"id": "late", "type": "agentMessage", "text": "late output"},
                },
            }
        )
        socket.feed(
            {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": terminal}}}
        )
        assert process.generation_stop_task is not None
        await asyncio.wait_for(asyncio.shield(process.generation_stop_task), 1)
    finally:
        await process.close()
    assert process.transport_error is None
    assert [p["reason"] for method, p in seen if method == "service/generationFailed"] == [
        "PROVIDER_RESPONSE_INTERRUPTED"
    ]
    assert not any(method == "item/completed" for method, _ in seen)
    assert [p["turn"]["status"] for method, p in seen if method == "turn/completed"] == [terminal]


@run_async
async def test_stream_replacement_preserves_confirmed_receipts_and_shared_retry_ceiling(tmp_path):
    async with managed(tmp_path) as m:
        loop = await begin(m, record={"threadId": "thread"})
        confirmed = await loop.complete(
            loop.contract.encode(
                {"kind": "act", "operations": [command("SET_CITY_FOCUS", target="city2", selection="FoodFocus")]}
            )
        )
        loop.delivered(confirmed)
        saved = copy.deepcopy(saved_decision(m.memory, confirmed["decisionId"]))
        await loop.begin(confirmed)
        for turn in ["first", "second"]:
            await loop.bind(turn)
            params = {"threadId": "thread", "turnId": turn, "reason": "PROVIDER_RESPONSE_INTERRUPTED"}
            await generation_event(m.gateway, loop.record, "service/generationFailed", params)
            await generation_event(
                m.gateway,
                loop.record,
                "turn/completed",
                {"threadId": "thread", "turn": {"id": turn, "status": "failed"}},
            )
            if turn == "first":
                request = await loop.begin(confirmed)
                assert request["outputSchema"] == loop.contract.schema
                assert loop.record["generationRetries"] == 1
            else:
                with pytest.raises(GenerationRecoveryError, match="STRUCTURED_GENERATION_RECOVERY_LIMIT"):
                    await loop.begin(confirmed)
        assert saved_decision(m.memory, confirmed["decisionId"]) == saved
        assert len(m.host.executed) == 1


class ContextRefreshTests(AgentServiceTestCase):
    async def test_context_refresh_keeps_job_and_usage_and_replaces_delivery_baselines(self):
        original = FakeCodex.request
        before = {}

        async def request(process, method, payload):
            if method == "turn/start" and not process.subordinate and not before:
                job = process.job
                before.update(process=process, job=job, scope=job.coordination_scope)
                job.trades["old-preview"] = {"terms": "old"}
                # One completed read uses most of the context window. Thread
                # totals deliberately differ from the last request's input.
                await process.notification(
                    "thread/tokenUsage/updated",
                    {
                        "threadId": process.thread_id,
                        "turnId": "large-context",
                        "tokenUsage": {
                            "last": {"inputTokens": 850},
                            "modelContextWindow": 1000,
                            "total": {
                                "inputTokens": 9000,
                                "cachedInputTokens": 200,
                                "outputTokens": 30,
                                "reasoningOutputTokens": 10,
                            },
                        },
                    },
                )
                await process.notification(
                    "item/completed",
                    {
                        "threadId": process.thread_id,
                        "turnId": "large-context",
                        "item": {
                            "id": "read",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": decision_text(
                                payload["outputSchema"],
                                {"kind": "inspect", "operations": [{"tool": "player_turn", "arguments": {}}]},
                            ),
                        },
                    },
                )
                await process.notification(
                    "turn/completed",
                    {"threadId": process.thread_id, "turn": {"id": "large-context", "status": "completed"}},
                )
                return {"turn": {"id": "large-context"}}
            if method == "turn/start" and not process.subordinate and process is not before.get("process"):
                self.assertTrue(before["process"].closed)
                self.assertIs(process.job, before["job"])
                self.assertNotEqual(process.job.coordination_scope, before["scope"])
                self.assertNotIn("old-preview", process.job.trades)
                self.assertIn("Play one turn", json.dumps(payload["input"]))
            return await original(process, method, payload)

        with patch.object(FakeCodex, "request", request):
            await self.service.start(self.payload())
            result = await self.terminal()
        self.assertEqual(result["phase"], "COMPLETED", result)
        record = self.service.records["activation-one"]
        self.assertEqual(record["contextRotations"], 1)
        self.assertEqual(record["previousThreadIds"], ["thread-one"])
        self.assertEqual(record["structuredRequests"], 2)
        self.assertEqual(record["usage"]["input_tokens"], 9010)
        self.assertEqual(record["usage"]["output_tokens"], 33)
        self.assertFalse(record["usageComplete"])  # No closed traces in this fake.


@pytest.mark.parametrize("missing", [False, True])
def test_context_refresh_reconciles_every_distinct_thread_without_losing_known_usage(missing):
    usage = {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "reasoning_output_tokens": 2}
    reader = Mock()
    reader.read.side_effect = [
        None if missing else {"usage": usage, "modelRequests": 2},
        {"usage": usage, "modelRequests": 1},
    ]
    record = {"previousThreadIds": ["old", "old"], "threadId": "new", "usage": {"input_tokens": 200}}
    assert reconcile_provider_usage(record, reader) is not missing
    assert reader.read.call_args_list == [(("old",),), (("new",),)]
    assert record["usage"]["input_tokens"] == 200
    assert record["usage"]["output_tokens"] == (10 if missing else 20)


@run_async
async def test_context_pressure_uses_current_input_not_cumulative_usage(tmp_path):
    async with managed(tmp_path) as m:
        loop = await begin(m, record={"threadId": "thread"})
        await loop.bind("turn")
        await generation_event(
            m.gateway,
            loop.record,
            "thread/tokenUsage/updated",
            {
                "threadId": "thread",
                "turnId": "turn",
                "tokenUsage": {
                    "last": {"inputTokens": 100},
                    "total": {"inputTokens": 1000000},
                    "modelContextWindow": 1000,
                },
            },
        )
        assert loop.record["contextInputTokens"] == 100
        assert loop.record["modelContextWindow"] == 1000
