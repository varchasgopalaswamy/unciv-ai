"""Reachable routing state, separate from model reads and unsettled host requests."""

import hashlib
import json
import time

from sqlalchemy import insert, select

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import COMMUNICATION_EVENTS, StateConflict
from civ_agent.conversation_context import audience, conversation_context
from civ_agent.diplomacy_decisions import BATCH_SECONDS, POLICY_VERSION
from civ_agent.memory_store import RecordStore, encode

EVENTS_KEY = "routing:events"


def event_states(memory, db):
    return memory._meta(db, EVENTS_KEY, {})


def ready_ids(memory, db, *, now=None):
    now = time.time() if now is None else now
    return [
        int(key)
        for key, value in event_states(memory, db).items()
        if not value.get("completed")
        and not value.get("attempted")
        and (value.get("choice") == "respond_now" or value.get("choice") == "batch" and value["deadline"] <= now)
    ]


def delivered(memory, db, ids, *, completed=False):
    states = event_states(memory, db)
    for id in ids:
        if str(id) in states:
            states[str(id)]["completed" if completed else "attempted"] = True
    memory._set(db, EVENTS_KEY, states)


class DiplomacyStore:
    def __init__(self, memory, clock=time.time):
        self.memory, self.clock = memory, clock

    def _check(self, db, epoch):
        if self.memory._meta(db, "epoch") != epoch:
            raise StateConflict("STALE_EPOCH")
        if self.memory._meta(db, "paused", False):
            raise StateConflict("STATE_PAUSED")

    def incoming(self):
        with self.memory.transaction() as db:
            states = event_states(self.memory, db)
            rows = db.execute(
                select(tables.events)
                .select_from(tables.events.join(tables.inbox))
                .where(tables.events.c.wake == 1, COMMUNICATION_EVENTS)
                .order_by(tables.events.c.id)
            ).mappings()
            return [
                {"rowId": row["id"], "event": json.loads(row["body"]), "routing": states.get(str(row["id"]), {})}
                for row in rows
                if not states.get(str(row["id"]), {}).get("completed")
                and not states.get(str(row["id"]), {}).get("attempted")
            ]

    def bundle(self, thread, pending, *, exclude_delivery=None):
        with self.memory.transaction() as db:
            context = conversation_context(db, self.memory.civilization, pending)
            history = [item for item in context["threads"] if audience(item, self.memory.civilization) == thread]
            # Objectives are the player's authored assessments; include only ones
            # explicitly scoped to this audience, never another private thread.
            objectives = [
                record["body"]
                for record in RecordStore(db).all(categories={"strategy", "note"}).values()
                if record["body"].get("subjects") and set(record["body"]["subjects"]) <= set(thread[1])
            ]
            drafts = [
                record["body"]
                for record in RecordStore(db).all(categories={"delivery"}).values()
                if tuple_thread(record["body"]["thread"]) == thread
                and record["body"]["deliveryId"] != exclude_delivery
                and record["body"]["status"] in {"queued", "omitted", "classifying"}
            ]
            return {
                "audience": thread,
                "pending": pending,
                "recentExchanges": history,
                "queuedDrafts": [{"text": item["payload"]["text"], "status": item["status"]} for item in drafts],
                "playerObjectives": objectives,
                "pendingProposals": [],
            }

    def route(self, epoch, ids, decision, fingerprint):
        with self.memory.transaction() as db:
            self._check(db, epoch)
            states = event_states(self.memory, db)
            ids = [
                id
                for id in ids
                if not states.get(str(id), {}).get("attempted")
                and not states.get(str(id), {}).get("completed")
                and states.get(str(id), {}).get("fallback") != "routing_deadline"
            ]
            if not ids:
                return  # A late classifier cannot undo deadline fallback or model delivery.
            oldest = min((states.get(str(id), {}).get("receivedAt", self.clock()) for id in ids), default=self.clock())
            for id in ids:
                prior = states.get(str(id), {})
                states[str(id)] = {
                    **prior,
                    **decision,
                    "fingerprint": fingerprint,
                    "deadline": min(prior.get("deadline", oldest + BATCH_SECONDS), oldest + BATCH_SECONDS),
                }
            self.memory._set(db, EVENTS_KEY, states)
            RecordStore(db).write(
                "routing:" + fingerprint,
                "routing",
                {"epoch": epoch, "coveredEventIds": ids, "decision": decision, "policyVersion": POLICY_VERSION},
            )

    def draft(self, job, call_id, payload, thread):
        id = "delivery-" + hashlib.sha256((job.id + ":" + call_id).encode()).hexdigest()[:24]
        with self.memory.transaction() as db:
            self.memory._validate(db, job.id)
            store = RecordStore(db)
            existing = store.all(keys=[id]).get(id)
            if existing:
                if existing["body"]["payload"] != payload:
                    raise StateConflict("CALL_CONFLICT")
                return existing["body"]
            deadlines = [
                r["body"]["deadline"]
                for r in store.all(categories={"delivery"}).values()
                if tuple_thread(r["body"]["thread"]) == thread and r["body"]["status"] in {"classifying", "queued"}
            ]
            now = self.clock()
            value = {
                "deliveryId": id,
                "payload": payload,
                "thread": thread,
                "createdAt": now,
                "deadline": min([now + BATCH_SECONDS, *deadlines]),
                "status": "classifying",
                "owner": self.memory.civilization,
                "sourceJob": job.id,
            }
            store.write(id, "delivery", value)
            return value

    def drafts(self):
        with self.memory.transaction() as db:
            return [r["body"] for r in RecordStore(db).all(categories={"delivery"}).values()]

    def get(self, id):
        return next((row for row in self.drafts() if row["deliveryId"] == id), None)

    def update(self, epoch, id, *, only_status=None, **changes):
        with self.memory.transaction() as db:
            self._check(db, epoch)
            store = RecordStore(db)
            prior = store.all(keys=[id]).get(id)
            if not prior or prior["category"] != "delivery":
                raise StateConflict("UNKNOWN_DELIVERY")
            if only_status is not None and prior["body"]["status"] != only_status:
                return prior["body"]
            value = {**prior["body"], **changes}
            store.write(id, "delivery", value)
            return value

    def prepare_delivery(self, epoch, id):
        """Create the ordinary outbox identity and mark sending in one transaction."""
        with self.memory.transaction() as db:
            self._check(db, epoch)
            self.memory._check_run_failure(db)
            store = RecordStore(db)
            draft = store.all(keys=[id])[id]["body"]
            if draft["status"] in {"sent", "cancelled", "failed"}:
                return None
            request_id = "r" + hashlib.sha256((epoch + ":" + id).encode()).hexdigest()[:24]
            row = (
                db.execute(
                    select(tables.outbox).select_from(tables.live_requests).where(tables.outbox.c.id == request_id)
                )
                .mappings()
                .first()
            )
            if row:
                return dict(row)
            job = "dispatch-" + request_id
            self.memory._start_job(
                db, job, "delivery", {}, None, {"epoch": epoch, "cursor": 0, "records": {}, "contextIds": []}
            )
            payload = {**draft["payload"], "epoch": epoch, "requestId": request_id}
            decision = store.handle("dec")
            store.write(
                decision, "decision", {"command": payload, "outcome": "pending", "receipt": None, "deliveryId": id}, job
            )
            row = {
                "id": request_id,
                "job": job,
                "transport": "player_communication",
                "payload": encode(payload),
                "result": None,
                "decision": decision,
            }
            db.execute(insert(tables.outbox).values(**row))
            db.execute(insert(tables.outgoing).values(id=request_id))
            store.write(id, "delivery", {**draft, "status": "sending", "requestId": request_id})
            return row

    def delivery_request(self, request_id):
        with self.memory.transaction() as db:
            row = (
                db.execute(
                    select(tables.outbox).select_from(tables.live_requests).where(tables.outbox.c.id == request_id)
                )
                .mappings()
                .one()
            )
            return dict(row)

    def settle(self, id, request, receipt):
        # receipt() is idempotent. A crash between these transactions is repaired
        # from the stored host receipt, without another send or model activation.
        if (
            not isinstance(receipt, dict)
            or receipt.get("queued")
            or (
                "error" not in receipt
                and (not isinstance(receipt.get("message"), dict) or not receipt["message"].get("message"))
            )
            or ("error" in receipt and not isinstance(receipt["error"], str))
            or receipt.get("error") in ("NOT_ENACTED", "COMMUNICATION_UNAVAILABLE", "INTERNAL_ERROR")
        ):
            raise StateConflict("DELIVERY_RECEIPT_UNCERTAIN")
        self.memory.receipt(request["id"], receipt)
        with self.memory.transaction() as db:
            store = RecordStore(db)
            draft = store.all(keys=[id])[id]["body"]
            store.write(
                id,
                "delivery",
                {
                    **draft,
                    "status": "failed" if "error" in receipt else "sent",
                    "receipt": receipt,
                    "deliveredAt": self.clock(),
                    "queueDelaySeconds": self.clock() - draft["createdAt"],
                },
            )
        self.memory.finish_job(request["job"], True)


def tuple_thread(thread):
    return thread[0], tuple(thread[1])


def delivery_status(draft):
    return {
        "deliveryId": draft["deliveryId"],
        "disposition": "queued" if draft["status"] == "classifying" else draft["status"],
        "delivered": draft["status"] == "sent",
        "deadline": draft["deadline"],
        "text": draft["payload"]["text"],
        "audience": draft["thread"],
        **({"receipt": draft["receipt"]} if "receipt" in draft else {}),
    }
