import asyncio
import copy

import pytest
from jsonschema import Draft202012Validator

from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from tests.memory_helpers import stored_records


class Host:
    def __init__(self):
        self.civilization = "Rome"
        self.revision = "rev1"
        self.offers = []
        self.consents = []
        self.executed = []
        self.seen = {}
        self.view: JsonObject = {
            "turn": 0,
            "canEndTurn": True,
            "constructionCatalogue": ["Granary", "Market", "Warrior"],
            "cities": [
                {
                    "handle": "city1",
                    "name": "Rome",
                    "constructionQueue": [],
                    "puppet": False,
                    "attacksRemaining": 1,
                    "constructionOptions": ["Market", "Granary"],
                }
            ],
            "map": {
                "units": [
                    {
                        "handle": "unit1",
                        "name": "Warrior",
                        "position": {"x": 0, "y": 0},
                        "health": 100,
                        "movement": "2",
                        "military": True,
                        "idle": True,
                        "movementSupported": True,
                        "canFoundCity": False,
                        "moves": [],
                        "combat": {"attacksRemaining": 1, "attacks": []},
                    }
                ]
            },
        }

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
            return {
                **copy.deepcopy(self.view),
                "epoch": "g1",
                "revision": self.revision,
                "phase": "ACTIVE",
                "turnReview": {"id": "review1"},
            }
        if name == "player_communication":
            if args["op"] == "negotiation":
                return {"negotiation": {"active": False}}
            if args["op"] == "trade_options":
                return {"partner": args["partner"], "mode": "SHARED", "give": [], "receive": []}
            if args["op"] == "state":
                return {
                    "civilization": self.civilization,
                    "epoch": "g1",
                    "offers": copy.deepcopy(self.offers),
                    "consents": copy.deepcopy(self.consents),
                    "outcomes": [],
                    "channels": ["Rome", "Greece", "France"],
                    "context": {"turn": self.view["turn"]},
                }
            return {"sent": True}
        if args["requestId"] in self.seen:
            return self.seen[args["requestId"]]
        self.executed.append(copy.deepcopy(args))
        self.revision = "rev" + str(len(self.executed) + 1)
        summary = {
            "END_TURN": "Ended turn.",
            "DISMISS_NOTIFICATIONS": f"Dismissed {len(args.get('targets', []))} ordinary notifications.",
        }.get(args["kind"], f"Executed {args['kind']} in test host.")
        result = {
            "requestId": args["requestId"],
            "revision": self.revision,
            "kind": args["kind"],
            "done": True,
            "actionSummary": summary,
        }
        self.seen[args["requestId"]] = result
        return result


def test_unrecorded_intent_is_explained_without_blocking_or_changing_native_receipt(tmp_path):
    async def run():
        memory = CivilizationState(tmp_path / "private", "Rome")
        io = ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        try:
            job = await gateway.open("game", "gameplay", "g1", host, {}, 0)
            await gateway.call(job, "player_turn", {}, "read")
            arguments = {
                "kind": "END_TURN",
                "intent": {"reason": "Finish chosen actions", "strategy": "invented-plan", "decision": "defense"},
            }
            result = await gateway.call(job, "player_command", arguments, "end")
            assert result["done"] and len(host.executed) == 1
            assert result["intentFeedback"]["unverifiedStrategy"] == "invented-plan"
            assert not result["intentFeedback"]["linked"]
            assert "intent" not in host.executed[0]
            receipt = memory.call_receipt("game", "end")
            assert receipt is not None and "intentFeedback" not in receipt
            assert await gateway.call(job, "player_command", arguments, "end") == result
            assert len(host.executed) == 1
        finally:
            await io.close()
            memory.engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("decision", [None, "acted", "deferred", "rejected"])
def test_end_turn_initiative_decision_is_optional_private_audit_metadata(tmp_path, decision):
    async def run():
        memory = CivilizationState(tmp_path / "private", "Rome")
        io = ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        try:
            from civ_agent.review_store import ReviewStore
            from tests.test_strategic_review import assessment

            memory.bind("g1")
            store = ReviewStore(memory)
            store.prepare("review", {"turn": 0, "observation": {"turn": 1}}, {}, 60)
            store.complete("review", assessment())
            host.view["turn"] = 1
            job = await gateway.open("game", "gameplay", "g1", host, {}, 1)
            await gateway.call(job, "player_turn", {}, "read")
            entry = {"initiative": 1, "decision": decision, "reason": "Wait for reconnaissance."}
            command = {"kind": "END_TURN", **({"initiativeDecisions": [entry]} if decision else {})}
            result = await gateway.call(job, "player_command", command, "end")
            assert result["done"] and len(host.executed) == 1
            assert "intent" not in host.executed[0] and "initiativeDecisions" not in host.executed[0]
            action = memory.summary(1)["actions"][0]
            assert action.get("initiativeDecisions", []) == ([{**entry, "reviewTurn": 0}] if decision else [])
        finally:
            await io.close()
            memory.engine.dispose()

    asyncio.run(run())


