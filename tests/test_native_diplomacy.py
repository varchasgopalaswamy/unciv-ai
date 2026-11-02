import asyncio
import copy

import pytest
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup
from civ_agent.tool_schemas import TOOL_MODELS
from tests.test_proposal_gateway import Host


class NativeHost(Host):
    def __init__(self):
        super().__init__()
        self.writes = []
        self.waits = []
        self.trade: JsonObject = {
            "proposal": "n1",
            "version": 1,
            "author": "China",
            "partner": "Rome",
            "mode": "NATIVE_AI",
            "status": "PENDING",
            "acceptAvailable": True,
            "declineAvailable": True,
            "withdrawAvailable": True,
            "counterofferAvailable": True,
        }
        self.shared = {**self.trade, "proposal": "d1", "author": "Greece", "mode": "SHARED"}

    async def tool(self, name, args):
        if name == "player_capabilities":
            return {"queries": [], "commands": []}
        if name == "victory_progress":
            return {"turn": 0, "victories": []}
        if name == "player_communication":
            if args["op"] == "state":
                result = await super().tool(name, args)
                return {**result, "trades": copy.deepcopy([self.trade, self.shared])}
            if args["op"] == "trade_options":
                return {
                    "partner": args["partner"],
                    "mode": "NATIVE_AI" if args["partner"] == "China" else "SHARED",
                    "available": True,
                    "canInitiate": True,
                    "friendship": {"available": True},
                    "give": [],
                    "receive": [],
                }
            if args["op"] == "trade_read":
                return {"trade": copy.deepcopy(self.trade if args["proposal"] == "n1" else self.shared)}
            if args["op"] == "negotiation":
                self.waits.append(copy.deepcopy(args))
            if args["op"] in {"trade_offer", "trade_decide", "friendship_offer", "send"}:
                if args["requestId"] in self.seen:
                    return copy.deepcopy(self.seen[args["requestId"]])
                self.writes.append(copy.deepcopy(args))
                if args["op"] == "send":
                    result = {"message": {"text": args["text"]}}
                    if args.get("nativeAction"):
                        result["nativeOutcome"] = {"action": args["nativeAction"]["kind"], "status": "ENACTED"}
                        self.revision = "after-native-action"
                else:
                    result = {"trade": copy.deepcopy(self.trade)}
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


@pytest.mark.parametrize(
    "tool,arguments",
    [("trade_decide", {"proposalId": "n1", "decision": decision}) for decision in ("accept", "decline", "withdraw")]
    + [
        (
            "trade_offer",
            {
                "partner": "China",
                "give": [{"type": "Gold", "name": "Gold", "amount": 1, "duration": -1}],
                "receive": [],
                **extra,
            },
        )
        for extra in ({}, {"counterofferTo": "n1"})
    ]
    + [("friendship_offer", {"partner": "China"})],
)
def test_native_trade_decisions_belong_to_gameplay(tmp_path, tool, arguments):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = NativeHost()
        arguments_with_intent = {**arguments, "intent": {"reason": "Consider cooperation"}}
        try:
            chat = await gateway.open("chat", "communication", "g1", host, {})
            game = await gateway.open("game", "gameplay", "g1", host, {})
            # Even a communication job on the active turn, with fresh state and
            # exact native terms, cannot act on the gameplay agent's request.
            for job in (chat, game):
                await gateway.call(job, "player_turn", {}, "turn")
                read = await gateway.call(job, "trade_read", {"proposalId": "n1"}, "terms")
                for choice in ("accept", "decline", "withdraw", "counteroffer"):
                    assert read["trade"][choice + "Available"] is (job is game)
                options = await gateway.call(job, "trade_options", {"partner": "China"}, "options")
                assert options["canInitiate"] is (job is game)
                assert options["friendship"]["available"] is (job is game)
            denied = await gateway.call(chat, tool, arguments_with_intent, "decision")
            assert denied["error"] == "GAMEPLAY_ONLY" and denied["applied"] is False
            assert "gameplay turn" in denied["instruction"]
            assert not host.writes and not memory.pending() and not gateway.failed.is_set()
            assert await gateway.call(chat, tool, arguments_with_intent, "decision") == denied
            allowed = await gateway.call(game, tool, arguments_with_intent, "decision")
            assert "error" not in allowed and len(host.writes) == 1
            assert host.writes[0]["op"] == tool
            assert not memory.pending()
        finally:
            await io.close()

    asyncio.run(run())


@pytest.mark.parametrize("include_history", [False, True])
def test_communication_inbox_excludes_native_offers_but_gameplay_keeps_them(tmp_path, include_history):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = NativeHost()
        events = [
            {"id": 1, "kind": "trade", "wake": False, "body": {**host.trade, "participants": ["Rome", "China"]}},
            {"id": 2, "kind": "trade", "wake": True, "body": {**host.shared, "participants": ["Rome", "Greece"]}},
        ]
        try:
            memory.bind("g1")
            memory.ingest("g1", events, 2)
            for purpose, expected in (("communication", ["d1"]), ("gameplay", ["n1", "d1"])):
                job = await gateway.open(purpose, purpose, "g1", host, {})
                prepared = await prepare_startup(gateway, job)
                assert [t["proposalId"] for t in prepared["reads"]["chat_read"]["trades"]] == expected
                assert [t["inspection"] for t in prepared["reads"]["chat_read"]["trades"]] == [
                    {"tool": "trade_read", "arguments": {"proposalId": proposal}} for proposal in expected
                ]
                inbox = await gateway.call(job, "chat_read", {"includeHistory": include_history}, "inbox")
                assert [t["proposalId"] for t in inbox["trades"]] == expected
                assert [e["body"]["proposalId"] for e in inbox["messages"]] == expected
            assert memory.chat_history("China")["events"][0]["body"]["proposal"] == "n1"
        finally:
            await io.close()

    asyncio.run(run())


def test_explicit_chat_action_uses_observed_revision_and_replays_once(tmp_path):
    async def run():
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "private", "Rome"), io, tmp_path / "workspace")
        host = NativeHost()
        try:
            job = await gateway.open("chat", "gameplay", "g1", host, {})
            args = {
                "to": "Greece",
                "text": "We denounce you.",
                "nativeAction": {"kind": "DENOUNCE", "target": "Greece"},
            }
            rejected = await gateway.call(job, "chat_send", args, "unobserved")
            assert rejected["error"] == "OBSERVATION_REQUIRED" and rejected["applied"] is False
            assert host.writes == []
            await gateway.call(job, "chat_send", {"to": "Greece", "text": "We denounce your conduct."}, "plain")
            assert "nativeAction" not in host.writes[-1]
            await gateway.call(job, "player_turn", {}, "observe")
            result = await gateway.call(job, "chat_send", args, "native")
            assert host.writes[-1]["nativeAction"]["revision"] == "rev1"
            assert result["nativeOutcome"]["status"] == "ENACTED" and job.observation is not None
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
