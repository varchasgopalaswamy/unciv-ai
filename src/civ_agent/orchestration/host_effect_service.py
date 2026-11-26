"""Trusted native command preparation and host-authoritative receipt recovery.

The local controller saves exact pending inputs before dispatch. Native receipts
decide whether an action committed; a restart only settles the old request.
"""

import copy
import hashlib
import hmac
import secrets
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

from civ_agent.decision_contracts import compile_decision_contract
from civ_agent.execution_windows import describe_stale, focused_stop_reason, native_effect_started, release_focus
from civ_agent.intent_handoffs import ATTEMPT_KEY, record_action_attempts
from civ_agent.orchestration.contracts import canonical, digest
from civ_agent.orchestration.gateway_execution import GatewayExecution, NativeGatewayExecutor, operation_identity
from civ_agent.orchestration.gateway_registry import PendingCall, job_key
from civ_agent.orchestration.host_effect_contracts import (
    FrozenNativeCommand,
    NativeCommandApplication,
    NativeCommandApplied,
    NativeCommandFinish,
    NativeCommandPreparation,
    NativeCommandStep,
)
from civ_agent.orchestration.store import JournalConflict
from civ_agent.proposal_gateway import public_result
from civ_agent.protocol import JsonObject


async def execute_prepared_command[T](
    lookup: Callable[[], Awaitable[JsonObject]], enact: Callable[[], Awaitable[T]], *, expected: JsonObject
) -> JsonObject | T:
    """The host serializes exact-ID sends; an unknown lookup never permits one."""
    result = await lookup()
    if result.get("error") == "NOT_ENACTED":
        return await enact()
    if result.get("error") == "REQUEST_CANCELLED":
        fields = ("requestId", "epoch", "kind")
        if (
            not isinstance(expected.get("requestId"), str)
            or not isinstance(expected.get("epoch"), str)
            or any(key in result and result[key] != expected.get(key) for key in fields)
            or "kind" in expected
            and any(result.get(key) != expected[key] for key in fields)
        ):
            raise JournalConflict("NATIVE_RECEIPT_BINDING_CONFLICT")
        # Command tombstones carry full identities. Communication's native API
        # returns only the cancellation code; its authenticated lookup is bound
        # to this exact frozen epoch/request ID by the trusted caller.
        return result
    if "error" in result:
        raise JournalConflict("NATIVE_RECEIPT_UNCERTAIN")
    return result


def native_effect_key(memory) -> bytes:
    with memory.transaction() as db:
        key = memory._meta(db, "nativeCommandSeal")
        if key is None:
            key = secrets.token_hex(32)
            memory._set(db, "nativeCommandSeal", key)
    return bytes.fromhex(key)


