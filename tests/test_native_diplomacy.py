import asyncio
import copy

import pytest
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from civ_agent.tool_schemas import TOOL_MODELS
from tests.test_proposal_gateway import Host


class NativeHost(Host):
    def __init__(self):
        super().__init__()
        self.writes = []
        self.waits = []

    async def tool(self, name, args):
        if name == "player_communication":
            if args["op"] == "negotiation":
                self.waits.append(copy.deepcopy(args))
            if args["op"] in {"trade_offer", "friendship_offer", "send"}:
                if args["requestId"] in self.seen:
                    return copy.deepcopy(self.seen[args["requestId"]])
                self.writes.append(copy.deepcopy(args))
                if args["op"] == "send":
                    result = {"message": {"text": args["text"]}}
                    if args.get("nativeAction"):
                        result["nativeOutcome"] = {"action": args["nativeAction"]["kind"], "status": "ENACTED"}
                        self.revision = "after-native-action"
                else:
                    result = {"trade": {"proposal": "n1", "version": 1, "mode": "NATIVE_AI", "status": "PENDING"}}
                self.seen[args["requestId"]] = result
                return copy.deepcopy(result)
        return await super().tool(name, args)


@pytest.mark.parametrize("tool", ["trade_offer", "friendship_offer"])
def test_native_offer_returns_pending_without_waiting_for_the_next_ai_turn(tmp_path, tool):
    async def run():
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "private", "Rome"), io, tmp_path / "workspace")
        host = NativeHost()
        try:
            job = await gateway.open("game", "gameplay", "g1", host, {})
            await gateway.call(job, "player_turn", {}, "observe")
            args: JsonObject = {"partner": "China", "intent": {"reason": "Improve our relations"}}
            if tool == "trade_offer":
                args.update(give=[{"type": "Gold", "name": "Gold", "amount": 25, "duration": -1}], receive=[])
            result = await asyncio.wait_for(gateway.call(job, tool, args, "offer"), 1)
            assert result["trade"]["status"] == "PENDING" and "outcome" not in result
            assert job.observation is not None and result["stateUpdate"]["kind"] == "delta"
            assert not any(r.get("waitSeconds") for r in host.waits)
            assert await gateway.call(job, tool, args, "offer") == result
            assert len(host.writes) == 1
        finally:
            await io.close()

    asyncio.run(run())


def test_explicit_chat_action_uses_observed_revision_and_replays_once(tmp_path):
    async def run():
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "private", "Rome"), io, tmp_path / "workspace")
        host = NativeHost()
        try:
            job = await gateway.open("chat", "communication", "g1", host, {})
            args = {
                "to": "Greece",
                "text": "We denounce you.",
                "nativeAction": {"kind": "DENOUNCE", "target": "Greece"},
            }
            with pytest.raises(StateConflict, match="Read player_turn"):
                await gateway.call(job, "chat_send", args, "unobserved")
            assert host.writes == []
            await gateway.call(job, "chat_send", {"to": "Greece", "text": "We denounce your conduct."}, "plain")
            assert "nativeAction" not in host.writes[-1]
            await gateway.call(job, "player_turn", {}, "observe")
            result = await gateway.call(job, "chat_send", args, "native")
            assert host.writes[-1]["nativeAction"]["revision"] == "rev1"
            assert result["nativeOutcome"]["status"] == "ENACTED" and job.observation is None
            assert await gateway.call(job, "chat_send", args, "native") == result
            assert len(host.writes) == 2
        finally:
            await io.close()

    asyncio.run(run())


def test_incoming_major_demand_exposes_copyable_response_without_choosing_for_the_player(tmp_path):
    async def run():
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "private", "Rome"), io, tmp_path / "workspace")
        host = NativeHost()
        host.view["alerts"] = [
            {
                "handle": "alert1",
                "canAcknowledge": False,
                "nativeDiplomacy": {
                    "active": True,
                    "civilizationId": "Greece",
                    "responseCommand": None,
                    "options": [
                        {"name": name, "available": available}
                        for name, available in (("Accept", True), ("Refuse", True), ("Unavailable", False))
                    ],
                },
            }
        ]
        try:
            job = await gateway.open("game", "gameplay", "g1", host, {})
            view = await gateway.call(job, "player_turn", {}, "observe")
            accept, refuse, unavailable = view["alerts"][0]["nativeDiplomacy"]["options"]
            assert "responseTool" in accept and "responseTool" not in unavailable
            response = refuse["responseTool"]
            assert response["tool"] == "chat_send"
            assert "text" not in response["arguments"] and "intent" not in response["arguments"]
            args = {
                **response["arguments"],
                "text": "We decline.",
                "intent": {"reason": "Keep our expansion options open"},
            }
            TOOL_MODELS["chat_send"].model_validate(args)
            result = await gateway.call(job, response["tool"], args, "respond")
            assert host.writes[-1]["nativeAction"] == {
                "kind": "RESPOND_DIPLOMACY",
                "target": "alert1",
                "selection": "Refuse",
                "revision": "rev1",
            }
            assert result["nativeOutcome"]["status"] == "ENACTED"
            assert result["stateUpdate"]["kind"] == "delta"
        finally:
            await io.close()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["TRADE", "PEACE", "DECLARE_WAR", "SIGN_DEFENSIVE_PACT"])
def test_chat_does_not_create_an_alternative_trade_or_war_protocol(kind):
    with pytest.raises(ValidationError):
        TOOL_MODELS["chat_send"].model_validate({"text": "proposal", "nativeAction": {"kind": kind, "target": "China"}})


def test_mediated_peace_is_a_trade_term():
    value = TOOL_MODELS["trade_offer"].model_validate(
        {
            "partner": "Greece",
            "give": [{"type": "PeaceProposal", "name": "China", "amount": 1, "duration": -1}],
            "receive": [],
            "intent": {"reason": "Negotiate native mediated peace"},
        }
    )
    assert value.model_dump()["give"][0]["type"] == "PeaceProposal"
