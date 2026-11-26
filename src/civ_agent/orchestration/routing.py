"""Local routing tasks over persisted broker requests and native delivery receipts."""

import asyncio
import hashlib
import hmac
import json
import time
from contextvars import ContextVar
from uuid import uuid4

import aiohttp
from pydantic import ValidationError
from sqlalchemy import func, select

from civ_agent import memory_schema as tables
from civ_agent.diplomacy_decisions import POLICY_VERSION, fallback
from civ_agent.diplomacy_store import DiplomacyStore, delivery_status
from civ_agent.host_effects import PreparedHostEffect
from civ_agent.orchestration.contracts import (
    NativeAuxiliaryTask,
    Scope,
    WorkflowLink,
    canonical,
    digest,
)
from civ_agent.orchestration.delivery_contracts import (
    DeliveryAdmission,
    FrozenNativeDelivery,
    NativeDeliveryApplication,
    NativeDeliveryExecution,
    NativeDeliveryPreparation,
    OutgoingDelivery,
    OutgoingDraft,
    OutgoingState,
    delivery_request_id,
    validate_native_delivery_receipt,
)
from civ_agent.orchestration.host_effect_service import execute_prepared_command, native_effect_key
from civ_agent.orchestration.routing_contracts import (
    RoutingSubmission,
    routing_submission,
    validate_routing_reply,
)
from civ_agent.orchestration.routing_models import (
    DraftDecision,
    DraftPlan,
    FinishRouting,
    RoutingLink,
    RoutingReply,
    RoutingResult,
    RoutingRun,
)
from civ_agent.orchestration.store import JournalConflict
from civ_agent.routing_policy import protected_thread, tuple_thread, validate_decision
from civ_agent.tool_schemas import tool_model


