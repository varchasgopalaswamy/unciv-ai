import asyncio
import copy

import pytest

from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO


class Host:
    def __init__(self):
        self.civilization = "Rome"
        self.revision = "rev1"
        self.peace = 0
        self.executed = []
        self.seen = {}

    async def close(self):
        pass

    async def list_tools(self):
        return [
            {
                "name": "player_command",
                "inputSchema": {
                    "properties": {
                        key: {"type": "string"} for key in ("kind", "target", "requestId", "epoch", "revision")
                    },
                    "required": ["kind", "requestId", "epoch", "revision"],
                },
            }
        ]

    async def tool(self, name, args):
        if name == "player_turn":
            return {"epoch": "g1", "revision": self.revision, "phase": "ACTIVE", "turnReview": {"id": "review1"}}
        if name == "player_communication":
            if args["op"] == "negotiation":
                return {"negotiation": {"active": False}}
            if args["op"] == "state":
                return {
                    "civilization": self.civilization,
                    "epoch": "g1",
                    "peaceVersion": self.peace,
                    "offers": [],
                    "consents": [],
                    "outcomes": [],
                }
            return {"sent": True}
        if args["requestId"] in self.seen:
            return self.seen[args["requestId"]]
        if args.get("socialVersion", self.peace) != self.peace:
            return {"error": "STALE_COMMITMENT"}
        self.executed.append(copy.deepcopy(args))
        self.revision = "rev" + str(len(self.executed) + 1)
        result = {"requestId": args["requestId"], "revision": self.revision, "kind": args["kind"], "done": True}
        self.seen[args["requestId"]] = result
        return result


