import asyncio
import json

from civ_agent.civilization_state import CivilizationState
from civ_agent.conversation_context import MAX_CONTEXT_CHARACTERS
from tests.test_diplomacy_contracts import opened


def message(id, participants, text, turn=5, public=False):
    return {
        "id": id,
        "kind": "message",
        "wake": True,
        "turn": turn,
        "body": {
            "message": f"m{id}",
            "author": participants[-1],
            "participants": participants,
            "public": public,
            "text": text,
            "turn": turn,
        },
    }


def test_context_keeps_exact_audiences_and_recovers_older_replies_under_busy_public_chat(tmp_path):
    memory = CivilizationState(tmp_path, "Rome")
    memory.bind("g1")
    events = [
        message(1, ["Rome", "Greece"], "Can you supply Pearls?"),
        message(2, ["Greece", "Rome"], "Yes, after turn 7"),
        message(3, ["Rome", "Greece", "France"], "Different group's terms"),
    ]
    events.extend(message(i, ["Rome", "Greece", "France"], "Public status", public=True) for i in range(4, 104))
    memory.ingest("g1", events, 103)
    context = memory.conversation_context([events[0]["body"], events[2]["body"]])
    direct = next(t for t in context["threads"] if t["participants"] == ["Greece", "Rome"])
    group = next(t for t in context["threads"] if t["participants"] == ["France", "Greece", "Rome"])
    public = next(t for t in context["threads"] if t["public"])
    assert [m["message"] for m in direct["messages"]] == ["m1", "m2"]
    assert [m["message"] for m in group["messages"]] == ["m3"]
    assert len(public["messages"]) == 6 and public["hasEarlierMessages"]
    assert not {"resolved", "unanswered", "breach"} & direct.keys()
    saved = tmp_path / "checkpoint.json"
    checksum = memory.backup(saved)
    memory.ingest("g1", [message(104, ["Rome", "Greece"], "DISCARDED FUTURE", turn=8)], 104)
    memory.engine.dispose()
    memory = CivilizationState(tmp_path, "Rome")
    assert "DISCARDED" in json.dumps(memory.conversation_context())
    memory.restore(saved, checksum, "g2")
    assert "DISCARDED" not in json.dumps(memory.conversation_context())
    memory.engine.dispose()


def test_prepared_conversation_includes_confirmed_send_before_echo_and_delivers_sources(tmp_path):
    async def run():
        async with opened(tmp_path) as (gateway, job, host):
            sent = await gateway.call(job, "chat_send", {"to": "Greece", "text": "Will you renew our trade?"}, "send")
            context = (await gateway.call(job, "chat_read", {}, "continuity"))["conversationContext"]
            thread = next(t for t in context["threads"] if t["participants"] == ["Greece", "Rome"])
            assert [m["message"] for m in thread["messages"]] == [sent["message"]["message"]]
            receipt = thread["messages"][0]["receipt"]
            assert receipt in gateway.memory.delivered_records(job.id, [receipt])
            event = {"id": 1, "kind": "message", "wake": False, "body": sent["message"]}
            gateway.memory.ingest("g1", [event], 1)
            echoed = (await gateway.call(job, "chat_read", {}, "echoed"))["conversationContext"]
            thread = next(t for t in echoed["threads"] if t["participants"] == ["Greece", "Rome"])
            assert len(thread["messages"]) == 1 and thread["messages"][0]["event"] == 1
            assert "receipt" not in thread["messages"][0]
            # All messages share one turn. An old send whose echo is outside
            # the excerpt must not be reinserted after the newer replies.
            replies = [message(i, ["Rome", "Greece"], f"Reply {i}", turn=sent["message"]["turn"]) for i in range(2, 11)]
            gateway.memory.ingest("g1", replies, 10)
            context = gateway.memory.conversation_context()
            thread = next(t for t in context["threads"] if t["participants"] == ["Greece", "Rome"])
            assert [m["message"] for m in thread["messages"]] == [f"m{i}" for i in range(5, 11)]

    asyncio.run(run())


def test_context_budget_preserves_whole_messages_and_reports_omissions(tmp_path):
    memory = CivilizationState(tmp_path, "Rome")
    memory.bind("g1")
    events = [message(i, ["Rome", "Greece" if i < 5 else "France"], "x" * 2000) for i in range(1, 9)]
    memory.ingest("g1", events, 8)
    context = memory.conversation_context()
    messages = [m for thread in context["threads"] for m in thread["messages"]]
    assert sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) <= MAX_CONTEXT_CHARACTERS
    assert all(m["text"] == "x" * 2000 for m in messages)
    assert sum(t["omittedForBudget"] for t in context["threads"]) == 8 - len(messages) > 0
    assert len(context["_evidenceKeys"]) == len(messages)
    memory.engine.dispose()