class NativeRouting:
    def __init__(self, root, broker, settings, *, human_players=(), reply_checkpoint=None, delivery_checkpoint=None):
        from civ_agent.decisions_policy import DecisionsSettings, decisions_values

        settings = DecisionsSettings.model_validate(decisions_values(settings))
        self.root, self.broker, self.settings = root, broker, settings
        # Routing and the journal must agree on delivery/acknowledgement semantics.
        # Enable this before any routed jobs or observations are opened.
        root.memory.routing_enabled = True
        self.tasks: dict[str, asyncio.Task] = {}
        self.admission: ContextVar[DeliveryAdmission | None] = ContextVar("local_delivery", default=None)
        self.failure: BaseException | None = None
        self.suspended = False
        self.human_players = set(human_players)
        self.store = DiplomacyStore(root.memory)
        self.reply_checkpoint, self.delivery_checkpoint = reply_checkpoint, delivery_checkpoint
        self.seal_key = native_effect_key(root.memory)

    async def record_link(self, link: RoutingLink):
        self.check(link.scope)
        await self.root.put("nativeRoutingLink:" + link.id, link.model_dump(mode="json"))

    async def results(self, scope):
        self.check(scope)
        with self.root.memory.transaction() as db:
            rows = list(
                db.scalars(
                    select(tables.meta.c.value).where(
                        tables.meta.c.key.like("nativeRoutingLink:%"),
                        func.json_extract(tables.meta.c.value, "$.scope.epoch") == scope.epoch,
                    )
                )
            )
        links = [RoutingLink.model_validate_json(row) for row in rows]
        for link in links:
            self.check(link.scope)
        return [WorkflowLink(id=link.id, kind=link.kind) for link in links]

    def suspend(self):
        self.suspended = True
        for task in self.tasks.values():
            task.cancel()

    async def close(self):
        self.suspend()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    def raise_if_failed(self):
        if self.failure is not None:
            raise self.failure

    def check(self, scope):
        self.root.check_owner(scope)
        with self.root.memory.transaction() as db:
            if self.root.memory._meta(db, "epoch") != scope.epoch:
                raise JournalConflict("ROUTING_EPOCH_CONFLICT")

    async def set_hold(self, scope, held):
        self.check(scope)
        await self.root.put("nativeRoutingHeld:" + scope.epoch, bool(held))

    async def held(self, scope):
        self.check(scope)
        gateway = self.root.gateway
        return bool(
            await self.root.get("nativeRoutingHeld:" + scope.epoch, True)
            or gateway.paused
            or gateway.checkpoint_requested
            or gateway.run_failure
        )

    async def connection(self, epoch):
        if self.root.communication is not None:
            return await self.root.communication.connection(epoch)
        if self.root.connection is None:
            raise JournalConflict("ROUTING_CONNECTION_UNAVAILABLE")
        return self.root.connection

    async def social(self, epoch):
        value = await (await self.connection(epoch)).tool("player_communication", {"op": "state", "epoch": epoch})
        if value.get("civilization") != self.root.civilization or "error" in value:
            raise JournalConflict("ROUTING_PLAYER_CONFLICT")
        return value

    async def protected_now(self, request, draft):
        # Bound the native protection read by the existing routing deadline.
        remaining = max(0.05, draft["deadline"] - time.time())
        async with asyncio.timeout(min(5, remaining)):
            return self.protected(tuple_thread(draft["thread"]), await self.social(request.scope.epoch))

    def protected(self, thread, social):
        return protected_thread(thread, social, player=self.root.civilization, human_players=self.human_players)

    async def start(self, request, key):
        """Start only the three fixed routing operations; the domain stores own their facts."""
        if self.suspended:
            raise JournalConflict("LOCAL_ROUTING_SUSPENDED")
        self.raise_if_failed()
        scope = request.request.scope if isinstance(request, OutgoingDelivery) else request.scope
        self.check(scope)
        identity = scope.identity("task", key)
        existing = self.tasks.get(identity)
        if existing is not None and not existing.cancelled():
            return existing
        kind = (
            "routing" if key == "routing" else "outgoing-delivery" if key.startswith("delivery") else "outgoing-draft"
        )
        await self.record_link(
            RoutingLink(scope=scope, id=identity, kind=kind, request=request if key == "routing" else None)
        )
        owner = DeliveryAdmission(task_id=identity, run_id=uuid4().hex)

        async def run():
            token = self.admission.set(owner)
            try:
                if key == "routing":
                    recorded = self.root.store.receipt(request.scope, "routing-result")
                    if recorded is not None:
                        result = RoutingResult.model_validate(recorded)
                        # Re-enter the exact finish operation, including its
                        # full frozen request check, without asking the broker
                        # for a new envelope after this decision committed.
                        return await self.finish(
                            FinishRouting(request=request, reply=RoutingReply(outcome=result.outcome))
                        )
                    return await self.finish(FinishRouting(request=request, reply=await self.exchange(request)))
                if key == "outgoing":
                    return await self.run_outgoing(request)
                if key in {"delivery", "delivery-force"}:
                    return await self.run_delivery(request)
                raise JournalConflict("LOCAL_ROUTING_TASK_UNKNOWN")
            finally:
                self.admission.reset(token)

        task = asyncio.create_task(run(), name="routing:" + key)
        self.tasks[identity] = task

        def finished(completed):
            if self.tasks.get(identity) is completed:
                self.tasks.pop(identity, None)
            if not completed.cancelled() and completed.exception() is not None:
                self.failure = completed.exception()
                if not self.root.gateway.run_failure:
                    self.root.gateway.run_failure = {"error": "LOCAL_ROUTING_FAILED"}

        task.add_done_callback(finished)
        return task

    async def run_outgoing(self, request):
        plan = await self.prepare(request)
        decision = DraftDecision(request=request)
        if plan.routing is not None:
            remaining = max(0, plan.deadline - time.time())
            if remaining:
                task = await self.start(plan.routing, "routing")
                try:
                    result = await asyncio.wait_for(asyncio.shield(task), remaining)
                    if not isinstance(result, RoutingResult):
                        raise JournalConflict("ROUTING_RESULT_CONFLICT")
                    decision.routing = result
                except TimeoutError:
                    decision.deadline_expired = True
            else:
                decision.deadline_expired = True
        state = await self.apply(decision)
        if state.disposition in {"queued", "sending"}:
            delivery = OutgoingDelivery(request=request, deadline=state.deadline, force=state.immediate)
            task = await self.start(delivery, "delivery")
            if state.immediate and not state.held:
                return await asyncio.shield(task)
        return state

    async def run_delivery(self, operation):
        if not operation.force:
            await asyncio.sleep(max(0, operation.deadline - time.time()))
        while True:
            preparation = await self.prepare_delivery(operation)
            state, effect = preparation.state, preparation.effect
            if effect is not None:
                while True:
                    result = await self.execute_delivery(effect)
                    if result.state is not None:
                        state = result.state
                        if state.terminal:
                            return state
                        await asyncio.sleep(2)
                        continue
                    assert result.receipt is not None
                    await self.apply_delivery(NativeDeliveryApplication(effect=effect, receipt=result.receipt))
                    return await self.finish_delivery(effect)
            if state.terminal:
                return state
            await asyncio.sleep(2)

    def freeze(self, scope, operation_id, direction, bundle, *, task: NativeAuxiliaryTask | None = None):
        self.check(scope)
        if bundle.get("player") != self.root.civilization or bundle.get("direction", direction) != direction:
            raise JournalConflict("ROUTING_INPUT_PLAYER_CONFLICT")
        scoped = scope.model_copy(update={"activation": digest([scope.activation, operation_id, direction])})
        if task is not None:
            if task.kind != direction + "_routing":
                raise JournalConflict("ROUTING_TASK_CONFLICT")
            if direction == "incoming":
                operation_id = "incoming-routing-" + task.key
            elif task.key != digest(operation_id):
                raise JournalConflict("ROUTING_TASK_CONFLICT")
        submission = routing_submission(operation_id, direction, bundle, self.settings.decisions_backend)
        return RoutingRun(
            scope=scoped,
            submission=self.root.store.put(scoped, submission.model_dump(mode="json")),
            policy=self.root.store.put(scoped, self.settings.model_dump()),
            request_id=submission.requestId,
            operation_id=operation_id,
            direction=direction,
            timeout_seconds=self.settings.decisions_timeout_seconds,
            task=task,
        )

    def inputs(self, request):
        self.check(request.scope)
        submission = RoutingSubmission.model_validate(self.root.store.get(request.scope, request.submission))
        if (
            self.root.store.get(request.scope, request.policy) != self.settings.model_dump()
            or submission.requestId != request.request_id
            or submission.operationId != request.operation_id
            or submission.direction != request.direction
            or request.timeout_seconds != self.settings.decisions_timeout_seconds
            or submission.bundle.get("player") != self.root.civilization
            or self.broker.backend != self.settings.decisions_backend
            or submission
            != routing_submission(
                request.operation_id, request.direction, submission.bundle, self.settings.decisions_backend
            )
        ):
            raise JournalConflict("ROUTING_BINDING_CONFLICT")
        return submission

    def result(self, request, result):
        submission = self.inputs(request)
        if (
            result.scope != request.scope
            or result.request_id != request.request_id
            or self.root.store.receipt(request.scope, "routing-result") != result.model_dump(mode="json")
        ):
            raise JournalConflict("ROUTING_RESULT_CONFLICT")
        value = self.root.store.get(request.scope, result.outcome)
        return validate_decision(
            request.direction, validate_routing_reply(submission, value, self.settings), self.settings
        )

    async def classify(self, scope, operation_id, direction, bundle, *, task: NativeAuxiliaryTask | None = None):
        request = self.freeze(scope, operation_id, direction, bundle, task=task)
        recorded = self.root.store.receipt(request.scope, "routing-result")
        if recorded is not None:
            return self.result(request, RoutingResult.model_validate(recorded))
        handle = await self.start(request, "routing")
        return self.result(request, await asyncio.shield(handle))

    async def exchange(self, request: RoutingRun) -> RoutingReply:
        submission = self.inputs(request)

        async def lookup():
            try:
                return await self.broker.lookup_route(request.request_id, timeout=min(5, request.timeout_seconds))
            except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
                return {"requestId": request.request_id, "fallback": "broker_unavailable"}

        # The broker owns dispatch intent, spending and immutable replies.
        # Lookup the exact original request before any possible submit.
        raw = await lookup()
        if raw.get("requestId") == request.request_id and raw.get("fallback") == "not_submitted":
            try:
                raw = await self.broker.route(
                    request.operation_id,
                    request.direction,
                    submission.bundle,
                    timeout=request.timeout_seconds,
                    scope=request.scope,
                    task=request.task,
                )
            except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
                raw = {"requestId": request.request_id, "fallback": "broker_unavailable"}
            checked = validate_routing_reply(submission, raw, self.settings)
            reason = checked.get("fallback")
            if isinstance(reason, str) and reason in {
                "broker_unavailable",
                "completion_unknown",
                "malformed_broker_output",
            }:
                raw = await lookup()  # One bounded reconciliation; never a new paid identity.
        result = validate_routing_reply(submission, raw, self.settings)
        # The broker decorates every reply with the principal's live aggregate
        # account. Other requests can change it between free lookups of the
        # same immutable decision. Retain per-request usage and provenance;
        # account totals remain owned by the broker's accounting ledger.
        result = {key: value for key, value in result.items() if key != "routingAccounting"}
        return RoutingReply(outcome=self.root.store.put(request.scope, result))

    async def finish(self, operation: FinishRouting) -> RoutingResult:
        request = operation.request
        submission = self.inputs(request)
        body = operation.model_dump(mode="json")
        _, recorded = self.root.store.reserve(request.scope, "routing-result", body)
        if recorded is not None:
            result = RoutingResult.model_validate(self.root.store.get(request.scope, recorded))
            if (result.scope, result.request_id, result.outcome) != (
                request.scope,
                request.request_id,
                operation.reply.outcome,
            ):
                raise JournalConflict("ROUTING_RESULT_CONFLICT")
            return result
        outcome = self.root.store.get(request.scope, operation.reply.outcome)
        if validate_routing_reply(submission, outcome, self.settings) != outcome:
            raise JournalConflict("ROUTING_OUTCOME_CONFLICT")
        result = RoutingResult(scope=request.scope, request_id=request.request_id, outcome=operation.reply.outcome)
        self.root.store.complete(request.scope, "routing-result", body, result.model_dump(mode="json"))
        if self.reply_checkpoint is not None:
            await self.reply_checkpoint("routing:submit", request)
        return result

    async def outgoing(self, request):
        self.check(request.scope)
        expected = await self.root.get("nativeOutgoing:" + request.delivery_id)
        if expected != request.model_dump(mode="json"):
            raise JournalConflict("OUTGOING_BINDING_CONFLICT")
        draft = await self.root.io.call(self.store.get, request.delivery_id, epoch=request.scope.epoch)
        if (
            draft is None
            or draft["payload"].get("epoch") != request.scope.epoch
            or draft["owner"] != self.root.civilization
        ):
            raise JournalConflict("OUTGOING_DRAFT_CONFLICT")
        return draft

    async def state(self, request, draft):
        return OutgoingState(
            result=self.root.store.put(request.scope, delivery_status(draft)),
            disposition=draft["status"],
            deadline=draft["deadline"],
            immediate=draft.get("decision", {}).get("choice") == "send_now",
            held=await self.held(request.scope),
            terminal=draft["status"] in {"sent", "cancelled", "failed", "omitted"},
        )

    async def finish_terminal_delivery(self, request, draft):
        draft = await self.outgoing(request)
        if draft["status"] not in {"sent", "failed"}:
            return
        await self.root.io.call(self.store.finish_native_delivery, request.scope.epoch, request.delivery_id)

    async def send(self, job, payload, call_id, *, immediate=False):
        root = self.root
        social = await root.gateway.read_social(job, delivered=False)
        recipients = payload.get("recipients") or ([payload["to"]] if payload.get("to") else [])
        thread = (False, tuple(sorted({root.civilization, *recipients}))) if recipients else (True, ())
        delivery_id = "delivery-" + hashlib.sha256((job.id + ":" + call_id).encode()).hexdigest()[:24]
        request = OutgoingDraft(
            scope=Scope(
                game=root.game, player=root.player, epoch=job.epoch, activation=digest(["outgoing", delivery_id])
            ),
            delivery_id=delivery_id,
            immediate=immediate or self.protected(thread, social),
        )
        async with root.gateway.dispatch:
            root.gateway.check(job)
            existing = await root.io.call(self.store.get, delivery_id, epoch=job.epoch)
            if existing is None:
                update = await root.gateway.conversation_update(job, payload)
                if update:
                    return update
            previous = await root.get("nativeOutgoing:" + delivery_id)
            if previous is not None:
                request = OutgoingDraft.model_validate(previous)
            else:
                await root.put("nativeOutgoing:" + delivery_id, request.model_dump(mode="json"))
            await root.io.call(self.store.draft, job, call_id, payload, thread, epoch=job.epoch)
        handle = await self.start(request, "outgoing")
        await asyncio.shield(handle)
        return delivery_status(await self.outgoing(request))

    async def reconcile_call(self, job, operation_id):
        """Complete the original tool acknowledgement for an already admitted draft.

        A routed send has its own later delivery job and native request identity.
        The source tool's ordinary outbox lookup therefore cannot find it. Bind
        the saved call to that exact draft before permitting batch recovery to
        acknowledge its admission; never infer another player action here.
        """
        root = self.root

        def original_call():
            with root.memory.transaction() as db:
                row = (
                    db.execute(
                        select(tables.calls).where(tables.calls.c.job == job.id, tables.calls.c.id == operation_id)
                    )
                    .mappings()
                    .one_or_none()
                )
                return dict(row) if row is not None else None

        call = await root.io.call(original_call)
        if call is None or call["name"] != "chat_send" or call["result"] is not None:
            return False
        delivery_id = "delivery-" + hashlib.sha256((job.id + ":" + operation_id).encode()).hexdigest()[:24]
        draft = await root.io.call(self.store.get, delivery_id, epoch=job.epoch)
        if draft is None:
            return False  # No admission exists; normal unexecuted-child rules apply.
        arguments = json.loads(call["arguments"])
        payload = (
            tool_model("chat_send", job.purpose).model_validate(arguments).model_dump(by_alias=True, exclude_unset=True)
        )
        if payload.get("nativeAction"):
            raise JournalConflict("OUTGOING_CALL_NATIVE_ACTION_CONFLICT")
        for key in ("intent", "delivery", "nativeAction"):
            payload.pop(key, None)
        payload.update(epoch=job.epoch, op="send")
        if not payload.get("to"):
            payload.pop("to", None)
        recipients = payload.get("recipients") or ([payload["to"]] if payload.get("to") else [])
        thread = (False, tuple(sorted({root.civilization, *recipients}))) if recipients else (True, ())
        request = OutgoingDraft.model_validate(await root.get("nativeOutgoing:" + delivery_id))
        expected_scope = Scope(
            game=root.game, player=root.player, epoch=job.epoch, activation=digest(["outgoing", delivery_id])
        )
        if (
            request.scope != expected_scope
            or request.delivery_id != delivery_id
            or draft.get("sourceJob") != job.id
            or draft["payload"] != payload
            or tuple_thread(draft["thread"]) != thread
        ):
            raise JournalConflict("OUTGOING_CALL_BINDING_CONFLICT")
        draft = await self.outgoing(request)
        if draft["status"] in {"classifying", "queued", "sending"}:
            handle = await self.start(request, "outgoing")
            if draft["status"] == "classifying":
                await asyncio.shield(handle)
            draft = await self.outgoing(request)
        await root.io.call(root.memory.call, job.id, operation_id, "chat_send", arguments, delivery_status(draft))
        return True

    async def prepare(self, request: OutgoingDraft) -> DraftPlan:
        draft = await self.outgoing(request)
        body = request.model_dump(mode="json")
        _, recorded = self.root.store.reserve(request.scope, "draft-plan", body)
        if recorded is not None:
            return DraftPlan.model_validate(self.root.store.get(request.scope, recorded))
        plan = DraftPlan(deadline=draft["deadline"])
        if draft["status"] == "classifying":
            try:
                protected = await self.protected_now(request, draft)
            except JournalConflict:
                raise
            except (OSError, ValueError, TimeoutError, aiohttp.ClientError):
                protected = True
            if request.immediate or protected:
                plan.decision = {
                    **fallback("outgoing", "immediate" if request.immediate else "protected"),
                    "policyVersion": POLICY_VERSION,
                }
            else:
                bundle = await self.root.io.call(
                    self.store.bundle,
                    draft["thread"],
                    [],
                    exclude_delivery=request.delivery_id,
                    epoch=request.scope.epoch,
                )
                bundle["proposedMessage"] = {"author": self.root.civilization, "text": draft["payload"]["text"]}
                plan.routing = self.freeze(
                    request.scope,
                    request.delivery_id,
                    "outgoing",
                    bundle,
                    task=NativeAuxiliaryTask(kind="outgoing_routing", key=digest(request.delivery_id)),
                )
        self.root.store.complete(request.scope, "draft-plan", body, plan.model_dump(mode="json"))
        return plan

    async def apply(self, operation: DraftDecision) -> OutgoingState:
        request = operation.request
        draft = await self.outgoing(request)
        plan = DraftPlan.model_validate(self.root.store.receipt(request.scope, "draft-plan"))
        if draft["status"] == "classifying":
            if operation.deadline_expired:
                if time.time() + 0.05 < draft["deadline"]:
                    raise JournalConflict("OUTGOING_DEADLINE_NOT_REACHED")
                decision = {**fallback("outgoing", "routing_deadline"), "policyVersion": POLICY_VERSION}
            elif plan.routing is not None:
                if operation.routing is None:
                    raise JournalConflict("OUTGOING_CLASSIFICATION_REQUIRED")
                decision = self.result(plan.routing, operation.routing)
            else:
                decision = plan.decision
            if decision is None:
                raise JournalConflict("OUTGOING_DECISION_REQUIRED")
            if decision["choice"] != "send_now" and time.time() < draft["deadline"]:
                try:
                    if await self.protected_now(request, draft):
                        decision = {**fallback("outgoing", "protected"), "policyVersion": POLICY_VERSION}
                except JournalConflict:
                    raise
                except (OSError, ValueError, TimeoutError, aiohttp.ClientError):
                    decision = {**fallback("outgoing", "protection_refresh_failed"), "policyVersion": POLICY_VERSION}
            if time.time() >= draft["deadline"]:
                decision = {**fallback("outgoing", "routing_deadline"), "policyVersion": POLICY_VERSION}
            draft = await self.root.io.call(
                self.store.update,
                request.scope.epoch,
                request.delivery_id,
                only_status="classifying",
                status="omitted" if decision["choice"] == "omit" else "queued",
                decision=decision,
            )
            if decision["choice"] == "send_now":
                await self.urgent_thread(request.scope.epoch, draft["thread"], exclude=request.delivery_id)
        return await self.state(request, draft)

    def sign_delivery(self, effect):
        return hmac.digest(
            self.seal_key, canonical(effect.model_dump(mode="json", exclude={"seal"})).encode(), "sha256"
        ).hex()

    async def verify_delivery(self, effect):
        if not hmac.compare_digest(effect.seal, self.sign_delivery(effect)):
            raise JournalConflict("OUTGOING_NATIVE_BINDING_CONFLICT")
        request = effect.operation.request
        draft = await self.outgoing(request)
        expected = {
            **draft["payload"],
            "epoch": request.scope.epoch,
            "requestId": delivery_request_id(request.scope.epoch, request.delivery_id),
        }
        if effect.prepared.payload != expected:
            raise JournalConflict("OUTGOING_NATIVE_BINDING_CONFLICT")
        return draft

    async def recovery_admission(self, owner: DeliveryAdmission, effect: FrozenNativeDelivery) -> bool:
        """Read actual admission without locks held by a delayed execute RPC.

        The trusted recovery caller separately verifies the permanent Root latch
        and native barrier. A frozen delivery candidate alone authorizes no host
        cancellation. This read publishes no receipt or domain state.
        """
        if not hmac.compare_digest(effect.seal, self.sign_delivery(effect)):
            raise JournalConflict("OUTGOING_NATIVE_BINDING_CONFLICT")
        request = effect.operation.request
        self.check(request.scope)
        if owner.task_id not in {request.scope.identity("task", name) for name in ("delivery", "delivery-force")}:
            raise JournalConflict("OUTGOING_ADMISSION_CONFLICT")

        def inspect_admission():
            with self.root.memory.transaction() as db:
                drafts = self.store._drafts(db, epoch=request.scope.epoch)
                draft = next((draft for draft in drafts if draft["deliveryId"] == request.delivery_id), None)
                if draft is None:
                    raise JournalConflict("OUTGOING_DRAFT_CONFLICT")
                expected = {
                    **draft["payload"],
                    "epoch": request.scope.epoch,
                    "requestId": delivery_request_id(request.scope.epoch, request.delivery_id),
                }
                if effect.prepared.payload != expected:
                    raise JournalConflict("OUTGOING_NATIVE_BINDING_CONFLICT")
                if draft["status"] in {"sent", "failed"}:
                    return False  # Preserve terminal facts; the owning task finishes its bookkeeping.
                if draft["status"] != "sending":
                    if draft.get("admission") is not None or draft.get("requestId") is not None:
                        raise JournalConflict("OUTGOING_ADMISSION_CONFLICT")
                    return False  # A frozen queued candidate has never been admitted.
                try:
                    actual = DeliveryAdmission.model_validate(draft.get("admission"))
                except ValidationError:
                    raise JournalConflict("OUTGOING_ADMISSION_REQUIRED") from None
                job = db.execute(tables.jobs.select().where(tables.jobs.c.id == effect.job)).mappings().one_or_none()
                if (
                    actual != owner
                    or draft.get("requestId") != expected["requestId"]
                    or job is None
                    or job["epoch"] != request.scope.epoch
                    or job["purpose"] != "delivery"
                    or job["status"] not in {"RUNNING", "INTERRUPTED"}
                ):
                    raise JournalConflict("OUTGOING_ADMISSION_CONFLICT")
                return True

        return await self.root.io.call(inspect_admission)

    async def delivery_blocked(self, operation, draft):
        request = operation.request
        state = await self.state(request, draft)
        if draft["status"] in {"sent", "failed", "cancelled"} or draft["status"] == "omitted" and not operation.force:
            return state
        # An explicit later override upgrades an already-running ordered force
        # task without changing its native identity or frozen text.
        state.terminal = False
        if await self.held(request.scope):
            return state
        if operation.ordered and draft.get("override") != "immediate":
            earlier = [
                other
                for other in await self.root.io.call(self.store.drafts, epoch=request.scope.epoch)
                if other["createdAt"] < draft["createdAt"]
                and tuple_thread(other["thread"]) == tuple_thread(draft["thread"])
                and other["status"] in {"queued", "sending"}
            ]
            if earlier:
                return state
        return None

    async def prepare_delivery(self, operation: OutgoingDelivery) -> NativeDeliveryPreparation:
        async with self.root.gateway.dispatch:
            request = operation.request
            draft = await self.outgoing(request)
            if draft["status"] in {"sent", "failed"}:
                await self.finish_terminal_delivery(request, draft)
            blocked = await self.delivery_blocked(operation, draft)
            if blocked is not None:
                return NativeDeliveryPreparation(state=blocked)
            identifier = delivery_request_id(request.scope.epoch, request.delivery_id)
            payload = {**draft["payload"], "epoch": request.scope.epoch, "requestId": identifier}
            prepared = PreparedHostEffect(
                name="chat_send",
                arguments={key: value for key, value in payload.items() if key not in {"op", "epoch", "requestId"}},
                transport="player_communication",
                payload=payload,
            )
            effect = FrozenNativeDelivery(
                operation=operation, job="dispatch-" + identifier, prepared=prepared, seal="0" * 64
            )
            effect.seal = self.sign_delivery(effect)
            return NativeDeliveryPreparation(state=await self.state(request, draft), effect=effect)

    async def execute_delivery(self, effect: FrozenNativeDelivery) -> NativeDeliveryExecution:
        root, request = self.root, effect.operation.request
        async with root.gateway.dispatch:
            await self.verify_delivery(effect)
            connection = await self.connection(request.scope.epoch)
            payload = effect.prepared.payload
            assert payload is not None
            root.gateway.in_flight += 1
            try:

                async def lookup():
                    return await connection.tool(
                        "player_communication",
                        {"op": "receipt", "epoch": request.scope.epoch, "requestId": payload["requestId"]},
                    )

                async def enact():
                    draft = await self.verify_delivery(effect)
                    if draft["status"] in {"sent", "failed"}:
                        raise JournalConflict("OUTGOING_NATIVE_RECEIPT_MISSING")
                    blocked = await self.delivery_blocked(effect.operation, draft)
                    if blocked is not None:
                        return NativeDeliveryExecution(state=blocked)
                    owner = self.admission.get()
                    if owner is None:
                        raise JournalConflict("LOCAL_DELIVERY_TASK_REQUIRED")
                    body = effect.model_dump(mode="json")
                    key = "nativeDelivery:" + payload["requestId"]
                    previous = await root.get(key)
                    if previous is not None and previous != body:
                        raise JournalConflict("OUTGOING_NATIVE_BINDING_CONFLICT")
                    await root.put(key, body)
                    await root.io.call(
                        self.store.begin_native_delivery,
                        request.scope.epoch,
                        request.delivery_id,
                        effect.job,
                        payload,
                        owner.model_dump(mode="json"),
                    )
                    receipt = await connection.tool("player_communication", payload)
                    if self.delivery_checkpoint is not None:
                        await self.delivery_checkpoint(request, receipt)
                    return NativeDeliveryExecution(receipt=receipt)

                result = await execute_prepared_command(lookup, enact, expected=payload)
                return (
                    result if isinstance(result, NativeDeliveryExecution) else NativeDeliveryExecution(receipt=result)
                )
            finally:
                root.gateway.in_flight -= 1

    def validate_delivery_receipt(self, effect, receipt):
        payload = effect.prepared.payload
        assert payload is not None
        validate_native_delivery_receipt(payload, receipt, author=self.root.civilization)

    async def apply_delivery(self, operation: NativeDeliveryApplication, *, receipt_only=False) -> OutgoingState:
        effect, receipt = operation.effect, operation.receipt
        request, root = effect.operation.request, self.root
        async with root.gateway.dispatch:
            await self.verify_delivery(effect)
            self.validate_delivery_receipt(effect, receipt)
            payload = effect.prepared.payload
            assert payload is not None
            await root.io.call(
                self.store.resume_native_delivery, request.scope.epoch, request.delivery_id, effect.job, payload
            )
            await root.io.call(
                root.memory.record_native_outcome,
                effect.job,
                {
                    "operationId": payload["requestId"],
                    "transport": "player_communication",
                    "payload": payload,
                    "intent": None,
                    "initiativeDecisions": [],
                    "turn": receipt.get("message", {}).get("turn"),
                },
                receipt,
                receipt_only=receipt_only,
            )
            draft = await root.io.call(
                self.store.apply_native_delivery, request.scope.epoch, request.delivery_id, effect.job, payload, receipt
            )
            return await self.state(request, draft)

    async def finish_delivery(self, effect: FrozenNativeDelivery) -> OutgoingState:
        async with self.root.gateway.dispatch:
            await self.verify_delivery(effect)
            request = effect.operation.request
            await self.root.io.call(self.store.finish_native_delivery, request.scope.epoch, request.delivery_id)
            return await self.state(request, await self.outgoing(request))

    async def force(self, request, draft, *, wait, ordered=False):
        operation = OutgoingDelivery(request=request, deadline=draft["deadline"], force=True, ordered=ordered)
        task = await self.start(operation, "delivery-force")
        if wait and not await self.held(request.scope):
            await asyncio.shield(task)
        return task

    async def settle_sending(self, scope, *, timeout_seconds=10):
        """Join only admitted sends after the caller has fenced further dispatch."""
        self.check(scope)
        if not await self.held(scope):
            raise JournalConflict("OUTGOING_SETTLEMENT_REQUIRES_HOLD")
        sending = [
            draft
            for draft in await self.root.io.call(self.store.drafts, epoch=scope.epoch)
            if draft["status"] in {"sending", "sent", "failed"}
        ]

        async def settle(draft):
            request = OutgoingDraft.model_validate(await self.root.get("nativeOutgoing:" + draft["deliveryId"]))
            await self.outgoing(request)
            if draft["status"] in {"sent", "failed"}:
                await self.finish_terminal_delivery(request, draft)
                return
            try:
                owner = DeliveryAdmission.model_validate(draft.get("admission"))
            except ValidationError:
                raise JournalConflict("OUTGOING_ADMISSION_REQUIRED") from None
            if owner.task_id not in {request.scope.identity("task", key) for key in ("delivery", "delivery-force")}:
                raise JournalConflict("OUTGOING_ADMISSION_CONFLICT")
            effect = FrozenNativeDelivery.model_validate(await self.root.get("nativeDelivery:" + draft["requestId"]))
            await self.verify_delivery(effect)
            async with self.root.gateway.dispatch, asyncio.timeout(5):
                receipt = await (await self.connection(scope.epoch)).tool(
                    "player_communication", {"op": "receipt", "epoch": scope.epoch, "requestId": draft["requestId"]}
                )
            if "error" in receipt:
                raise JournalConflict("OUTGOING_SETTLEMENT_UNRESOLVED")
            await self.apply_delivery(NativeDeliveryApplication(effect=effect, receipt=receipt))
            await self.finish_delivery(effect)

        try:
            async with asyncio.timeout(timeout_seconds):
                results = await asyncio.gather(*(settle(draft) for draft in sending), return_exceptions=True)
        except TimeoutError:
            # A timeout proves no quiescence; accepted native effects retain their exact IDs.
            raise JournalConflict("OUTGOING_SETTLEMENT_UNRESOLVED") from None
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def drain(self, scope):
        await self.set_hold(scope, False)
        for draft in await self.root.io.call(self.store.drafts, epoch=scope.epoch):
            if draft["status"] in {"sent", "failed"}:
                request = OutgoingDraft.model_validate(await self.root.get("nativeOutgoing:" + draft["deliveryId"]))
                if request.scope.epoch == scope.epoch:
                    await self.finish_terminal_delivery(request, draft)
                continue
            if draft["status"] in {"sent", "cancelled", "failed", "omitted"} and draft.get("override") != "immediate":
                continue
            request = OutgoingDraft.model_validate(await self.root.get("nativeOutgoing:" + draft["deliveryId"]))
            if request.scope.epoch != scope.epoch:
                continue
            handle = await self.start(request, "outgoing")
            await asyncio.shield(handle)
            latest = await self.outgoing(request)
            if latest["status"] in {"sent", "cancelled", "failed"} or (
                latest["status"] == "omitted" and latest.get("override") != "immediate"
            ):
                continue
            if latest.get("override") == "immediate" or latest.get("urgent"):
                await self.force(request, latest, wait=True, ordered=latest.get("override") != "immediate")
            else:
                operation = OutgoingDelivery(request=request, deadline=latest["deadline"])
                await asyncio.shield(await self.start(operation, "delivery"))

    async def control(self, job, arguments):
        draft = await self.root.io.call(self.store.get, arguments["deliveryId"], epoch=job.epoch)
        if draft is None:
            return {"error": "UNKNOWN_DELIVERY", "applied": False}
        request = OutgoingDraft.model_validate(await self.root.get("nativeOutgoing:" + draft["deliveryId"]))
        self.check(request.scope)
        if request.scope.epoch != job.epoch:
            raise JournalConflict("OUTGOING_CONTROL_EPOCH_CONFLICT")
        action = arguments.get("action", "status")
        async with self.root.gateway.dispatch:
            self.root.gateway.check(job)
            draft = await self.outgoing(request)
            if action == "cancel" and draft["status"] in {"classifying", "queued", "omitted"}:
                draft = await self.root.io.call(
                    self.store.update, job.epoch, draft["deliveryId"], status="cancelled", override="cancel"
                )
            elif action == "immediate" and draft["status"] not in {"sent", "cancelled", "failed"}:
                draft = await self.root.io.call(self.store.update, job.epoch, draft["deliveryId"], override="immediate")
        if action == "immediate" and draft["status"] not in {"sent", "cancelled", "failed"}:
            await self.force(request, draft, wait=True)
        return delivery_status(await self.outgoing(request))

    async def urgent_thread(self, epoch, thread, *, exclude=None):
        for draft in await self.root.io.call(self.store.drafts, epoch=epoch):
            if (
                tuple_thread(draft["thread"]) != tuple_thread(thread)
                or draft["status"] != "queued"
                or draft["deliveryId"] == exclude
            ):
                continue
            request = OutgoingDraft.model_validate(await self.root.get("nativeOutgoing:" + draft["deliveryId"]))
            self.check(request.scope)
            if request.scope.epoch != epoch:
                raise JournalConflict("OUTGOING_URGENT_EPOCH_CONFLICT")
            draft = await self.root.io.call(
                self.store.update, epoch, draft["deliveryId"], deadline=min(time.time(), draft["deadline"]), urgent=True
            )
            await self.force(request, draft, wait=False, ordered=True)

    async def recover(self, scope):
        self.check(scope)
        self.suspended = False
        self.failure = None
        for draft in await self.root.io.call(self.store.drafts, epoch=scope.epoch):
            if draft["status"] in {"sent", "failed"}:
                request = OutgoingDraft.model_validate(await self.root.get("nativeOutgoing:" + draft["deliveryId"]))
                if request.scope.epoch == scope.epoch:
                    await self.finish_terminal_delivery(request, draft)
                continue
            if draft["status"] == "cancelled" or (
                draft["status"] == "omitted" and draft.get("override") != "immediate"
            ):
                continue
            request = OutgoingDraft.model_validate(await self.root.get("nativeOutgoing:" + draft["deliveryId"]))
            self.check(request.scope)
            if request.scope.epoch != scope.epoch:
                continue
            if draft.get("override") == "immediate" or draft.get("urgent"):
                await self.force(request, draft, wait=False, ordered=draft.get("override") != "immediate")
            elif draft["status"] in {"classifying", "queued", "sending"}:
                await self.start(request, "outgoing")

    async def retire_pending(self, scope):
        """Resolve accepted sends under the native barrier; cancel unsent drafts."""
        self.check(scope)
        if not await self.held(scope):
            raise JournalConflict("OUTGOING_SETTLEMENT_REQUIRES_HOLD")
        await self.close()
        for draft in await self.root.io.call(self.store.drafts, epoch=scope.epoch):
            request = OutgoingDraft.model_validate(await self.root.get("nativeOutgoing:" + draft["deliveryId"]))
            await self.outgoing(request)
            if draft["status"] in {"sent", "failed"}:
                await self.finish_terminal_delivery(request, draft)
                continue
            if draft["status"] in {"cancelled", "omitted"}:
                continue
            if draft["status"] != "sending":
                await self.root.io.call(
                    self.store.update, scope.epoch, request.delivery_id, status="cancelled", override="cancel"
                )
                continue
            owner = DeliveryAdmission.model_validate(draft.get("admission"))
            if owner.task_id not in {request.scope.identity("task", key) for key in ("delivery", "delivery-force")}:
                raise JournalConflict("OUTGOING_ADMISSION_CONFLICT")
            effect = FrozenNativeDelivery.model_validate(await self.root.get("nativeDelivery:" + draft["requestId"]))
            await self.verify_delivery(effect)
            payload = effect.prepared.payload
            if payload is None:
                raise JournalConflict("OUTGOING_NATIVE_BINDING_CONFLICT")
            response = await self.root.op(
                "resolve_request",
                leaseId=self.root.game_state["lease"],
                epoch=scope.epoch,
                civilization=self.root.civilization,
                transport="player_communication",
                request=payload,
            )
            receipt = response.get("receipt")
            self.validate_delivery_receipt(effect, receipt)
            await self.apply_delivery(NativeDeliveryApplication(effect=effect, receipt=receipt), receipt_only=True)
            await self.finish_delivery(effect)

    async def statuses(self):
        with self.root.memory.transaction() as db:
            epoch = self.root.memory._meta(db, "epoch")
        if epoch is None:
            raise JournalConflict("ROUTING_EPOCH_REQUIRED")
        return [
            delivery_status(draft)
            for draft in await self.root.io.call(self.store.drafts, epoch=epoch)
            if draft["status"] not in {"sent", "cancelled", "failed"}
        ]