def test_gateway_owns_bookkeeping_and_retries_exact_call(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        job = await gateway.open("planning", "gameplay", "g1", host, {})
        tool = (await gateway.tools(job))[0]
        assert tool["inputSchema"]["required"] == ["kind"]
        assert set(tool["inputSchema"]["properties"]) == {"kind", "target"}
        view = await gateway.call(job, "player_turn", {}, "1")
        assert "revision" not in view and "epoch" not in view
        action = {"kind": "MOVE_UNIT", "target": "unit1"}
        result = await gateway.call(job, "player_command", action, "2")
        assert await gateway.call(job, "player_command", action, "2") == result
        assert len(host.executed) == 1 and host.executed[0]["revision"] == "rev1"
        assert "requestId" not in result
        with pytest.raises(ValueError):
            await gateway.call(job, "player_command", {**action, "requestId": "mine"}, "3")
        await gateway.close(job, True)
        with pytest.raises(StateConflict):
            await gateway.call(job, "player_turn", {}, "4")
        await io.close()

    asyncio.run(run())


def test_commitment_change_rejects_attack_but_greeting_preserves_economic_job(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        job = await gateway.open("planning", "gameplay", "g1", host, {})
        await gateway.call(job, "player_turn", {}, "1")
        host.peace += 1
        rejected = await gateway.call(job, "player_command", {"kind": "ATTACK", "target": "unit1"}, "2")
        assert "error" in rejected and not host.executed
        memory.ingest("g1", [{"id": 1, "kind": "message", "body": {"text": "Hello"}}], 1)
        result = await gateway.call(
            job, "player_command", {"kind": "QUEUE_CONSTRUCTION", "target": "city1", "selection": "Market"}, "3"
        )
        assert result["done"] and len(host.executed) == 1
        communication = await gateway.open("chat", "communication", "g1", host, {})
        with pytest.raises(StateConflict, match="GAMEPLAY_NOT_PERMITTED"):
            await gateway.call(communication, "player_command", {"kind": "END_TURN"}, "4")
        await gateway.close(job, True)
        await gateway.close(communication, True)
        await io.close()

    asyncio.run(run())


def test_slow_game_dispatch_does_not_block_mailbox_or_memory(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        game = await gateway.open("planning", "gameplay", "g1", host, {})
        chat = await gateway.open("chat", "communication", "g1", host, {})
        await gateway.call(game, "player_turn", {}, "1")
        began, release = asyncio.Event(), asyncio.Event()
        original = host.tool

        async def slow(name, args):
            if name == "player_command":
                began.set()
                await release.wait()
            return await original(name, args)

        host.tool = slow
        task = asyncio.create_task(gateway.call(game, "player_command", {"kind": "MOVE_UNIT", "target": "unit1"}, "2"))
        await began.wait()
        await asyncio.wait_for(
            io.call(memory.ingest, "g1", [{"id": 1, "kind": "message", "wake": True, "body": {"text": "Hello"}}], 1), 1
        )
        await gateway.call(chat, "chat_read", {}, "messages")
        result = await asyncio.wait_for(
            gateway.call(
                chat,
                "memory_update",
                {
                    "changes": [
                        {
                            "key": "relationships/Greece",
                            "category": "belief",
                            "body": {
                                "text": "Friendly greeting; motives uncertain",
                                "sources": ["event:1"],
                                "uncertainty": "high",
                            },
                        }
                    ]
                },
                "3",
            ),
            1,
        )
        assert result["changes"][0]["accepted"] and not task.done()
        release.set()
        await task
        await gateway.close(game, True)
        await gateway.close(chat, True)
        await io.close()

    asyncio.run(run())


def test_wrong_player_credential_cannot_supply_evidence_or_change_memory(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        host.civilization = "Greece"
        job = await gateway.open("misrouted", "gameplay", "g1", host, {})
        try:
            for name, arguments in (
                ("player_turn", {}),
                (
                    "memory_update",
                    {"changes": [{"key": "economy", "category": "plan", "body": {"text": "Spend Greek gold"}}]},
                ),
            ):
                with pytest.raises(StateConflict, match="PLAYER_CREDENTIAL_MISMATCH"):
                    await gateway.call(job, name, arguments, name)
            assert not memory.snapshot("gameplay")["records"]
            assert not host.executed
        finally:
            await gateway.close(job, False)
            await io.close()

    asyncio.run(run())


def test_game_and_chat_observations_preserve_the_full_ledger_shown_to_the_model(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        original = host.tool
        ledger = {"offers": [{"proposal": "p1", "terms": "No attacks before turn 8"}], "consents": []}

        async def observed_host(name, args):
            value = await original(name, args)
            return (
                {**value, **copy.deepcopy(ledger)}
                if name == "player_communication" and args["op"] == "state"
                else value
            )

        host.tool = observed_host
        job = await gateway.open("planning", "gameplay", "g1", host, {})
        try:
            shown = await gateway.call(job, "player_turn", {}, "read")
            stored = memory.snapshot("gameplay")["records"]["observation:game"]["body"]
            assert stored["communication"]["offers"] == shown["communication"]["offers"] == ledger["offers"]
            ledger["consents"].append({"proposal": "p1", "author": "Greece", "decision": "accept"})
            await gateway.call(job, "chat_read", {}, "new-consent")
            await gateway.call(
                job,
                "memory_update",
                {
                    "changes": [
                        {
                            "key": "relationships/Greece",
                            "category": "belief",
                            "body": {
                                "text": "Greece accepted the pact; future conduct is uncertain",
                                "uncertainty": "medium",
                                "sources": ["observation:game", "observation:communication"],
                            },
                        }
                    ]
                },
                "remember",
            )
            records = memory.snapshot("gameplay")["records"]
            assert records["observation:game"]["body"]["communication"]["consents"] == []
            assert records["observation:communication"]["body"]["consents"] == ledger["consents"]
            assert [r["key"] for r in records["relationships/Greece"]["body"]["_evidence"]] == [
                "observation:game",
                "observation:communication",
            ]
        finally:
            await gateway.close(job, True)
            await io.close()

    asyncio.run(run())
