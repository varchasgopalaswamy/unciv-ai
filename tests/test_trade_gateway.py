import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from civ_agent.chat_runtime import ChatRuntime
from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from tests.test_proposal_gateway import Host


class HoldingHost(Host):
    """One deterministic hold; events release application waits, without model polling."""

    def __init__(self):
        super().__init__()
        self.active = False
        self.waiting = asyncio.Event()
        self.settled = asyncio.Event()
        self.writes = []
        self.trade: JsonObject = {
            "proposal": "d1",
            "version": 1,
            "author": "Rome",
            "partner": "Greece",
            "status": "PENDING",
            "clauses": [],
        }

    async def tool(self, name, args):
        if name == "player_command" and self.active:
            return {"error": {"code": "NEGOTIATION_ACTIVE"}}
        if name == "player_communication":
            if args["op"] == "negotiation":
                if self.active and args.get("waitSeconds"):
                    self.waiting.set()
                    await self.settled.wait()
                return {"negotiation": {"active": self.active}}
            if args["op"] == "trade_read":
                return {"trade": copy.deepcopy(self.trade)}
            if args["op"] == "trade_offer":
                self.writes.append(copy.deepcopy(args))
                self.active = True
                return {"trade": copy.deepcopy(self.trade)}
            if args["op"] == "trade_decide":
                self.writes.append(copy.deepcopy(args))
                self.active = False
                self.trade.update(status="ENACTED", outcome={"action": "ENACT_TRADE"})
                self.revision = "after-trade"
                self.settled.set()
                return {"trade": copy.deepcopy(self.trade), "outcome": self.trade["outcome"]}
        return await super().tool(name, args)


