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


class PendingTradesHost(Host):
    """The turn-147 failure: a native offer blocks handoff until answered."""

    def __init__(self):
        super().__init__()
        self.writes = []
        self.reads = []
        self.trades = [
            {
                "proposal": "dabc12",
                "version": 1,
                "author": "Egypt",
                "partner": "Rome",
                "status": "PENDING",
                "mode": "NATIVE_AI",
                "acceptAvailable": True,
                "declineAvailable": True,
                "withdrawAvailable": False,
                "clauses": [
                    {
                        "from": a,
                        "to": b,
                        "item": {"type": "Treaty", "name": "Defensive Pact", "amount": 1, "duration": 25},
                    }
                    for a, b in [("Egypt", "Rome"), ("Rome", "Egypt")]
                ],
            },
            {
                "proposal": "ddef34",
                "version": 1,
                "author": "Greece",
                "partner": "Rome",
                "status": "PENDING",
                "mode": "SHARED",
            },
        ]

    def pending(self):
        return [
            {k: trade[k] for k in ("proposal", "author", "partner", "status", "mode")}
            for trade in self.trades
            if trade["status"] == "PENDING"
        ]

    async def tool(self, name, args):
        if name == "player_communication":
            if args["op"] == "state":
                return {**await super().tool(name, args), "trades": self.pending()}
            if args["op"] == "trade_read":
                self.reads.append(args["proposal"])
                trade = next((t for t in self.trades if t["proposal"] == args["proposal"]), None)
                if trade is None:
                    return {
                        "error": "UNKNOWN_TRADE",
                        "reason": "No trade with this reference is available to you.",
                        "proposal": args["proposal"],
                        "applied": False,
                        "recoverable": True,
                        "pendingTrades": self.pending(),
                    }
                return {"trade": copy.deepcopy(trade), "history": []}
            if args["op"] == "trade_decide":
                self.writes.append(copy.deepcopy(args))
                trade = next(t for t in self.trades if t["proposal"] == args["proposal"])
                assert args["version"] == trade["version"]
                trade.update(status="DECLINED", acceptAvailable=False, declineAvailable=False)
                return {"trade": copy.deepcopy(trade)}
        return await super().tool(name, args)


@pytest.mark.parametrize("tool", ["trade_read", "trade_decide", "trade_offer"])
def test_mistyped_trade_recovers_via_copyable_read_and_explicit_decision(tmp_path, tool):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = PendingTradesHost()
        try:
            game = await gateway.open("game", "gameplay", "g1", host, {})
            await gateway.call(game, "player_turn", {}, "turn")
            chat = await gateway.call(game, "chat_read", {}, "chat")
            assert chat["trades"][0]["inspection"] == {
                "tool": "trade_read",
                "arguments": {"proposal": "dabc12"},
            }
            assert "responses" not in chat["trades"][0]
            arguments: JsonObject = {"proposal": "dabc122"}  # One extra character, as in the stopped game.
            if tool != "trade_read":
                arguments["intent"] = {"reason": "Consider the offered pact"}
            if tool == "trade_decide":
                arguments["decision"] = "decline"
            elif tool == "trade_offer":
                arguments.update(
                    partner="Egypt",
                    give=[{"type": "Gold", "name": "Gold", "amount": 1, "duration": -1}],
                    receive=[],
                )
            rejected = await gateway.call(game, tool, arguments, "typo")
            assert rejected["error"] == "UNKNOWN_TRADE" and rejected["applied"] is False
            assert rejected["recoverable"] is True
            pending = rejected["pendingTrades"][0]
            assert pending["proposal"] == "dabc12" and pending["author"] == "Egypt"
            assert "mode" not in pending and "responses" not in pending
            assert not host.writes and not memory.pending() and not game.trades
            assert await gateway.call(game, tool, arguments, "typo") == rejected
            assert host.reads == ["dabc122"]

            # Seeing the pending list does not count as reading exact terms.
            choice = {"proposal": pending["proposal"], "decision": "decline", "intent": {"reason": "Avoid a pact"}}
            unread = await gateway.call(game, "trade_decide", choice, "unread")
            assert unread["error"] == "PROPOSAL_NOT_OBSERVED" and unread["applied"] is False
            assert unread["recovery"]["reads"] == [pending["inspection"]]
            assert not game.trades and not host.writes
            read = pending["inspection"]
            inspected = await gateway.call(game, read["tool"], read["arguments"], "inspect")
            assert inspected["trade"]["clauses"] == host.trades[0]["clauses"]
            response = next(r for r in inspected["trade"]["responses"] if r["arguments"]["decision"] == "decline")
            assert "intent" not in response["arguments"]
            assert await gateway.call(game, "trade_decide", choice, "unread") == unread
            assert not host.writes  # A replay of a rejected decision never acts after inspection.
            result = await gateway.call(
                game, response["tool"], {**response["arguments"], "intent": choice["intent"]}, "decide"
            )
            assert result["trade"]["status"] == "DECLINED" and "responses" not in result["trade"]
            assert len(host.writes) == 1 and not memory.pending() and not gateway.failed.is_set()
        finally:
            await io.close()

    asyncio.run(run())


