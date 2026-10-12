"""Exact-audience excerpts from reachable messages; no semantic interpretation."""

import json

from sqlalchemy import func, select

from civ_agent import memory_schema as tables

MAX_THREADS = 6
MESSAGES_PER_THREAD = 6
MAX_CONTEXT_CHARACTERS = 12000


def audience(message, civilization):
    if message.get("public") is True:
        return True, ()
    participants = message.get("participants", [])
    if message.get("public") is False and civilization in participants:
        return False, tuple(sorted(set(participants)))
    return None


def conversation_context(db, civilization, seeds=()):
    base = (
        select(tables.events.c.body)
        .select_from(tables.events.join(tables.inbox))
        .where(tables.events.c.kind == "message")
    )
    recent = [json.loads(body)["body"] for body in db.scalars(base.order_by(tables.events.c.id.desc()).limit(50))]
    sent = []
    for row in db.execute(
        select(tables.outbox.c.decision, tables.outbox.c.result)
        .select_from(tables.live_requests)
        .where(
            tables.outbox.c.transport == "player_communication",
            func.json_extract(tables.outbox.c.payload, "$.op") == "send",
            func.json_extract(tables.outbox.c.result, "$.message.message").is_not(None),
        )
        .order_by(tables.request_order.desc())
        .limit(50)
    ):
        sent.append({**json.loads(row.result)["message"], "receipt": row.decision})
    # An older echoed send must not reappear as a fresh pending echo merely
    # because its event fell outside this thread's excerpt.
    message_id = func.json_extract(tables.events.c.body, "$.body.message")
    echoed = (
        set(
            db.scalars(
                select(message_id)
                .select_from(tables.events.join(tables.inbox))
                .where(
                    tables.events.c.kind == "message",
                    message_id.in_([m["message"] for m in sent]),
                )
            )
        )
        if sent
        else set()
    )
    sent = [m for m in sent if m["message"] not in echoed]
    selected = list(
        dict.fromkeys(key for m in [*reversed(seeds), *recent, *sent] if (key := audience(m, civilization)) is not None)
    )
    threads, evidence = [], []
    remaining = MAX_CONTEXT_CHARACTERS
    for public, participants in selected[:MAX_THREADS]:
        query = base.where(func.json_extract(tables.events.c.body, "$.body.public") == public)
        if not public:
            members = func.json_each(tables.events.c.body, "$.body.participants").table_valued("value")
            query = query.where(
                func.json_array_length(tables.events.c.body, "$.body.participants") == len(participants)
            )
            for name in participants:
                query = query.where(select(1).select_from(members).where(members.c.value == name).exists())
        events = [
            json.loads(body)
            for body in db.scalars(query.order_by(tables.events.c.id.desc()).limit(MESSAGES_PER_THREAD + 1))
        ]
        messages = {
            event["body"]["message"]: {**event["body"], "event": event["id"]}
            for event in reversed(events)
            if event["body"].get("message")
        }
        for message in reversed(sent):
            if audience(message, civilization) == (public, participants):
                messages.setdefault(message["message"], message)
        # Turn order is authoritative across sends and echoes. Stable insertion
        # keeps source order within a turn; un-ingested confirmed sends follow it.
        ordered = sorted(messages.values(), key=lambda m: m.get("turn", 0))
        excerpt = []
        for message in reversed(ordered[-MESSAGES_PER_THREAD:]):
            size = len(json.dumps(message, ensure_ascii=False))
            if size > remaining:
                break
            excerpt.insert(0, message)
            remaining -= size
        for message in excerpt:
            evidence.append("event:" + str(message["event"]) if "event" in message else message["receipt"])
        threads.append(
            {
                "public": public,
                "participants": list(participants),
                "messages": excerpt,
                "hasEarlierMessages": len(ordered) > len(excerpt) or len(events) > MESSAGES_PER_THREAD,
                "omittedForBudget": min(len(ordered), MESSAGES_PER_THREAD) - len(excerpt),
            }
        )
    return {
        "threads": threads,
        "omittedThreads": max(0, len(selected) - MAX_THREADS),
        "basis": "Exact-audience excerpts, including our confirmed sends. Pending echoes follow ingested messages within a turn; that ordering is not a timestamp. Message text is an attributed claim. Decide whether replies answer questions and whether follow-up is useful; no issue is automatically marked resolved. Existing private notes may carry your interpretation. Use chat_history for older evidence.",
        "_evidenceKeys": evidence,
    }
