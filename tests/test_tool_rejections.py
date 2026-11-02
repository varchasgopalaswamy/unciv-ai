"""Correctable model requests produce receipts without hiding lifecycle failures."""

import copy
import json

import pytest

from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.decision_contracts import DecisionContract
from civ_agent.memory_store import RecordStore
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from civ_agent.structured_decisions import DecisionLoop
from tests.test_agreement_lifecycle import AgreementHost, ended_event
from tests.test_decision_recovery import begin, saved_decision
from tests.test_diplomacy_contracts import opened
from tests.test_economy_optimization import managed
from tests.test_economy_roles import run_async


@pytest.mark.parametrize(
    "purpose,tool,arguments,code",
    [
        ("gameplay", "native_advice", {}, "NATIVE_ADVICE_DISABLED"),
        ("gameplay", "chat_delivery", {"deliveryId": "missing"}, "DIPLOMACY_ROUTING_DISABLED"),
        ("communication", "research_details", {}, "TOOL_NOT_PERMITTED"),
        ("communication", "player_command", {"kind": "END_TURN"}, "GAMEPLAY_NOT_PERMITTED"),
        ("gameplay", "unknown_tool", {}, "TOOL_NOT_PERMITTED"),
        ("gameplay", "finish_role", {}, "ACTIVE_ROLE_REQUIRED"),
        ("gameplay", "economy_request", {"text": "Build a Worker."}, "ACTIVE_ROLE_REQUIRED"),
        ("gameplay", "city_override", {"city": "city1", "reason": "Handle the city."}, "ACTIVE_ROLE_REQUIRED"),
        ("gameplay", "player_command", {"kind": "END_TURN", "revision": "invented"}, "INVALID_TOOL_ARGUMENTS"),
    ],
)
@run_async
async def test_definite_permission_rejections_are_replayable_without_host_actions(
    tmp_path, purpose, tool, arguments, code
):
    async with opened(tmp_path, purpose=purpose) as (gateway, job, host):
        result = await gateway.call(job, tool, arguments, "rejected")
        assert result["error"] == code and result["applied"] is False
        assert await gateway.call(job, tool, arguments, "rejected") == result
        assert not gateway.memory.pending() and not host.executed and not host.writes
        corrected = await gateway.call(job, "chat_history", {"query": "earlier"}, "corrected")
        assert "error" not in corrected


@run_async
async def test_long_history_query_is_rejected_before_store_and_can_be_corrected(tmp_path):
    async with opened(tmp_path) as (gateway, job, host):
        query = "q" * 201
        result = await gateway.call(job, "chat_history", {"query": query}, "too-long")
        assert result["error"] == "INVALID_TOOL_ARGUMENTS" and result["applied"] is False
        assert await gateway.call(job, "chat_history", {"query": query}, "too-long") == result
        loop = DecisionLoop(gateway, job, DecisionContract(job.purpose, await gateway.tools(job)), {})
        await loop.begin("Read earlier messages.")
        valid = {"kind": "inspect", "operations": [{"tool": "chat_history", "arguments": {"query": "q" * 200}}]}
        wire = json.loads(loop.contract.encode(valid))
        wire["decision"]["operations"][0]["arguments"]["query"] = query
        rejected = await loop.complete(json.dumps(wire))
        assert rejected["error"] == "INVALID_STRUCTURED_DECISION"
        await loop.begin(rejected)
        corrected = await loop.complete(loop.contract.encode(valid))
        assert corrected["receipts"][0]["status"] == "confirmed"
        assert not gateway.memory.pending() and not host.writes