def test_unknown_trade_recovery_keeps_native_requests_out_of_communication_jobs(tmp_path):
    async def run():
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "private", "Rome"), io, tmp_path / "workspace")
        host = PendingTradesHost()
        try:
            job = await gateway.open("chat", "communication", "g1", host, {})
            unknown = await gateway.call(job, "trade_read", {"proposal": "54"}, "event-id")
            assert [t["proposal"] for t in unknown["pendingTrades"]] == ["ddef34"]
            assert "Egypt" not in json.dumps(unknown)
            assert unknown["pendingTrades"][0]["inspection"]["arguments"] == {"proposal": "ddef34"}
            native = await gateway.call(job, "trade_read", {"proposal": "dabc12"}, "native")
            assert "responses" not in native["trade"]
            assert native["trade"]["acceptAvailable"] is False and native["trade"]["declineAvailable"] is False
            assert not host.writes
        finally:
            await io.close()

    asyncio.run(run())


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
            "mode": "SHARED",
            "deadlineMillis": 999999,
            "counterofferLimit": 3,
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


class ResearchAgreementHost(Host):
    def __init__(self):
        super().__init__()
        self.view.update(
            turn=113,
            economy={"treasury": 654},
            diplomacy={"knownCivilizations": [{"civilizationId": "Greece", "researchAgreementTurns": 0}]},
        )
        self.trade: JsonObject = {
            "proposal": "d1",
            "version": 1,
            "author": "Greece",
            "partner": "Rome",
            "status": "PENDING",
            "clauses": [],
            "mode": "SHARED",
        }
        self.acceptances = 0
        self.payments = 0

    async def tool(self, name, args):
        if name == "player_communication":
            if args["op"] == "trade_read":
                return {"trade": copy.deepcopy(self.trade)}
            if args["op"] == "trade_decide":
                assert args["decision"] == "accept" and args["version"] == self.trade["version"]
                self.acceptances += 1
                if self.trade["status"] == "PENDING":
                    self.payments += 1
                    self.view["economy"]["treasury"] -= 167
                    self.view["diplomacy"]["knownCivilizations"][0]["researchAgreementTurns"] = 25
                    self.revision = "accepted"
                    self.trade.update(status="ENACTED", outcome={"action": "ENACT_TRADE"})
                return {"trade": copy.deepcopy(self.trade), "outcome": self.trade["outcome"]}
        return await super().tool(name, args)