def test_gateway_owns_bookkeeping_and_retries_exact_call(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        job = await gateway.open("planning", "gameplay", "g1", host, {})
        tool = (await gateway.tools(job))[0]
        validator = Draft202012Validator(tool["inputSchema"])
        assert validator.is_valid({"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}})
        invalid = await gateway.call(job, "player_command", {"kind": "MOVE_UNIT", "target": "unit1"}, "invalid")
        assert invalid["error"] == "INVALID_COMMAND_ARGUMENTS" and invalid["applied"] is False
        assert "destination" in invalid["missingFields"] and host.executed == []
        view = await gateway.call(job, "player_turn", {}, "1")
        assert "revision" not in view and "epoch" not in view
        await gateway.call(job, "unit_details", {"target": "unit1"}, "inspect")
        action = {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}}
        result = await gateway.call(job, "player_command", action, "2")
        assert await gateway.call(job, "player_command", action, "2") == result
        assert len(host.executed) == 1 and host.executed[0]["revision"] == "rev1"
        assert "requestId" not in result
        invalid = await gateway.call(job, "player_command", {**action, "requestId": "mine"}, "3")
        assert invalid["error"] == "INVALID_TOOL_ARGUMENTS" and invalid["applied"] is False
        assert len(host.executed) == 1
        await gateway.close(job, True)
        with pytest.raises(StateConflict):
            await gateway.call(job, "player_turn", {}, "4")
        await io.close()

    asyncio.run(run())


@pytest.mark.parametrize("new_consent", [False, True])
@pytest.mark.parametrize(
    "action,detail,arguments",
    [
        (
            {
                "kind": "ATTACK",
                "target": "unit1",
                "destination": {"x": 1, "y": 0},
                "attackFrom": {"x": 0, "y": 0},
            },
            "unit_details",
            {"target": "unit1"},
        ),
        (
            {"kind": "CITY_ATTACK", "target": "city1", "destination": {"x": 1, "y": 0}},
            "city_details",
            {"target": "city1"},
        ),
        (
            {"kind": "DECLARE_WAR", "target": "Greece", "confirmWar": True},
            "player_details",
            {"section": "diplomacy"},
        ),
    ],
)
def test_social_promises_do_not_restrict_hostile_commands(tmp_path, action, detail, arguments, new_consent):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        host.offers.append(
            {
                "proposal": "p1",
                "version": 1,
                "participants": ["Rome", "Greece"],
                "kind": "peace",
                "terms": "No attacks before turn 8",
                "effective": not new_consent,
            }
        )
        job = await gateway.open("planning", "gameplay", "g1", host, {})
        try:
            await gateway.call(
                job,
                "memory_update",
                {
                    "changes": [
                        {
                            "op": "create",
                            "content": {
                                "type": "strategy",
                                "assessment": "Keep peace with Greece",
                                "reasons": ["Honor the stated promise"],
                                "reconsiderWhen": ["No attacks before turn 8"],
                                "subjects": ["Greece"],
                            },
                        }
                    ]
                },
                "strategy",
            )
            await gateway.call(job, "player_turn", {}, "observe")
            await gateway.call(job, detail, arguments, "inspect")
            if new_consent:
                host.offers[0]["effective"] = True
                host.consents.append({"proposal": "p1", "version": 1, "author": "Greece", "decision": "accept"})
                memory.ingest(
                    "g1",
                    [
                        {
                            "id": 1,
                            "kind": "consent",
                            "body": {**host.consents[0], "participants": ["Rome", "Greece"]},
                        }
                    ],
                    1,
                )
                assert stored_records(memory)["mem1"]["body"]["needsReview"]
            result = await gateway.call(
                job,
                "player_command",
                {**action, "intent": {"reason": "Choose war despite the social promise"}},
                "act",
            )
            assert result["done"] and len(host.executed) == 1
            assert host.executed[0]["kind"] == action["kind"]
            assert host.executed[0]["revision"] == "rev1"
            assert "socialVersion" not in host.executed[0]
            assert host.offers[0]["effective"]
            # A changed promise can flag related memory, but an independent
            # decision remains legal without rewriting or withdrawing that strategy.
            if new_consent:
                assert stored_records(memory)["mem1"]["body"]["needsReview"]
        finally:
            await gateway.close(job, True)
            await io.close()

    asyncio.run(run())


def test_greeting_preserves_economic_job_and_chat_cannot_issue_gameplay(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        job = await gateway.open("planning", "gameplay", "g1", host, {})
        await gateway.call(job, "player_turn", {}, "1")
        await gateway.call(job, "city_details", {"target": "city1"}, "city")
        memory.ingest("g1", [{"id": 1, "kind": "message", "body": {"message": "m1", "text": "Hello"}}], 1)
        result = await gateway.call(
            job,
            "player_command",
            {
                "kind": "QUEUE_CONSTRUCTION",
                "target": "city1",
                "selection": "Market",
                "intent": {"reason": "Advance the test assessment"},
            },
            "3",
        )
        assert result["done"] and len(host.executed) == 1
        communication = await gateway.open("chat", "communication", "g1", host, {})
        denied = await gateway.call(communication, "player_command", {"kind": "END_TURN"}, "4")
        assert denied == {"error": "GAMEPLAY_NOT_PERMITTED", "applied": False}
        assert len(host.executed) == 1
        await gateway.close(job, True)
        await gateway.close(communication, True)
        assert not (tmp_path / "workspace/memory-index.md").exists()
        assert not (tmp_path / "workspace/chat.jsonl").exists()
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
        await gateway.call(game, "unit_details", {"target": "unit1"}, "inspect")
        began, release = asyncio.Event(), asyncio.Event()
        original = host.tool

        async def slow(name, args):
            if name == "player_command":
                began.set()
                await release.wait()
            return await original(name, args)

        host.tool = slow
        task = asyncio.create_task(
            gateway.call(
                game, "player_command", {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}}, "2"
            )
        )
        await began.wait()
        await asyncio.wait_for(
            io.call(
                memory.ingest,
                "g1",
                [{"id": 1, "kind": "message", "wake": True, "body": {"message": "m1", "text": "Hello"}}],
                1,
            ),
            1,
        )
        await gateway.call(chat, "chat_read", {}, "messages")
        result = await asyncio.wait_for(
            gateway.call(
                chat,
                "memory_update",
                {
                    "changes": [
                        {
                            "op": "create",
                            "content": {
                                "type": "belief",
                                "claim": "Friendly greeting; motives uncertain",
                                "sources": ["event:1"],
                                "uncertainty": "high",
                                "subjects": ["Greece"],
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
                    {
                        "changes": [
                            {
                                "op": "create",
                                "content": {
                                    "type": "strategy",
                                    "assessment": "Spend Greek gold",
                                    "reasons": ["Carry out the stated assessment"],
                                    "reconsiderWhen": ["Objective achieved"],
                                    "subjects": ["economy"],
                                },
                            }
                        ]
                    },
                ),
            ):
                with pytest.raises(StateConflict, match="PLAYER_CREDENTIAL_MISMATCH"):
                    await gateway.call(job, name, arguments, name)
            assert not stored_records(memory)
            assert not host.executed
        finally:
            await gateway.close(job, False)
            await io.close()

    asyncio.run(run())


def test_game_overview_and_chat_read_deliver_distinct_evidence(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        original = host.tool
        ledger = {
            "offers": [
                {
                    "proposal": "p1",
                    "version": 1,
                    "participants": ["Rome", "Greece"],
                    "terms": "No attacks before turn 8",
                }
            ],
            "consents": [],
        }

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
            stored = stored_records(memory)["observation:game"]["body"]
            assert stored["communication"]["offers"] == ledger["offers"]
            assert "communication" not in shown  # Versioned promises have one model-facing home: chat_read.
            assert "No attacks" not in str(shown)
            ledger["consents"].append(
                {
                    "proposal": "p1",
                    "version": 1,
                    "author": "Greece",
                    "decision": "accept",
                    "intent": {"reason": "Advance the test assessment"},
                }
            )
            await gateway.call(job, "chat_read", {}, "new-consent")
            await gateway.call(
                job,
                "memory_update",
                {
                    "changes": [
                        {
                            "op": "create",
                            "content": {
                                "type": "belief",
                                "claim": "Greece accepted the pact; future conduct is uncertain",
                                "uncertainty": "medium",
                                "sources": ["observation:overview", "observation:communication"],
                                "subjects": ["Greece"],
                            },
                        }
                    ]
                },
                "remember",
            )
            records = stored_records(memory)
            assert records["observation:game"]["body"]["communication"]["consents"] == []
            assert records["observation:communication"]["body"]["consents"] == ledger["consents"]
            assert [r["key"] for r in records["mem1"]["body"]["_evidence"]] == [
                "observation:overview",
                "observation:communication",
            ]
        finally:
            await gateway.close(job, True)
            await io.close()

    asyncio.run(run())