def test_game_command_waits_outside_dispatch_and_requires_a_new_decision(tmp_path):
    async def run():
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "private", "Rome"), io, tmp_path / "workspace")
        host = HoldingHost()
        try:
            game = await gateway.open("game", "gameplay", "g1", host, {})
            chat = await gateway.open("chat", "communication", "g1", host, {})
            await gateway.call(game, "player_turn", {}, "observe")
            host.active = True
            pending = asyncio.create_task(gateway.call(game, "player_command", {"kind": "END_TURN"}, "old"))
            await asyncio.wait_for(host.waiting.wait(), 1)
            assert not pending.done() and not gateway.dispatch.locked() and not host.executed
            await gateway.call(chat, "trade_read", {"proposal": "d1"}, "read")
            await gateway.call(
                chat,
                "trade_decide",
                {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test objective"}},
                "accept",
            )
            result = await asyncio.wait_for(pending, 1)
            assert result["error"] == "OBSERVATION_REQUIRED" and result["applied"] is False
            assert host.executed == []
            await gateway.call(game, "player_turn", {}, "refresh")
            assert await gateway.call(game, "player_command", {"kind": "END_TURN"}, "old") == result
            await gateway.call(game, "player_command", {"kind": "END_TURN"}, "new-decision")
            assert len(host.executed) == 1 and host.executed[0]["revision"] == "after-trade"
        finally:
            await io.close()

    asyncio.run(run())


def test_interrupted_offer_wait_replays_receipt_without_reopening_negotiation(tmp_path):
    async def run():
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "private", "Rome"), io, tmp_path / "workspace")
        host = HoldingHost()
        try:
            game = await gateway.open("game", "gameplay", "g1", host, {})
            arguments = {
                "partner": "Greece",
                "give": [{"type": "Gold", "name": "Gold", "amount": 1, "duration": -1}],
                "receive": [],
                "intent": {"reason": "Advance the test objective"},
            }
            pending = asyncio.create_task(gateway.call(game, "trade_offer", arguments, "offer"))
            await asyncio.wait_for(host.waiting.wait(), 1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert game.wait_started is None and not gateway.dispatch.locked()
            # Counteroffer arrival and acceptance can run while the gameplay caller is interrupted.
            host.trade.update(author="Greece", partner="Rome", version=2)
            chat = await gateway.open("chat", "communication", "g1", host, {})
            await gateway.call(chat, "trade_read", {"proposal": "d1"}, "read")
            await gateway.call(
                chat,
                "trade_decide",
                {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test objective"}},
                "accept",
            )
            result = await gateway.call(game, "trade_offer", arguments, "offer")
            assert result["trade"]["status"] == "ENACTED" and result["trade"]["version"] == 2
            assert result["outcome"]["action"] == "ENACT_TRADE"
            assert await gateway.call(game, "trade_offer", arguments, "offer") == result
            assert [request["op"] for request in host.writes] == ["trade_offer", "trade_decide"]
        finally:
            await io.close()

    asyncio.run(run())


class TradeHost(Host):
    def __init__(self):
        super().__init__()
        self.trade: JsonObject = {
            "proposal": "d1",
            "version": 1,
            "author": "Greece",
            "partner": "Rome",
            "clauses": [
                {"from": "Greece", "to": "Rome", "item": {"type": "Gold", "name": "Gold", "amount": 20, "duration": -1}}
            ],
            "status": "PENDING",
        }
        self.writes = []

    async def tool(self, name, args):
        if name == "player_communication":
            if args["op"] == "trade_read":
                return {"trade": copy.deepcopy(self.trade), "history": []}
            if args["op"] == "receipt":
                return copy.deepcopy(self.seen.get(args["requestId"], {"error": "NOT_ENACTED"}))
            if args["op"] in {"trade_decide", "trade_offer"}:
                if args["requestId"] in self.seen:
                    return copy.deepcopy(self.seen[args["requestId"]])
                self.writes.append(copy.deepcopy(args))
                if args.get("version") != self.trade["version"]:
                    result = {"error": "STALE_REVISION"}
                else:
                    result = {"trade": copy.deepcopy(self.trade), "outcome": {"confirmation": {"source": "engine"}}}
                self.seen[args["requestId"]] = result
                return result
        return await super().tool(name, args)


def test_war_consent_rechecks_observed_target_relations_before_dispatch(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = TradeHost()
        host.trade["clauses"].append(
            {
                "from": "Rome",
                "to": "Greece",
                "item": {"type": "WarDeclaration", "name": "France", "amount": 1, "duration": -1},
            }
        )
        host.view["diplomacy"] = {"knownCivilizations": [{"civilizationId": "France", "atWar": False}]}
        try:
            old = await gateway.open("old", "communication", "g1", host, {})
            observer = await gateway.open("observer", "gameplay", "g1", host, {})
            await gateway.call(old, "player_turn", {}, "observe")
            await gateway.call(old, "trade_read", {"proposal": "d1"}, "terms")
            host.view["diplomacy"]["knownCivilizations"][0]["atWar"] = True
            host.revision = "after-war"
            await gateway.call(observer, "player_turn", {}, "new-observation")
            result = await gateway.call(
                old,
                "trade_decide",
                {"proposal": "d1", "decision": "accept", "intent": {"reason": "Join the war"}},
                "accept",
            )
            assert result["error"] == "STALE_ASSUMPTIONS" and result["applied"] is False
            assert not host.writes
            assert result["changedRecords"] == ["observation:diplomacy/France"]
            assert {"tool": "player_turn", "arguments": {}} in result["recovery"]["reads"]
        finally:
            await io.close()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["economic", "peace", "bilateral", "third_party"])
def test_trade_acceptance_uses_the_jobs_inspected_version_despite_other_reads(tmp_path, kind):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = TradeHost()
        if kind == "third_party":
            host.trade["clauses"].append(
                {
                    "from": "Rome",
                    "to": "Greece",
                    "item": {"type": "WarDeclaration", "name": "France", "amount": 1, "duration": -1},
                }
            )
            host.trade["yourWarDeclarations"] = [
                {"targetCivilization": "France", "confirmation": ["Declare war on France?"]}
            ]
        if kind == "bilateral":
            host.trade["clauses"] += [
                {"from": a, "to": b, "item": {"type": "Treaty", "name": name, "amount": amount, "duration": 25}}
                for a, b in (("Rome", "Greece"), ("Greece", "Rome"))
                for name, amount in (("Research Agreement", 166), ("Defensive Pact", 1))
            ]
            host.trade["clauses"].append(
                {
                    "from": "Rome",
                    "to": "Greece",
                    "item": {"type": "City", "name": "tc1", "amount": 1, "duration": -1},
                    "city": {"name": "Antium", "population": 3},
                }
            )
        if kind == "peace":
            host.trade["clauses"] += [
                {"from": a, "to": b, "item": {"type": "Treaty", "name": "Peace Treaty", "amount": 1, "duration": 10}}
                for a, b in (("Rome", "Greece"), ("Greece", "Rome"))
            ]
        try:
            old = await gateway.open("old", "gameplay", "g1", host, {})
            chat = await gateway.open("chat", "communication", "g1", host, {})
            with pytest.raises(StateConflict, match="trade_read"):
                await gateway.call(
                    old,
                    "trade_decide",
                    {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test objective"}},
                    "unseen",
                )
            shown = await gateway.call(old, "trade_read", {"proposal": "d1"}, "read")
            assert shown["trade"]["clauses"][0]["item"]["amount"] == 20
            host.trade["version"] = 2
            host.trade["clauses"][0]["item"]["amount"] = 40
            if kind == "third_party":
                host.trade["clauses"][1]["item"]["name"] = "Egypt"
                host.trade["yourWarDeclarations"] = [
                    {"targetCivilization": "Egypt", "confirmation": ["Declare war on Egypt?"]}
                ]
            await gateway.call(chat, "trade_read", {"proposal": "d1"}, "new-read")
            # An unrelated ledger refresh must not select another job's newer terms.
            await gateway.call(old, "chat_read", {}, "greeting")
            stale = await gateway.call(
                old,
                "trade_decide",
                {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test objective"}},
                "accept",
            )
            assert stale == {"error": "STALE_REVISION"}
            assert host.writes[-1]["version"] == 1
            assert host.writes[-1].keys() == {"op", "epoch", "requestId", "proposal", "version", "decision"}
            accepted = await gateway.call(
                chat,
                "trade_decide",
                {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test objective"}},
                "accept",
            )
            assert accepted["outcome"]["confirmation"]["source"] == "engine"
            assert host.writes[-1]["version"] == 2
            assert old.trades["d1"]["clauses"] == shown["trade"]["clauses"]
            assert (
                await gateway.call(
                    chat,
                    "trade_decide",
                    {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test objective"}},
                    "accept",
                )
                == accepted
            )
            assert len(host.writes) == 2
            assert old.trades["d1"]["clauses"][0]["item"]["amount"] == 20
            await gateway.call(
                old,
                "memory_update",
                {
                    "changes": [
                        {
                            "op": "create",
                            "content": {
                                "type": "belief",
                                "claim": "Greece originally offered 20 gold",
                                "uncertainty": "low",
                                "sources": ["observation:trade/d1"],
                                "subjects": ["Greece"],
                            },
                        }
                    ]
                },
                "remember",
            )
            note = next(r["body"] for r in memory.snapshot("gameplay")["records"].values() if r["category"] == "belief")
            assert note["_evidence"][0]["version"] == 1
        finally:
            await io.close()

    asyncio.run(run())


def test_trade_tools_keep_decisions_small_and_do_not_grant_gameplay(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = TradeHost()
        try:
            job = await gateway.open("chat", "communication", "g1", host, {})
            tools = {tool["name"]: tool for tool in await gateway.tools(job)}
            assert "player_command" not in tools
            assert tools["friendship_offer"]["inputSchema"]["required"] == ["intent", "partner"]
            assert set(tools["friendship_offer"]["inputSchema"]["properties"]) == {"partner", "intent"}
            assert set(tools["trade_offer"]["inputSchema"]["$defs"]["TradeItem"]["properties"]["type"]["enum"]) == {
                "Gold",
                "Gold_Per_Turn",
                "Luxury_Resource",
                "Strategic_Resource",
                "Treaty",
                "Agreement",
                "Embassy",
                "City",
                "WarDeclaration",
                "PeaceProposal",
            }
            assert tools["trade_decide"]["inputSchema"]["required"] == ["intent", "proposal", "decision"]
            assert set(tools["trade_decide"]["inputSchema"]["properties"]) == {"proposal", "decision", "intent"}
            await gateway.call(job, "trade_read", {"proposal": "d1"}, "read")
            with pytest.raises(ValueError):
                await gateway.call(
                    job,
                    "trade_decide",
                    {
                        "proposal": "d1",
                        "decision": "accept",
                        "version": 2,
                        "intent": {"reason": "Advance the test objective"},
                    },
                    "version",
                )
            with pytest.raises(ValueError):
                await gateway.call(
                    job,
                    "trade_decide",
                    {
                        "proposal": "d1",
                        "decision": "accept",
                        "clauses": [],
                        "intent": {"reason": "Advance the test objective"},
                    },
                    "terms",
                )
            with pytest.raises(StateConflict, match="GAMEPLAY_NOT_PERMITTED"):
                await gateway.call(job, "player_command", {"kind": "END_TURN"}, "move")
            offer = {
                "proposal": "d1",
                "partner": "Greece",
                "give": [],
                "receive": [host.trade["clauses"][0]["item"]],
                "intent": {"reason": "Advance the test objective"},
            }
            await gateway.call(job, "trade_offer", offer, "counter")
            assert host.writes[-1]["version"] == 1
            assert host.writes[-1]["partner"] == "Greece"
        finally:
            await io.close()

    asyncio.run(run())


def test_friendship_proposal_uses_the_same_small_decision_and_durable_request_path(tmp_path):
    class FriendshipHost(TradeHost):
        async def tool(self, name, args):
            if name == "player_communication" and args["op"] == "friendship_offer":
                self.writes.append(copy.deepcopy(args))
                return {
                    "trade": {
                        "proposal": "d2",
                        "version": 1,
                        "author": "Rome",
                        "partner": "Greece",
                        "clauses": [],
                        "friendship": {"name": "Declaration of Friendship", "duration": 30},
                        "status": "PENDING",
                    }
                }
            return await super().tool(name, args)

    async def run():
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "private", "Rome"), io, tmp_path / "workspace")
        host = FriendshipHost()
        try:
            job = await gateway.open("chat", "communication", "g1", host, {})
            offered = await gateway.call(
                job, "friendship_offer", {"partner": "Greece", "intent": {"reason": "Build friendship"}}, "offer"
            )
            assert offered["trade"]["friendship"] == {"name": "Declaration of Friendship", "duration": 30}
            assert set(host.writes[-1]) == {"op", "epoch", "requestId", "partner"}
            assert (
                await gateway.call(
                    job, "friendship_offer", {"partner": "Greece", "intent": {"reason": "Build friendship"}}, "offer"
                )
                == offered
            )
            assert len(host.writes) == 1
            with pytest.raises(ValueError):
                await gateway.call(
                    job,
                    "friendship_offer",
                    {"partner": "Greece", "duration": 999, "intent": {"reason": "Invalid test"}},
                    "forged",
                )
            host.trade = {
                "proposal": "d1",
                "version": 1,
                "author": "Greece",
                "partner": "Rome",
                "clauses": [],
                "friendship": {"name": "Declaration of Friendship", "duration": 30},
                "status": "PENDING",
            }
            await gateway.call(job, "trade_read", {"proposal": "d1"}, "inspect")
            result = await gateway.call(
                job,
                "trade_decide",
                {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test objective"}},
                "accept",
            )
            assert result["outcome"]
            assert set(host.writes[-1]) == {"op", "epoch", "requestId", "proposal", "version", "decision"}
            assert job.trades["d1"]["friendship"]["duration"] == 30
        finally:
            await io.close()

    asyncio.run(run())


def test_new_economic_policy_invalidates_an_outstanding_trade_decision(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = TradeHost()
        try:
            chat = await gateway.open("chat", "communication", "g1", host, {})
            planner = await gateway.open("planner", "gameplay", "g1", host, {})
            await gateway.call(chat, "trade_read", {"proposal": "d1"}, "read")
            await gateway.call(
                planner,
                "memory_update",
                {
                    "changes": [
                        {
                            "op": "create",
                            "content": {
                                "type": "plan",
                                "objective": "Reserve resources for construction",
                                "approach": "Carry out the stated objective",
                                "successCriteria": "Objective achieved",
                                "subjects": ["economy"],
                            },
                        }
                    ]
                },
                "policy",
            )
            stale = await gateway.call(
                chat, "trade_decide", {"proposal": "d1", "decision": "accept", "intent": {"plan": "mem1"}}, "accept"
            )
            assert stale["error"] == "STALE_ASSUMPTIONS" and stale["recoverable"] and not stale["applied"]
            assert stale["changedRecords"] == ["mem1"]
            assert stale["recovery"]["reads"] == [
                {"tool": "memory_read", "arguments": {"ids": ["mem1"], "includeRetired": True}},
                {"tool": "trade_read", "arguments": {"proposal": "d1"}},
            ]
            assert not host.writes
            await gateway.call(chat, "memory_read", {"ids": ["mem1"], "includeRetired": True}, "reconsider")
            assert "outcome" in await gateway.call(
                chat,
                "trade_decide",
                {"proposal": "d1", "decision": "accept", "intent": {"plan": "mem1"}},
                "fresh-decision",
            )
        finally:
            await io.close()

    asyncio.run(run())


@pytest.mark.parametrize("enacted", [False, True])
def test_trade_receipt_reconciliation_after_response_loss(tmp_path, enacted):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = TradeHost()
        original = host.tool

        async def unreliable(name, args):
            if name == "player_communication" and args["op"] == "trade_decide":
                if enacted:
                    await original(name, args)
                raise TimeoutError("Lost trade response")
            return await original(name, args)

        host.tool = unreliable
        try:
            job = await gateway.open("chat", "communication", "g1", host, {})
            await gateway.call(job, "trade_read", {"proposal": "d1"}, "read")
            with pytest.raises(TimeoutError):
                await gateway.call(
                    job,
                    "trade_decide",
                    {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test objective"}},
                    "accept",
                )
            assert job.closed and len(memory.pending()) == 1
            restarted = CivilizationState(tmp_path / "private", "Rome")
            runtime = ChatRuntime(SimpleNamespace(io=io, memory=restarted))
            runtime.connection, runtime.epoch = host, "g1"
            await runtime.reconcile()
            assert not restarted.pending()
            assert len(host.writes) == int(enacted)
            receipt = json.loads(restarted.requests_for_job("chat")[0]["result"])
            assert ("outcome" in receipt) == enacted
        finally:
            await io.close()

    asyncio.run(run())


def test_duplicate_offer_rejection_with_null_trade_is_durable_and_does_not_break_settlement(tmp_path):
    class BusyHost(HoldingHost):
        async def tool(self, name, args):
            if name == "player_communication" and args["op"] == "trade_offer" and self.active:
                return {
                    "error": "TRADE_UNAVAILABLE",
                    "reason": "A negotiation is already holding gameplay.",
                    "trade": None,
                }
            return await super().tool(name, args)

    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = BusyHost()
        try:
            chat = await gateway.open("chat", "communication", "g1", host, {})
            game = await gateway.open("game", "gameplay", "g1", host, {}, game_turn=0)
            args = {
                "partner": "Greece",
                "give": [{"type": "Gold", "name": "Gold", "amount": 30, "duration": -1}],
                "receive": [],
                "intent": {"reason": "Make the opening offer"},
            }
            assert (await gateway.call(chat, "trade_offer", args, "first"))["trade"]["status"] == "PENDING"
            rejected = await gateway.call(game, "trade_offer", args, "duplicate")
            assert rejected["error"] == "TRADE_UNAVAILABLE" and rejected["trade"] is None
            assert not memory.pending()
            assert await gateway.call(game, "trade_offer", args, "duplicate") == rejected
            assert len(host.writes) == 1
            # A later terminal read walks earlier receipts, including the null rejection.
            host.trade.update(status="ENACTED", outcome={"action": "ENACT_TRADE"})
            host.active = False
            await gateway.call(chat, "trade_read", {"proposal": "d1"}, "settled")
            summary = memory.summary(job="game")
            assert summary["actions"][0]["outcome"] == "rejected"
            assert summary["actions"][0].get("settlement") is None
        finally:
            await io.close()

    asyncio.run(run())
