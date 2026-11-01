"""A bad message reference rejects one receipt; it cannot become a disconnect."""

import shutil
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import StateConflict
from civ_agent.decision_contracts import DecisionContract
from civ_agent.diplomacy_routing import DiplomacyRouting
from civ_agent.memory_snapshots import load_snapshot
from civ_agent.structured_decisions import DecisionLoop, journal
from civ_agent.tool_failures import failure_detail, failure_report
from tests.test_diplomacy_contracts import opened
from tests.test_diplomacy_routing import event
from tests.test_economy_roles import run_async


def handled(memory, job):
    with memory.transaction() as db:
        saved = db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job.id))
        return load_snapshot(db, saved).get("handledRoutingEventIds", [])


async def loop_for(gateway, job):
    loop = DecisionLoop(gateway, job, DecisionContract(job.purpose, await gateway.tools(job)), {})
    await loop.begin("Delivered chat context")
    return loop


def read(messages=()):
    return {"tool": "chat_read", "arguments": {"handledMessages": list(messages)}}


@pytest.mark.parametrize("messages", [["7"], ["unseen"], ["mtq1dv", "7"]])
@run_async
async def test_wrong_ack_is_atomic_replayable_and_correctable(tmp_path, messages):
    async with opened(tmp_path / "working", purpose="gameplay") as (gateway, job, host):
        gateway.memory.routing_enabled = True
        gateway.routing = DiplomacyRouting(gateway, AsyncMock())
        host.list_tools = AsyncMock(
            return_value=[
                *await host.list_tools(),
                *[
                    {"name": name, "description": name, "inputSchema": {"type": "object"}}
                    for name in ("player_turn", "player_inspect")
                ],
            ]
        )
        incoming = event(7)
        incoming["body"]["message"] = "mtq1dv"  # Captured event/message ID confusion.
        gateway.memory.ingest("g1", [incoming], 7)
        await gateway.call(job, "chat_read", {}, "startup-read")
        loop = await loop_for(gateway, job)
        operations = [
            {**read(messages), "arguments": {**read(messages)["arguments"], "includeContext": True}},
            {"tool": "player_inspect", "arguments": {"subject": "NOTIFICATIONS", "limit": 20, "offset": 0}},
            {"tool": "player_turn", "arguments": {"view": "full"}},
        ]
        result = await loop.complete(loop.contract.encode({"kind": "inspect", "operations": operations}))
        assert [r["status"] for r in result["receipts"]] == ["rejected", "unexecuted", "unexecuted"]
        receipt = result["receipts"][0]
        assert receipt["result"]["error"] == "MESSAGES_NOT_DELIVERED_TO_JOB"
        assert receipt["result"]["applied"] is False and receipt["result"]["recoverable"] is True
        assert "messages[].body.message" in receipt["result"]["instruction"]
        assert not handled(gateway.memory, job)
        assert not host.writes
        assert await loop.execute() == result
        restarted = DecisionLoop(gateway, job, loop.contract, {})
        restarted.current = journal(gateway.memory, result["decisionId"])
        assert await restarted.execute(recovery=True) == result

        loop.delivered(result)
        await loop.begin(result)
        fresh = await loop.complete(loop.contract.encode({"kind": "inspect", "operations": [read()]}))
        loop.delivered(fresh)
        await loop.begin(fresh)
        corrected = await loop.complete(loop.contract.encode({"kind": "inspect", "operations": [read(["mtq1dv"])]}))
        assert corrected["receipts"][0]["status"] == "confirmed"
        assert len(handled(gateway.memory, job)) == 1
        # Even after a valid acknowledgment, the rejected operation is immutable.
        assert await restarted.execute(recovery=True) == result
        source = gateway.memory.root

    game = tmp_path / "game"
    shutil.copytree(source, game / "agent-services" / "Rome" / "civilization")
    report = failure_report([game], tool="chat_read")
    assert len(report["failures"]) == 1
    failure = report["failures"][0]
    assert failure["category"] == "state_changed"
    assert failure["decisionId"] == result["decisionId"]
    assert failure["executionStatus"] == "rejected"
    assert failure_detail(report, failure["id"])["result"]["applied"] is False


@pytest.mark.parametrize("observation", ["sibling_read", "host_read"])
@run_async
async def test_intermediate_reads_cannot_acknowledge_messages_not_delivered_to_model(tmp_path, observation):
    async with opened(tmp_path, purpose="gameplay") as (gateway, job, _):
        gateway.memory.routing_enabled = True
        gateway.routing = DiplomacyRouting(gateway, AsyncMock())
        loop = await loop_for(gateway, job)
        gateway.memory.ingest("g1", [event(8)], 8)
        operations = [read(["m8"])]
        if observation == "sibling_read":
            operations.insert(0, read())
        else:
            await gateway.chat_read(job, {})
        result = await loop.complete(loop.contract.encode({"kind": "inspect", "operations": operations}))
        assert result["receipts"][-1]["result"]["error"] == "MESSAGES_NOT_DELIVERED_TO_JOB"
        assert not handled(gateway.memory, job)
        if observation == "host_read":
            loop.delivered(result)
            await loop.begin(result)
            result = await loop.complete(loop.contract.encode({"kind": "inspect", "operations": [read()]}))
        loop.delivered(result)
        await loop.begin(result)
        corrected = await loop.complete(loop.contract.encode({"kind": "inspect", "operations": [read(["m8"])]}))
        assert corrected["receipts"][0]["status"] == "confirmed"
        assert len(handled(gateway.memory, job)) == 1


@pytest.mark.parametrize("failure", [StateConflict("HOST_PROTOCOL_ERROR"), OSError("private exception text")])
@run_async
async def test_unexpected_ack_failure_stays_uncertain(tmp_path, monkeypatch, failure):
    async with opened(tmp_path, purpose="gameplay") as (gateway, job, _):
        gateway.memory.routing_enabled = True
        gateway.routing = DiplomacyRouting(gateway, AsyncMock())
        gateway.memory.ingest("g1", [event(7)], 7)
        await gateway.call(job, "chat_read", {}, "startup-read")
        loop = await loop_for(gateway, job)

        def fail(*args):
            raise failure

        monkeypatch.setattr(gateway.memory, "acknowledge_messages", fail)
        with pytest.raises(type(failure)):
            await loop.complete(loop.contract.encode({"kind": "inspect", "operations": [read(["m7"])]}))
        assert loop.current is not None
        saved = journal(gateway.memory, loop.current["id"])
        assert saved is not None
        assert saved["status"] == "uncertain"
        assert saved["category"] == ("host" if isinstance(failure, StateConflict) else "transport")
        assert saved["error"] == type(failure).__name__
        assert saved["receipts"] == []


@run_async
async def test_gateway_rejects_wrong_ack_without_a_structured_loop(tmp_path):
    async with opened(tmp_path, purpose="gameplay") as (gateway, job, _):
        gateway.memory.routing_enabled = True
        gateway.routing = DiplomacyRouting(gateway, AsyncMock())
        response = await gateway.call(job, "chat_read", {"handledMessages": ["7"]}, "ack")
        assert response["error"] == "MESSAGES_NOT_DELIVERED_TO_JOB"
        assert response["applied"] is False
        assert await gateway.call(job, "chat_read", {"handledMessages": ["7"]}, "ack") == response
