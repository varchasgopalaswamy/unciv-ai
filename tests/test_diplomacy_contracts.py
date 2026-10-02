"""Model contracts must translate to native requests without publishing private intent."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState
from civ_agent.game_commands import command_arguments
from civ_agent.memory_records import Intent
from civ_agent.proposal_gateway import ProposalGateway, public_result
from civ_agent.review_store import ReviewStore
from civ_agent.service_io import ServiceIO
from civ_agent.tool_schemas import tool_model
from tests.test_proposal_gateway import Host
from tests.test_strategic_review import assessment


class ContractHost(Host):
    def __init__(self):
        super().__init__()
        self.validator = Draft202012Validator(
            json.loads(Path("src/main/resources/uncivai/schemas/communication.json").read_text())
        )
        self.writes = []
        self.offers = [
            {
                "proposal": "pabc12",
                "version": 1,
                "author": "Greece",
                "participants": ["Rome", "Greece"],
                "kind": "peace",
                "terms": "Avoid war while useful",
                "conditions": "If interests align",
                "turn": 0,
                "effective": False,
            }
        ]

    async def tool(self, name, args):
        if name != "player_communication":
            return await super().tool(name, args)
        self.validator.validate(args)
        if args["op"] in {"send", "offer", "revise", "accept", "withdraw", "reconsider"}:
            self.writes.append(copy.deepcopy(args))
            if args["op"] == "send":
                return {
                    "message": {
                        "message": f"m{len(self.writes):05d}",
                        "author": "Rome",
                        "participants": ["Rome", args["to"]] if "to" in args else ["Rome", "Greece", "France"],
                        "public": "to" not in args,
                        "text": args["text"],
                        "turn": 0,
                    }
                }
            if args["op"] in {"offer", "revise"}:
                offer = {
                    **self.offers[0],
                    **{k: v for k, v in args.items() if k not in {"op", "epoch", "requestId"}},
                    "author": "Rome",
                    "proposal": args.get("proposal", "pnew12"),
                    "version": 1
                    if args["op"] == "offer"
                    else max(o["version"] for o in self.offers if o["proposal"] == args["proposal"]) + 1,
                }
                self.offers.append(offer)
                return {"offer": offer}
            return {"proposal": args["proposal"], "version": args["version"], "decision": args["op"]}
        return await super().tool(name, args)


@asynccontextmanager
async def opened(tmp_path, purpose="communication"):
    memory = CivilizationState(tmp_path / "memory", "Rome")
    io, host = ServiceIO(), ContractHost()
    gateway = ProposalGateway(memory, io, tmp_path / "work")
    job = await gateway.open("first", purpose, "g1", host, {}, 0)
    try:
        yield gateway, job, host
    finally:
        await io.close()
        memory.engine.dispose()


@pytest.mark.parametrize("decision", ["accept", "withdraw", "reconsider"])
def test_commitment_templates_obey_native_schema_and_keep_intent_private(tmp_path, decision):
    async def run():
        async with opened(tmp_path) as (gateway, job, host):
            if decision != "accept":
                host.consents = [{"proposal": "pabc12", "version": 1, "author": "Rome", "decision": "accept"}]
            read = await gateway.call(job, "chat_read", {}, "read")
            assert read["chatRecipients"] == ["Greece", "France"] and "channels" not in read
            offer = read["offers"][0]
            assert offer["proposalId"] == "pabc12" and "proposal" not in offer
            assert public_result(read) == read
            response = next(r for r in offer["responses"] if r["arguments"]["decision"] == decision)
            args = {**response["arguments"], "intent": {"reason": "PRIVATE leverage assessment"}}
            if decision != "accept":
                assert response["requiredFields"] == ["publicReason"]
                args["publicReason"] = "Our circumstances changed."
            result = await gateway.call(job, response["tool"], args, "choose")
            assert "error" not in result
            sent = host.writes[-1]
            assert sent["op"] == decision and sent["proposal"] == "pabc12" and sent["version"] == 1
            assert "reason" not in sent if decision == "accept" else sent["reason"] == args["publicReason"]
            assert "PRIVATE" not in json.dumps(sent) and "intent" not in sent
            assert gateway.memory.summary(job=job.id)["actions"][-1]["reason"] == "PRIVATE leverage assessment"

    asyncio.run(run())


@pytest.mark.parametrize(
    "extra",
    [{"reason": "Private?"}, {"publicReason": "Unneeded"}, {"proposalId": "A lengthy description of proposed terms"}],
)
def test_invalid_acceptance_returns_action_specific_help_without_dispatch(tmp_path, extra):
    async def run():
        async with opened(tmp_path) as (gateway, job, host):
            result = await gateway.call(
                job,
                "commitment_decide",
                {
                    "proposalId": "pabc12",
                    "decision": "accept",
                    "intent": {"reason": "PRIVATE"},
                    **extra,
                },
                "invalid",
            )
            assert result["error"] == "INVALID_TOOL_ARGUMENTS" and result["applied"] is False
            assert set(result["expectedArguments"]["properties"]) == {"proposalId", "decision", "intent"}
            assert "PRIVATE" not in json.dumps(result) and not host.writes

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["peace", "other"])
@pytest.mark.parametrize("purpose", ["gameplay", "communication"])
def test_commitment_creation_and_revision_use_distinct_short_references(tmp_path, kind, purpose):
    async def run():
        async with opened(tmp_path, purpose) as (gateway, job, host):
            args = {
                "participants": ["Rome", "France"],
                "terms": "Discuss borders",
                "kind": kind,
                "conditions": "When useful",
                "intent": {"reason": "Private negotiation"},
            }
            result = await gateway.call(job, "commitment_offer", args, "offer")
            reference = result["offer"]["proposalId"]
            await gateway.call(job, "chat_read", {}, "read")
            revised = await gateway.call(
                job,
                "commitment_offer",
                {**args, "terms": "Discuss borders next turn", "reviseProposalId": reference},
                "revise",
            )
            assert "error" not in revised and host.writes[-1]["op"] == "revise"
            assert host.writes[-1]["proposal"] == reference
            assert "reviseProposalId" not in host.writes[-1]
            summary = gateway.memory.summary(job=job.id)
            assert [a["action"] for a in summary["actions"]] == [
                f"Offered {kind} commitment {reference} v1 between Rome, France",
                f"Revised {kind} commitment {reference} v2 between Rome, France",
            ]
            assert all(a["outcome"] == "confirmed" for a in summary["actions"])
            history = gateway.memory.history(0)["actions"]
            assert [a["action"] for a in history] == [a["action"] for a in summary["actions"]]
            assert all(a["outcome"] == "confirmed" for a in history)
            rejected = await gateway.call(job, "commitment_offer", {**args, "participants": ["France"]}, "missing-self")
            assert rejected["error"] == "INVALID_TOOL_ARGUMENTS" and len(host.writes) == 2

    asyncio.run(run())


def test_plain_chat_recipients_and_native_actions_are_distinct(tmp_path):
    async def run():
        async with opened(tmp_path) as (gateway, job, host):
            rejected = await gateway.call(job, "chat_send", {"to": "Egypt", "text": "Help us"}, "ai")
            assert rejected["error"] == "INVALID_TOOL_ARGUMENTS"
            assert rejected["chatRecipients"] == ["Greece", "France"] and not host.writes
            schemas = {t["name"]: t["inputSchema"] for t in await gateway.tools(job)}
            assert "nativeAction" not in schemas["chat_send"]["properties"]
            result = await gateway.call(
                job,
                "chat_send",
                {"to": "Greece", "text": "Hello", "nativeAction": {"kind": "DENOUNCE", "target": "Greece"}},
                "native",
            )
            assert result["error"] == "INVALID_TOOL_ARGUMENTS" and not host.writes
            assert "nativeAction" in tool_model("chat_send", "gameplay").model_json_schema()["properties"]

    asyncio.run(run())


@pytest.mark.parametrize("target", ["Greece", None])
def test_concurrent_replies_reconsider_only_unseen_messages_without_extra_reads(tmp_path, target):
    async def run():
        async with opened(tmp_path) as (gateway, job, host):
            other = await gateway.open("other", "gameplay", "g1", host, {}, 0)
            await gateway.call(job, "chat_read", {}, "read")
            await gateway.call(other, "chat_read", {}, "read")
            args = {"text": "We agree", **({"to": target} if target else {})}
            first, second = await asyncio.gather(
                gateway.call(job, "chat_send", args, "reply"), gateway.call(other, "chat_send", args, "reply")
            )
            assert "error" not in first
            assert second["error"] == "CONVERSATION_UPDATED" and second["applied"] is False
            assert second["messages"] == [first["message"]] and len(host.writes) == 1
            assert await gateway.call(other, "chat_send", args, "reply") == second  # Idempotent rejection.
            # Fresh deliberate decision may add a different message after seeing the reply.
            third = await gateway.call(other, "chat_send", {**args, "text": "One further condition"}, "new-decision")
            assert "error" not in third and len(host.writes) == 2
            # Inbox subscription has not echoed either send yet. A read still returns both.
            current = await gateway.call(job, "chat_read", {}, "fresh-read")
            assert len(current["recentSentMessages"]) == 2
            assert "error" not in await gateway.call(job, "chat_send", {**args, "text": "Understood"}, "informed")
            if target:
                # A reply to Greece does not invalidate a France conversation.
                assert "error" not in await gateway.call(
                    other, "chat_send", {"to": "France", "text": "Hello"}, "other-player"
                )

    asyncio.run(run())


def test_initiative_numbers_are_checked_against_delivered_report(tmp_path):
    async def run():
        async with opened(tmp_path, "gameplay") as (gateway, job, host):
            await gateway.call(job, "player_turn", {}, "read")
            entry = {"initiative": 1, "decision": "deferred", "reason": "Wait for reconnaissance"}
            absent = await gateway.call(
                job, "player_command", {"kind": "END_TURN", "initiativeDecisions": [entry]}, "no-report"
            )
            assert absent["error"] == "INVALID_INITIATIVE" and absent["availableInitiatives"] == []
            store = ReviewStore(gateway.memory)
            store.prepare("opening", {"turn": 0, "observation": {"turn": 0}}, {}, 60)
            store.complete("opening", assessment())
            await gateway.close(job, True)
            new = await gateway.open("new", "gameplay", "g1", host, {}, 0)
            await gateway.call(new, "player_turn", {}, "read")
            invalid = await gateway.call(
                new,
                "player_command",
                {"kind": "END_TURN", "initiativeDecisions": [{**entry, "initiative": 2}]},
                "unknown",
            )
            assert invalid["error"] == "INVALID_INITIATIVE" and invalid["availableInitiatives"] == [1]
            assert not host.executed

    asyncio.run(run())


def test_initiatives_are_end_turn_only_unique_and_cannot_supply_report_date():
    entry = {"initiative": 1, "decision": "acted", "reason": "Contacted France"}
    for args in [
        {"kind": "END_TURN", "initiativeDecisions": [entry, entry]},
        {"kind": "END_TURN", "initiativeDecisions": [{**entry, "reviewTurn": 99}]},
        {"kind": "DISMISS_NOTIFICATIONS", "initiativeDecisions": [entry]},
    ]:
        with pytest.raises(ValidationError):
            command_arguments(args)
    assert "initiativeDecisions" not in Intent.model_fields
    with pytest.raises(ValidationError):
        tool_model("chat_send", "communication").model_validate({"text": "Hi", "initiativeDecisions": [entry]})


def test_job_opened_during_a_pending_send_still_sees_the_completed_reply(tmp_path):
    async def run():
        async with opened(tmp_path) as (gateway, first, host):
            began, release = asyncio.Event(), asyncio.Event()
            native = host.tool

            async def delayed(name, args):
                if args.get("op") == "send":
                    began.set()
                    await release.wait()
                return await native(name, args)

            host.tool = delayed
            pending = asyncio.create_task(gateway.call(first, "chat_send", {"to": "Greece", "text": "Agreed"}, "send"))
            try:
                await asyncio.wait_for(began.wait(), 3)
                second = await gateway.open("second", "gameplay", "g1", host, {}, 0)
                await gateway.call(second, "chat_read", {}, "read")
            finally:
                release.set()
            sent = await pending
            reply = await gateway.call(second, "chat_send", {"to": "Greece", "text": "Agreed"}, "send")
            assert reply["error"] == "CONVERSATION_UPDATED"
            assert reply["messages"] == [sent["message"]] and len(host.writes) == 1

    asyncio.run(run())


def test_sent_message_supplement_does_not_repeat_delivered_or_consumed_history(tmp_path):
    async def run():
        async with opened(tmp_path) as (gateway, job, host):
            sent = await gateway.call(job, "chat_send", {"to": "Greece", "text": "Our terms"}, "send")
            first = await gateway.call(job, "chat_read", {}, "read")
            assert first["recentSentMessages"] == [sent["message"]]
            second = await gateway.call(job, "chat_read", {}, "reread")
            assert "recentSentMessages" not in second
            gateway.memory.ingest("g1", [{"id": 1, "kind": "message", "body": sent["message"], "wake": False}], 1)
            echoed = await gateway.call(job, "chat_read", {}, "echo")
            assert len(echoed["messages"]) == 1 and "recentSentMessages" not in echoed
            await gateway.close(job, True)
            later = await gateway.open("later", "communication", "g1", host, {}, 0)
            view = await gateway.call(later, "chat_read", {}, "later")
            assert not view["messages"] and "recentSentMessages" not in view

    asyncio.run(run())