@pytest.mark.parametrize("retrying_purpose", ["gameplay", "communication"])
def test_concurrent_acceptance_of_enacted_terms_does_not_stop_the_run(tmp_path, retrying_purpose):
    """Shared trades can still race: another job accepts the offer after a refresh."""

    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = ResearchAgreementHost()
        # The turn-113/114 trade race occurs after this civilization's turn-110 review.
        with memory.transaction() as db:
            memory._set(db, "lastStrategicReviewTurn", 110)
        arguments = {"proposal": "d1", "decision": "accept", "intent": {"reason": "Fund joint research"}}
        try:
            winner_purpose = "communication" if retrying_purpose == "gameplay" else "gameplay"
            winner = await gateway.open("winner", winner_purpose, "g1", host, {})
            await gateway.call(winner, "player_turn", {}, "previous-turn")
            retrying = await gateway.open("retrying", retrying_purpose, "g1", host, {})
            host.view["turn"] = 114
            host.revision = "turn114"
            await gateway.call(winner, "player_turn", {}, "current-turn")
            await gateway.call(retrying, "trade_read", {"proposal": "d1"}, "terms")
            stale = await gateway.call(retrying, "trade_decide", arguments, "first")
            assert stale["error"] == "STALE_ASSUMPTIONS"
            for index, read in enumerate(stale["recovery"]["reads"]):
                await gateway.call(retrying, read["tool"], read["arguments"], f"refresh:{index}")
            await gateway.call(winner, "trade_read", {"proposal": "d1"}, "terms")
            enacted = await gateway.call(winner, "trade_decide", arguments, "accepted")
            assert enacted["trade"]["status"] == "ENACTED"
            if winner_purpose == "communication":
                await gateway.call(winner, "player_turn", {}, "after-accept")

            result = await gateway.call(retrying, "trade_decide", arguments, "retry")
            assert result["trade"]["status"] == "ENACTED"
            assert result["outcome"] == enacted["outcome"]
            assert not gateway.failed.is_set() and memory.run_failure() is None
            assert host.payments == 1 and host.view["economy"]["treasury"] == 487
            assert host.acceptances == 2 and not memory.pending()
            assert await gateway.call(retrying, "trade_decide", arguments, "retry") == result
            assert host.acceptances == 2
        finally:
            await io.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,version,decision",
    [("PENDING", 1, "accept"), ("DECLINED", 1, "accept"), ("ENACTED", 2, "accept"), ("ENACTED", 1, "decline")],
)
def test_stale_trade_decisions_still_require_recovery(tmp_path, status, version, decision):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = ResearchAgreementHost()
        try:
            game = await gateway.open("game", "gameplay", "g1", host, {})
            await gateway.call(game, "player_turn", {}, "previous-turn")
            chat = await gateway.open("chat", "communication", "g1", host, {})
            await gateway.call(chat, "trade_read", {"proposal": "d1"}, "terms")
            host.trade.update(status=status, version=version)
            host.view["turn"] = 114
            host.revision = "turn114"
            await gateway.call(game, "player_turn", {}, "current-turn")
            arguments = {"proposal": "d1", "decision": decision, "intent": {"reason": "Consider research"}}
            first = await gateway.call(chat, "trade_decide", arguments, "first")
            assert first["error"] == "STALE_ASSUMPTIONS" and first["applied"] is False
            second = await gateway.call(chat, "trade_decide", arguments, "second")
            assert second["error"] == "REPEATED_STALE_ASSUMPTIONS" and second["recoverable"] is False
            assert host.acceptances == 0 and not memory.pending()
        finally:
            await io.close()

    asyncio.run(run())


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
                {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test assessment"}},
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
                "intent": {"reason": "Advance the test assessment"},
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
                {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test assessment"}},
                "accept",
            )
            result = await gateway.call(game, "trade_offer", arguments, "offer")
            assert result["trade"]["status"] == "ENACTED" and result["trade"]["version"] == 2
            assert result["outcome"]["action"] == "ENACT_TRADE"
            assert not {"mode", "deadlineMillis", "counterofferLimit"} & result["trade"].keys()
            assert await gateway.call(game, "trade_offer", arguments, "offer") == result
            assert [request["op"] for request in host.writes] == ["trade_offer", "trade_decide"]
        finally:
            await io.close()

    asyncio.run(run())


def test_native_offer_routing_survives_projection_and_interrupted_receipt_replay(tmp_path):
    class NativeHost(HoldingHost):
        async def tool(self, name, args):
            if name == "player_communication" and args["op"] == "trade_offer":
                self.writes.append(copy.deepcopy(args))
                return {"trade": {**self.trade, "mode": "NATIVE_AI", "partner": "Egypt", "nativeRequestId": "native1"}}
            if name == "player_communication" and args["op"] == "negotiation":
                raise AssertionError("A native AI offer must not enter the shared negotiation wait")
            return await super().tool(name, args)

    async def run():
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "private", "Rome"), io, tmp_path / "workspace")
        host = NativeHost()
        arguments = {
            "partner": "Egypt",
            "give": [{"type": "Gold", "name": "Gold", "amount": 1, "duration": -1}],
            "receive": [],
            "intent": {"reason": "Offer a goodwill gift"},
        }
        try:
            game = await gateway.open("game", "gameplay", "g1", host, {})
            original = gateway.update_after_action

            async def interrupted(job, name, arguments, call_id, result) -> JsonObject:
                raise asyncio.CancelledError()

            gateway.update_after_action = interrupted
            with pytest.raises(asyncio.CancelledError):
                await gateway.call(game, "trade_offer", arguments, "offer")
            gateway.update_after_action = original
            result = await gateway.call(game, "trade_offer", arguments, "offer")
            assert result["trade"]["status"] == "PENDING"
            assert result["trade"]["partner"] == "Egypt"
            assert not {"mode", "nativeRequestId", "deadlineMillis", "counterofferLimit"} & result["trade"].keys()
            assert "instruction" not in result
            assert await gateway.call(game, "trade_offer", arguments, "offer") == result
            assert len(host.writes) == 1
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
            "mode": "SHARED",
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
            unread = await gateway.call(
                old,
                "trade_decide",
                {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test assessment"}},
                "unseen",
            )
            assert unread["error"] == "PROPOSAL_NOT_OBSERVED" and unread["applied"] is False
            assert unread["recovery"]["reads"] == [{"tool": "trade_read", "arguments": {"proposal": "d1"}}]
            assert not old.trades and not host.writes
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
                {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test assessment"}},
                "accept",
            )
            assert stale == {"error": "STALE_REVISION"}
            assert host.writes[-1]["version"] == 1
            assert host.writes[-1].keys() == {"op", "epoch", "requestId", "proposal", "version", "decision"}
            accepted = await gateway.call(
                chat,
                "trade_decide",
                {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test assessment"}},
                "accept",
            )
            assert accepted["outcome"]["confirmation"]["source"] == "engine"
            assert host.writes[-1]["version"] == 2
            assert old.trades["d1"]["clauses"] == shown["trade"]["clauses"]
            assert (
                await gateway.call(
                    chat,
                    "trade_decide",
                    {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test assessment"}},
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
                        "intent": {"reason": "Advance the test assessment"},
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
                        "intent": {"reason": "Advance the test assessment"},
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
                "intent": {"reason": "Advance the test assessment"},
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
                        "mode": "SHARED",
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
                "mode": "SHARED",
            }
            await gateway.call(job, "trade_read", {"proposal": "d1"}, "inspect")
            result = await gateway.call(
                job,
                "trade_decide",
                {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test assessment"}},
                "accept",
            )
            assert result["outcome"]
            assert set(host.writes[-1]) == {"op", "epoch", "requestId", "proposal", "version", "decision"}
            assert "d1" not in job.trades
            assert {"tool": "trade_read", "arguments": {"proposal": "d1"}} in result["invalidatedDetails"]
        finally:
            await io.close()

    asyncio.run(run())


def test_private_policy_changes_do_not_gate_an_inspected_trade_decision(tmp_path):
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
                                "type": "strategy",
                                "assessment": "Reserve resources for construction",
                                "reasons": ["Carry out the stated assessment"],
                                "reconsiderWhen": ["Objective achieved"],
                                "subjects": ["economy"],
                            },
                        }
                    ]
                },
                "policy",
            )
            stale = await gateway.call(
                chat,
                "trade_decide",
                {"proposal": "d1", "decision": "accept", "intent": {"strategy": "mem1", "reason": "Defend Rome"}},
                "accept",
            )
            assert "outcome" in stale
            assert host.writes
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
                    {"proposal": "d1", "decision": "accept", "intent": {"reason": "Advance the test assessment"}},
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