class NativeCommandService:
    def __init__(self, registry, *, workflow_store=None, authored_store=None, reply_checkpoint=None):
        self.registry, self.gateway = registry, registry.gateway
        self.workflow_store, self.authored_store = workflow_store, authored_store
        self.reply_checkpoint = reply_checkpoint
        self.seal_key = native_effect_key(self.gateway.memory)

    async def execute_local(self, identity, call, *, run_id: str) -> GatewayExecution:
        return await self.registry.windows.run(
            identity,
            operation_identity(identity.job, identity.scope.epoch, call.model_step, call.call_id),
            lambda: self._execute_local(identity, call, run_id=run_id),
            write=True,
            cost=max(1, len(call.decision.get("operations", []))),
            run_id=run_id,
        )

    async def _execute_local(self, identity, call, *, run_id: str) -> GatewayExecution:
        """Execute a typed batch with its exact pending input saved before dispatch."""
        parent = operation_identity(identity.job, identity.scope.epoch, call.model_step, call.call_id)
        receipts, stop = [], None
        for index, operation in enumerate(call.decision.get("operations", [])):
            step = NativeCommandStep(identity=identity, call=call, index=index, run_id=run_id)
            child = self.child(step)
            if stop:
                receipts.append({"operationId": child, "status": "unexecuted", "reason": stop})
                continue
            preparation = await self.prepare(step)
            stop = preparation.stop
            if preparation.effect is not None:
                effect = preparation.effect
                key = "localNativeEffect:" + child

                def save(value, key=key):
                    with self.gateway.memory.transaction() as db:
                        self.gateway.memory._set(db, key, value)

                await self.gateway.io.call(save, effect.model_dump(mode="json"))
                raw = await self.execute(effect)
                applied = await self.apply(NativeCommandApplication(effect=effect, receipt=raw))
                result, stop = applied.result, applied.stop
                await self.gateway.io.call(save, None)
            elif preparation.result is not None:
                result = preparation.result
            elif stop:
                receipts.append(
                    {
                        "operationId": child,
                        "status": "unexecuted",
                        "reason": stop,
                        **({"feedback": preparation.feedback} if preparation.feedback else {}),
                    }
                )
                continue
            else:
                raise JournalConflict("NATIVE_COMMAND_PREPARATION_INVALID")
            rejected = "error" in result or any(change.get("accepted") is False for change in result.get("changes", []))
            receipts.append(
                {
                    "operationId": child,
                    "tool": operation["tool"],
                    "status": "rejected" if rejected else "confirmed",
                    "result": result,
                }
            )
        await self._acknowledge_attempts(identity, call, receipts, decision_id=parent)
        await self.finish(NativeCommandFinish(identity=identity, call=call))
        registered = await self.gateway.io.call(self.registry.load, identity)
        return GatewayExecution(
            result={
                "decisionId": parent,
                "receipts": receipts,
                "unexecuted": [item["operationId"] for item in receipts if item["status"] == "unexecuted"],
            },
            state=registered.state,
        )

    async def _acknowledge_attempts(self, identity, call, receipts, *, decision_id):
        """Save advisory dispositions with the settled batch's existing context."""
        async with self.registry.dispatch:
            record, decision = await self.gateway.io.call(self._load, identity, call, live=False)
            if record.pending is None or record.retired or self.gateway.retiring_epoch(identity.scope.epoch):
                return
            data = copy.deepcopy(record.state.data)
            projected = SimpleNamespace(
                memory=data["memory"],
                observation=data["observation"],
                prior_role_observation=data["prior_role_observation"],
                purpose=identity.purpose,
            )
            record_action_attempts(projected, decision["operations"], receipts, decision_id=decision_id)
            state = record.state.model_copy(update={"data": data})
            await self.gateway.io.call(self._save_context, identity, call, state)
            cached = self.registry.jobs.get(job_key(identity))
            if cached is not None:
                cached.memory[ATTEMPT_KEY] = copy.deepcopy(projected.memory.get(ATTEMPT_KEY, []))

    def _sign(self, effect: FrozenNativeCommand):
        body = canonical(effect.model_dump(mode="json", exclude={"seal"})).encode()
        return hmac.digest(self.seal_key, body, "sha256").hex()

    def _verify(self, effect: FrozenNativeCommand):
        if not hmac.compare_digest(effect.seal, self._sign(effect)):
            raise JournalConflict("NATIVE_COMMAND_BINDING_CONFLICT")
        step, prepared = effect.step, effect.prepared
        if prepared.transport != "player_command" or prepared.payload is None or prepared.routed:
            raise JournalConflict("NATIVE_COMMAND_TRANSPORT_CONFLICT")
        child = self.child(step)
        expected = "r" + hashlib.sha256((step.identity.job + ":" + child).encode()).hexdigest()[:24]
        if (
            prepared.name != "player_command"
            or prepared.payload.get("requestId") != expected
            or prepared.payload.get("epoch") != step.identity.scope.epoch
            or (effect.state.job, effect.state.epoch, effect.state.purpose)
            != (step.identity.job, step.identity.scope.epoch, step.identity.purpose)
        ):
            raise JournalConflict("NATIVE_COMMAND_BINDING_CONFLICT")

    @staticmethod
    def parent(step):
        return operation_identity(step.identity.job, step.identity.scope.epoch, step.call.model_step, step.call.call_id)

    @classmethod
    def child(cls, step):
        return cls.parent(step) + ":" + str(step.index)

    def _load(self, identity, call, *, claim=False, live=True):
        with self.gateway.memory.transaction() as db:
            record = self.registry._load(db, identity)
            contract = compile_decision_contract(identity.purpose, record.catalogue)
            decision = contract.parse(contract.encode(call.decision))
            operations = decision.get("operations", [])
            if (
                decision.get("kind") != "act"
                or not operations
                or any(operation["tool"] != "player_command" for operation in operations)
            ):
                raise JournalConflict("NATIVE_COMMAND_BATCH_REQUIRED")
            if live:
                if record.retired:
                    raise JournalConflict("GATEWAY_JOB_RETIRED")
                self.registry._resume(db, record)
            pending = PendingCall(call=call, fingerprint=digest(decision), execution="native-command-v1")
            if record.pending is not None and record.pending != pending:
                raise JournalConflict("GATEWAY_RECONCILIATION_REQUIRED")
            if claim and record.pending is None:
                if call.model_step < record.state.delivery_step:
                    raise JournalConflict("GATEWAY_MODEL_STEP_REWOUND")
                record.pending = pending
                self.gateway.memory._set(db, job_key(identity), record.model_dump(mode="json"))
            return record, decision

    async def _job(self, record, *, receipt_only=False):
        if receipt_only:
            if self.registry.resolve_receipts is None:
                raise JournalConflict("GATEWAY_RECEIPT_RESTORATION_REQUIRED")
            return await self.registry.resolve_receipts(record)
        key = job_key(record.identity)
        job = self.registry.jobs.get(key)
        if job is None:
            job = await self.registry.resolve(record)
            self.registry.jobs[key] = job
        if self.registry.refresh is not None:
            await self.registry.refresh(job)
            job.verified = False
        self.gateway.check(job, ended_allowed=True)
        await self.gateway.verify_identity(job)
        return job

    def _executor(self, job, record, decision, step):
        executor = NativeGatewayExecutor(
            self.gateway, job, compile_decision_contract(record.identity.purpose, record.catalogue), record.state
        )
        executor._begin_step(step.call.model_step)
        executor.loop.current = {"dispatch": decision["operations"]}
        return executor

    def _save_context(self, identity, call, state, *, finish=False):
        with self.gateway.memory.transaction() as db:
            record = self.registry._load(db, identity)
            contract = compile_decision_contract(identity.purpose, record.catalogue)
            decision = contract.parse(contract.encode(call.decision))
            expected = PendingCall(call=call, fingerprint=digest(decision), execution="native-command-v1")
            if record.pending != expected:
                raise JournalConflict("NATIVE_COMMAND_ADMISSION_CHANGED")
            record.state = state
            if finish:
                record.pending = None
            self.gateway.memory._set(db, job_key(identity), record.model_dump(mode="json"))

    async def prepare(self, step: NativeCommandStep) -> NativeCommandPreparation:
        return await self.registry.windows.run(
            step.identity,
            operation_identity(step.identity.job, step.identity.scope.epoch, step.call.model_step, step.call.call_id),
            lambda: self._prepare(step),
            cost=0,
        )

    async def _prepare(self, step: NativeCommandStep) -> NativeCommandPreparation:
        if self.gateway.retiring_epoch(step.identity.scope.epoch):
            await self.gateway.io.call(self.registry.load, step.identity)
            return NativeCommandPreparation(stop="SOURCE_EPOCH_RETIRED")
        async with self.gateway.admit(), self.registry.dispatch:
            record, decision = await self.gateway.io.call(self._load, step.identity, step.call, claim=True)
            if reason := focused_stop_reason():
                return NativeCommandPreparation(stop=reason)
            operations = decision["operations"]
            if step.index >= len(operations):
                raise JournalConflict("NATIVE_COMMAND_INDEX_CONFLICT")
            job = await self._job(record)
            executor = self._executor(job, record, decision, step)
            operation = operations[step.index]
            stop = executor.loop._prerequisite(
                operation["tool"], operation["arguments"], executor.loop.delivered_access, step.index
            )
            if stop:
                return NativeCommandPreparation(
                    stop=stop,
                    feedback=describe_stale(job, executor.loop.delivered_access.get("revision"))
                    if stop == "FRESH_OBSERVATION_REQUIRED"
                    else None,
                )
            async with self.gateway.role_dispatch:
                prepared = await self.gateway.prepare_host_effect(
                    job, operation["tool"], operation["arguments"], self.child(step)
                )
            if prepared.result is not None:
                return NativeCommandPreparation(result=prepared.result, stop="OPERATION_REJECTED")
            effect = FrozenNativeCommand(step=step, prepared=prepared, state=executor.capture(), seal="0" * 64)
            if (
                prepared.intent is not None
                and any(prepared.intent.get(key) for key in ("cityPlan", "strategy"))
                and step.run_id is not None
                and self.workflow_store is not None
                and self.authored_store is not None
            ):
                from civ_agent.orchestration.authored_sources import authored_sources

                effect.sources = await authored_sources(
                    self.workflow_store, step.identity, step.run_id, step.call, self.authored_store
                )
            effect.seal = self._sign(effect)
            # Only the admitted intent/context is local. The returned exact
            # wire envelope is persisted by the native durable operation.
            await self.gateway.io.call(self._save_context, step.identity, step.call, effect.state)
            return NativeCommandPreparation(effect=effect)

    async def execute(self, effect: FrozenNativeCommand) -> JsonObject:
        step = effect.step
        return await self.registry.windows.run(
            step.identity,
            operation_identity(step.identity.job, step.identity.scope.epoch, step.call.model_step, step.call.call_id),
            lambda: self._execute(effect),
            write=True,
            cost=0,
            complete=False,
            settlement=True,
        )

    async def _execute(self, effect: FrozenNativeCommand) -> JsonObject:
        self._verify(effect)
        step, prepared = effect.step, effect.prepared
        async with (
            self.gateway.admit(settlement_epoch=step.identity.scope.epoch),
            self.registry.dispatch,
            self.gateway.dispatch,
        ):
            retiring = self.gateway.retiring_epoch(step.identity.scope.epoch)
            record, _ = await self.gateway.io.call(self._load, step.identity, step.call, live=not retiring)
            if record.pending is None:
                raise JournalConflict("NATIVE_COMMAND_ADMISSION_REQUIRED")
            job = await self._job(record, receipt_only=retiring)
            payload = copy.deepcopy(prepared.payload)
            assert payload is not None
            self.gateway.in_flight += 1
            try:

                async def lookup():
                    return await job.connection.tool("player_receipt", payload)

                async def enact():
                    if self.gateway.retiring_epoch(step.identity.scope.epoch):
                        raise JournalConflict("SOURCE_EPOCH_RETIRED")
                    # The same exact ID is safe even if the original send is
                    # still pending: the host serializes and deduplicates it.
                    self.gateway.check(job)
                    result = await job.connection.tool("player_command", payload)
                    if self.reply_checkpoint is not None:
                        await self.reply_checkpoint(effect, result)
                    return result

                native_effect_started()
                return await execute_prepared_command(lookup, enact, expected=payload)
            finally:
                self.gateway.in_flight -= 1

    async def apply(self, request: NativeCommandApplication) -> NativeCommandApplied:
        step = request.effect.step
        return await self.registry.windows.run(
            step.identity,
            operation_identity(step.identity.job, step.identity.scope.epoch, step.call.model_step, step.call.call_id),
            lambda: self._apply(request),
            write=True,
            cost=0,
            complete=False,
            settlement=True,
        )

    async def _apply(self, request: NativeCommandApplication) -> NativeCommandApplied:
        effect, receipt = request.effect, request.receipt
        self._verify(effect)
        native_effect_started()
        step, prepared = effect.step, effect.prepared
        payload = prepared.payload
        if payload is None:
            raise JournalConflict("NATIVE_COMMAND_PAYLOAD_REQUIRED")
        if "error" not in receipt and (
            receipt.get("requestId") != payload["requestId"]
            or receipt.get("epoch") != payload["epoch"]
            or receipt.get("kind") != payload["kind"]
        ):
            raise JournalConflict("NATIVE_RECEIPT_BINDING_CONFLICT")
        async with self.gateway.admit(settlement_epoch=step.identity.scope.epoch), self.registry.dispatch:
            current = await self.gateway.io.call(self.registry.load, step.identity)
            retiring = self.gateway.retiring_epoch(step.identity.scope.epoch) or current.retired
            record, decision = await self.gateway.io.call(self._load, step.identity, step.call, live=not retiring)
            if record.pending is None:
                raise JournalConflict("NATIVE_COMMAND_ADMISSION_REQUIRED")
            job = await self._job(record, receipt_only=retiring)
            executor = self._executor(job, record, decision, step)
            projected = await self.gateway.io.call(
                self.gateway.memory.record_native_outcome,
                job.id,
                {
                    "operationId": payload["requestId"],
                    "toolOperationId": self.child(step),
                    "transport": prepared.transport,
                    "payload": payload,
                    "intent": prepared.intent,
                    "initiativeDecisions": prepared.initiative_decisions,
                    "turn": effect.state.data["memory"].get("gameTurn"),
                    "pinned": self._pinned(effect),
                },
                receipt,
                receipt_only=retiring,
            )
            if retiring:
                # The original frozen effect is terminal. A paused source
                # cannot supply a fresh observation for ordinary role reducers.
                self.gateway.forget_observation(job)
                result = public_result(
                    {
                        **receipt,
                        **{key: value for key, value in projected.items() if key != "replayed"},
                        "refreshRequired": True,
                    }
                )
                await self.gateway.io.call(self._save_context, step.identity, step.call, executor.capture())
                self.registry.windows.settled(self.parent(step))
                return NativeCommandApplied(result=result, stop="SOURCE_EPOCH_RETIRED")
            async with self.gateway.role_dispatch:
                result = await self.gateway.apply_host_effect(
                    job, prepared, self.child(step), receipt, replayed=projected["replayed"]
                )
            result.update({key: value for key, value in projected.items() if key != "replayed"})
            result = public_result(result)
            executor._stage_delivery(result)
            state = executor.capture()
            await self.gateway.io.call(self._save_context, step.identity, step.call, state)
            self.registry.windows.settled(self.parent(step))
            stop = (
                "OPERATION_REJECTED"
                if "error" in result
                else "FRESH_OBSERVATION_REQUIRED"
                if result.get("stateUpdate", {}).get("kind") in {"unavailable", "turn_ended"}
                else "POSITION_CHANGED"
                if job.role_refresh_required or job.negotiation_pending.is_set()
                else None
            )
            return NativeCommandApplied(result=result, stop=stop)

    def _pinned(self, effect: FrozenNativeCommand) -> JsonObject:
        prepared = effect.prepared
        intent, pinned, _ = self.gateway.memory._action_attribution(
            effect.state.data["memory"], prepared.payload, prepared.intent, prepared.initiative_decisions
        )
        if intent is None:
            return pinned
        city = effect.sources.get(intent.get("cityPlan"))
        if (
            city is not None
            and city["category"] == "city_plan"
            and city["body"].get("city") == (prepared.payload or {}).get("target")
        ):
            pinned["cityPlan"] = {
                "key": intent["cityPlan"],
                "version": city.get("version"),
                "reason": intent["reason"],
                "content": city["body"],
                "source": city["source"],
            }
            pinned.pop("unverifiedCityPlan", None)
        strategy = effect.sources.get(intent.get("strategy"))
        if strategy is not None and strategy["category"] == "strategy":
            pinned["strategy"] = {
                "key": intent["strategy"],
                "version": strategy.get("version"),
                "assessment": strategy["body"]["assessment"],
                "content": strategy["body"],
                "source": strategy["source"],
            }
            pinned.pop("unverifiedStrategy", None)
            chosen = intent.get("decision")
            if chosen and any(row["id"] == chosen for row in strategy["body"].get("decisions", [])):
                baseline = strategy["body"].get("_decisionBaselines", {}).get(chosen, {})
                pinned["decision"] = {"strategy": intent["strategy"], "id": chosen, "baseline": baseline.get("id")}
                pinned.pop("unverifiedDecision", None)
        return pinned

    async def finish(self, request: NativeCommandFinish):
        async with self.registry.dispatch:
            record, _ = await self.gateway.io.call(self._load, request.identity, request.call, live=False)
            if record.pending is not None:
                await self.gateway.io.call(
                    self._save_context, request.identity, request.call, record.state, finish=True
                )
            release_focus("native_batch_acknowledged")
            return {"finished": True}
