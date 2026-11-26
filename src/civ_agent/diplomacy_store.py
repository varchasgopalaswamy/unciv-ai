"""Reachable routing state, separate from model reads and unsettled host requests."""

import hashlib
import json
import time

from sqlalchemy import func, insert, select

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import COMMUNICATION_EVENTS, StateConflict, receipt_is_uncertain
from civ_agent.conversation_context import audience, conversation_context
from civ_agent.decisions_ledger import canonical
from civ_agent.diplomacy_decisions import BATCH_SECONDS, POLICY_VERSION, request_body
from civ_agent.memory_store import RecordStore, encode
from civ_agent.routing_policy import build_routing_bundle, routing_timing, tuple_thread

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


def attention_ids(memory, db):
    """An existing activation can handle younger safe-to-batch messages early."""
    return [
        int(key)
        for key, value in event_states(memory, db).items()
        if not value.get("completed") and not value.get("attempted") and value.get("choice") in {"respond_now", "batch"}
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

    def capture_input(self, direction, bundle):
        """Keep exact already-permitted evidence privately, never in model context."""
        body = request_body(direction, bundle)
        digest = hashlib.sha256(canonical(bundle).encode()).hexdigest()
        prompt_hash = hashlib.sha256(canonical(body["questions"]).encode()).hexdigest()
        key = "routing-input:" + hashlib.sha256(canonical(body).encode()).hexdigest()
        with self.memory.transaction() as db:
            store = RecordStore(db)
            if not store.all(keys=[key]):
                store.write(
                    key,
                    "routing_input",
                    {
                        "policyVersion": POLICY_VERSION,
                        "direction": direction,
                        "inputHash": digest,
                        "promptHash": prompt_hash,
                        "request": body,
                    },
                )

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

    def bundle(self, thread, pending, *, exclude_delivery=None, epoch=None):
        thread = tuple_thread(thread)
        with self.memory.transaction() as db:
            pending = [message for message in pending if audience(message, self.memory.civilization) == thread]
            context = conversation_context(
                db,
                self.memory.civilization,
                [*pending, {"public": thread[0], "participants": list(thread[1])}],
                exclude_message_ids={message["message"] for message in pending if message.get("message")},
            )
            # Objectives are the player's authored assessments; include only ones
            # explicitly scoped to this audience, never another private thread.
            objectives = [
                record["body"]
                for record in RecordStore(db).all(categories={"strategy", "note"}).values()
                if record["body"].get("subjects") and set(record["body"]["subjects"]) <= set(thread[1])
            ]
            all_drafts = [draft for draft in self._drafts(db, epoch=epoch) if tuple_thread(draft["thread"]) == thread]
            if pending:
                pending_ids = {message["message"] for message in pending if message.get("message")}
                states = event_states(self.memory, db)
                routing = [
                    states.get(str(row.id), {})
                    for row in db.execute(
                        select(tables.events.c.id, tables.events.c.body)
                        .select_from(tables.events.join(tables.inbox))
                        .where(
                            tables.events.c.kind == "message",
                            func.json_extract(tables.events.c.body, "$.body.message").in_(pending_ids),
                        )
                    )
                    if audience(json.loads(row.body)["body"], self.memory.civilization) == thread
                ]
                timing = self._timing(
                    routing,
                    received="receivedAt",
                    complete=len(routing) == len(pending),
                )
            else:
                timing = self._timing(
                    [item for item in all_drafts if item["status"] in {"queued", "classifying"}],
                    received="createdAt",
                )
            return build_routing_bundle(
                self.memory.civilization,
                thread,
                pending,
                context,
                current_turn=self.memory._meta(db, "gameTurn"),
                queued_drafts=[
                    {"text": item["payload"]["text"], "status": item["status"]}
                    for item in all_drafts
                    if item["deliveryId"] != exclude_delivery and item["status"] in {"queued", "omitted", "classifying"}
                ],
                player_objectives=objectives,
                timing=timing,
            )

    def _timing(self, states, *, received, complete=True):
        return routing_timing(states, now=self.clock(), received=received, complete=complete)

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
                {
                    "epoch": epoch,
                    "coveredEventIds": ids,
                    "decision": decision,
                    "policyVersion": decision.get("policyVersion", POLICY_VERSION),
                },
            )

    def draft(self, job, call_id, payload, thread, *, epoch=None):
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
                draft["deadline"]
                for draft in self._drafts(db, epoch=epoch)
                if tuple_thread(draft["thread"]) == thread and draft["status"] in {"classifying", "queued"}
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

    def _drafts(self, db, *, epoch=None):
        drafts = [record["body"] for record in RecordStore(db).all(categories={"delivery"}).values()]
        if epoch is None:
            return drafts  # Legacy callers and historical readers retain their original view.
        from pydantic import ValidationError

        from civ_agent.orchestration.delivery_contracts import OutgoingDraft
        from civ_agent.orchestration.store import JournalConflict

        if self.memory._meta(db, "epoch") != epoch:
            raise StateConflict("STALE_EPOCH")
        owner = self.memory._meta(db, "nativeGatewayOwner")
        if not isinstance(owner, dict):
            raise JournalConflict("OUTGOING_OWNER_REQUIRED")
        selected = []
        for draft in drafts:
            try:
                request = OutgoingDraft.model_validate(self.memory._meta(db, "nativeOutgoing:" + draft["deliveryId"]))
                if request.delivery_id != draft["deliveryId"] or draft["payload"]["epoch"] != request.scope.epoch:
                    raise JournalConflict("OUTGOING_DRAFT_CONFLICT")
            except (ValidationError, KeyError, TypeError):
                raise JournalConflict("OUTGOING_DRAFT_CONFLICT") from None
            # A restored historical draft stays exact authored intent. Its old
            # queue status, override and deadline cannot arm a target-epoch send.
            if request.scope.epoch != epoch:
                continue
            if (request.scope.game, request.scope.player) != (owner.get("game"), owner.get("player")):
                raise JournalConflict("COORDINATOR_OWNER_CONFLICT")
            if draft.get("owner") != self.memory.civilization:
                raise JournalConflict("OUTGOING_DRAFT_CONFLICT")
            selected.append(draft)
        return selected

    def drafts(self, *, epoch=None):
        with self.memory.transaction() as db:
            return self._drafts(db, epoch=epoch)

    def get(self, id, *, epoch=None):
        return next((row for row in self.drafts(epoch=epoch) if row["deliveryId"] == id), None)

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

    def begin_native_delivery(self, epoch, id, job, payload, admission=None):
        """Admit one exact send with its code-owned local task identity."""
        from civ_agent.orchestration.delivery_contracts import DeliveryAdmission

        admission = DeliveryAdmission.model_validate(admission).model_dump(mode="json")
        with self.memory.transaction() as db:
            self._check(db, epoch)
            self.memory._check_run_failure(db)
            store = RecordStore(db)
            draft = store.all(keys=[id])[id]["body"]
            identifier = "r" + hashlib.sha256((epoch + ":" + id).encode()).hexdigest()[:24]
            expected = {**draft["payload"], "epoch": epoch, "requestId": identifier}
            if job != "dispatch-" + identifier or payload != expected:
                raise StateConflict("OUTGOING_NATIVE_BINDING_CONFLICT")
            if draft["status"] == "sending" and draft.get("admission") != admission:
                raise StateConflict("OUTGOING_ADMISSION_CONFLICT")
            row = db.execute(select(tables.jobs).where(tables.jobs.c.id == job)).mappings().one_or_none()
            if row is None:
                self.memory._start_job(
                    db, job, "delivery", {}, None, {"epoch": epoch, "cursor": 0, "records": {}, "contextIds": []}
                )
            elif row["epoch"] != epoch or row["purpose"] != "delivery" or row["status"] == "COMPLETED":
                raise StateConflict("OUTGOING_DELIVERY_JOB_CONFLICT")
            elif row["status"] == "INTERRUPTED":
                db.execute(tables.jobs.update().where(tables.jobs.c.id == job).values(status="RUNNING"))
            store.write(
                id,
                "delivery",
                {
                    **draft,
                    "status": "sending",
                    "requestId": identifier,
                    "admission": admission,
                },
            )

    def resume_native_delivery(self, epoch, id, job, payload):
        """Resume domain attribution only, without a source role or local outbox."""
        with self.memory.transaction() as db:
            self._check(db, epoch)
            draft = RecordStore(db).all(keys=[id])[id]["body"]
            identifier = "r" + hashlib.sha256((epoch + ":" + id).encode()).hexdigest()[:24]
            row = db.execute(select(tables.jobs).where(tables.jobs.c.id == job)).mappings().one_or_none()
            if (
                job != "dispatch-" + identifier
                or draft.get("requestId") != identifier
                or payload != {**draft["payload"], "epoch": epoch, "requestId": identifier}
                or row is None
                or row["epoch"] != epoch
                or row["purpose"] != "delivery"
            ):
                raise StateConflict("OUTGOING_DELIVERY_JOB_CONFLICT")
            if row["status"] == "INTERRUPTED":
                db.execute(tables.jobs.update().where(tables.jobs.c.id == job).values(status="RUNNING"))

    def apply_native_delivery(self, epoch, id, job, payload, receipt):
        with self.memory.transaction() as db:
            self._check(db, epoch)
            store = RecordStore(db)
            draft = store.all(keys=[id])[id]["body"]
            native = {
                "job": job,
                "operationId": payload["requestId"],
                "requestSha256": hashlib.sha256(encode(["player_communication", payload]).encode()).hexdigest(),
                "receiptSha256": hashlib.sha256(encode(receipt).encode()).hexdigest(),
            }
            if draft.get("native") is not None:
                if draft["native"] != native:
                    raise StateConflict("OUTGOING_NATIVE_OUTCOME_CONFLICT")
                return draft
            now = self.clock()
            value = {
                **draft,
                "status": "failed" if "error" in receipt else "sent",
                "native": native,
                "deliveredAt": now,
                "queueDelaySeconds": now - draft["createdAt"],
            }
            if "error" in receipt:
                value["error"] = receipt["error"]
            else:
                value["message"] = receipt["message"]
            store.write(id, "delivery", value)
            return value

    def finish_native_delivery(self, epoch, id):
        with self.memory.transaction() as db:
            self._check(db, epoch)
            store = RecordStore(db)
            draft = store.all(keys=[id])[id]["body"]
            proof = draft.get("native")
            identifier = "r" + hashlib.sha256((epoch + ":" + id).encode()).hexdigest()[:24]
            if (
                not proof
                or draft["status"] not in {"sent", "failed"}
                or proof["operationId"] != identifier
                or proof["job"] != "dispatch-" + identifier
            ):
                raise StateConflict("OUTGOING_NATIVE_COMPLETION_REQUIRED")
            outcome = store.all(keys=["native:" + identifier], project=False).get("native:" + identifier)
            marker = outcome["body"].get("native", {}) if outcome else {}
            if any(marker.get(key) != proof[key] for key in ("operationId", "requestSha256", "receiptSha256")):
                raise StateConflict("OUTGOING_NATIVE_OUTCOME_CONFLICT")
            row = db.execute(select(tables.jobs).where(tables.jobs.c.id == proof["job"])).mappings().one_or_none()
            if row is None or row["epoch"] != epoch or row["purpose"] != "delivery":
                raise StateConflict("OUTGOING_DELIVERY_JOB_CONFLICT")
            if row["status"] == "INTERRUPTED":
                db.execute(tables.jobs.update().where(tables.jobs.c.id == proof["job"]).values(status="RUNNING"))
        self.memory.finish_job(proof["job"], True)

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
            or receipt_is_uncertain(receipt)
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


def delivery_status(draft):
    receipt = draft.get("receipt")
    if "native" in draft:
        receipt = (
            {"error": draft["error"]} if "error" in draft else {"message": draft["message"], "nativeOutcome": None}
        )
    return {
        "deliveryId": draft["deliveryId"],
        "disposition": "queued" if draft["status"] == "classifying" else draft["status"],
        "delivered": draft["status"] == "sent",
        "deadline": draft["deadline"],
        "text": draft["payload"]["text"],
        "audience": draft["thread"],
        **({"receipt": receipt} if receipt is not None else {}),
    }