@run_async
async def test_native_chat_without_observation_returns_refresh_receipt(tmp_path):
    async with managed(tmp_path) as m:
        loop = await begin(m, job=m.main)
        m.gateway.forget_observation(m.main)
        result = await loop.complete(
            loop.contract.encode(
                {
                    "kind": "act",
                    "operations": [
                        {
                            "tool": "chat_send",
                            "arguments": {
                                "to": "Greece",
                                "text": "A native diplomatic action.",
                                "nativeAction": {"kind": "DENOUNCE", "target": "Greece"},
                            },
                        }
                    ],
                }
            )
        )
        receipt = result["receipts"][0]
        assert receipt["status"] == "rejected"
        assert receipt["result"]["error"] == "OBSERVATION_REQUIRED"
        assert receipt["result"]["applied"] is False and not m.memory.pending()
        assert saved_decision(m.memory, result["decisionId"])["status"] != "uncertain"
        assert await loop.execute(recovery=True) == result
        await loop.begin(result)
        refreshed = await loop.complete(
            loop.contract.encode({"kind": "inspect", "operations": [{"tool": "player_turn", "arguments": {}}]})
        )
        assert refreshed["receipts"][0]["status"] == "confirmed"


@pytest.mark.parametrize("tool,arguments", [("agreement_read", {"agreement": "a1"}), ("agreement_list", {})])
@run_async
async def test_newer_agreement_event_rejects_stale_read_without_aborting_decision(tmp_path, tool, arguments):
    memory, io = CivilizationState(tmp_path / "private", "Rome"), ServiceIO()
    gateway = ProposalGateway(memory, io, tmp_path / "workspace")
    host = AgreementHost()
    try:
        job = await gateway.open("job", "communication", "g1", host, {})
        await gateway.call(job, tool, arguments, "initial")
        stale = await host.tool("player_communication", {**arguments, "op": tool})
        native = host.tool
        raced = False

        async def race(name, args):
            nonlocal raced
            if name == "player_communication" and args["op"] == tool and not raced:
                raced = True
                memory.ingest("g1", [ended_event()], 1)
                return copy.deepcopy(stale)
            return await native(name, args)

        host.tool = race
        loop = DecisionLoop(gateway, job, DecisionContract(job.purpose, await gateway.tools(job)), {})
        await loop.begin("Read current agreement terms.")
        decision = {"kind": "inspect", "operations": [{"tool": tool, "arguments": arguments}]}
        result = await loop.complete(loop.contract.encode(decision))
        receipt = result["receipts"][0]
        assert receipt["status"] == "rejected"
        assert receipt["result"]["error"] == "AGREEMENT_CHANGED"
        assert receipt["result"]["applied"] is False
        assert receipt["result"]["recovery"]["reads"] == [{"tool": tool, "arguments": arguments}]
        assert "agreement" not in receipt["result"] and "agreements" not in receipt["result"]
        scope = "observation:agreement/a1" if tool == "agreement_read" else "observation:agreements"
        with memory.transaction() as db:
            remembered = RecordStore(db).all(keys=[scope])[scope]["body"]
        assert (remembered["latestVersion"] if tool == "agreement_read" else remembered["latestVersions"]["a1"]) == 2
        assert await loop.execute(recovery=True) == result
        host.agreement.update(version=2, status="ENDED", remainingTurns=0)
        await loop.begin(result)
        current = await loop.complete(loop.contract.encode(decision))
        assert current["receipts"][0]["status"] == "confirmed"
        value = current["receipts"][0]["result"]
        assert (value["agreement"] if tool == "agreement_read" else value["agreements"][0])["version"] == 2
        assert not memory.pending() and not host.executed
    finally:
        await io.close()
        memory.engine.dispose()


@pytest.mark.parametrize("code", ["STALE_EPOCH", "STALE_JOB", "RECEIPT_UNCERTAIN", "UNKNOWN_INTERNAL_CONFLICT"])
@run_async
async def test_agreement_read_does_not_hide_lifecycle_or_uncertain_state(tmp_path, code):
    async with opened(tmp_path) as (gateway, job, host):
        native = host.tool

        async def tool(name, args):
            if name == "player_communication" and args["op"] == "agreement_list":
                return {"agreements": []}
            return await native(name, args)

        host.tool = tool
        observe = gateway.memory.observe

        def fail(key, body, job=None):
            if key == "agreements":
                raise StateConflict(code)
            return observe(key, body, job)

        gateway.memory.observe = fail
        with pytest.raises(StateConflict, match=code):
            await gateway.call(job, "agreement_list", {}, "read")
