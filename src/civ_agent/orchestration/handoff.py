"""Native workflow activities for trusted handoff, without a Python receipt queue."""

import hmac

from civ_agent.civilization_state import receipt_is_uncertain
from civ_agent.economy_roles import TurnRoles
from civ_agent.orchestration.contracts import GatewayJob, HandoffResult, canonical, digest
from civ_agent.orchestration.handoff_contracts import (
    FrozenNativeHandoff,
    NativeHandoffApplication,
    NativeHandoffPreparation,
    NativeHandoffRequest,
    confirmed_handoff,
)
from civ_agent.orchestration.host_effect_service import execute_prepared_command
from civ_agent.orchestration.role_state import capture_roles, restore_roles
from civ_agent.orchestration.store import JournalConflict


class NativeHandoffs:
    def __init__(self, coordinator, *, seal_key: bytes):
        self.root, self.seal_key = coordinator, seal_key

    def _sign(self, effect):
        return hmac.digest(
            self.seal_key, canonical(effect.model_dump(mode="json", exclude={"seal"})).encode(), "sha256"
        ).hex()

    async def _bound(self, request):
        root, ticket = self.root, request.ticket
        root.check_owner(ticket.scope)
        with root.store.transaction(ticket.scope):
            pass
        record = await root.get("nativeTurn:" + ticket.scope.activation)
        if record is None or record["ticket"] != ticket.model_dump(mode="json"):
            raise JournalConflict("NATIVE_TURN_UNREGISTERED")
        return record

    async def _verify(self, effect):
        if not hmac.compare_digest(effect.seal, self._sign(effect)):
            raise JournalConflict("NATIVE_HANDOFF_SEAL_CONFLICT")
        record = await self._bound(effect.request)
        if record["roleJobs"]["gameplay"] != effect.main.model_dump(mode="json"):
            raise JournalConflict("NATIVE_HANDOFF_MAIN_CONFLICT")
        return record

    def _resume_main(self, binding):
        with self.root.memory.transaction() as db:
            registered = self.root.registry._load(db, binding)
            if not registered.retired:
                self.root.registry._resume(db, registered)

    def _roles(self, record):
        root = self.root
        if (
            root.record is not None
            and root.record["activationId"] == record["activationId"]
            and root.turn_roles is not None
        ):
            roles = root.turn_roles
            roles.record = root.record = record
        else:
            roles = TurnRoles(root, record)
            if record.get("roleContext"):
                restore_roles(roles, record["roleContext"])
        return roles

    async def prepare(self, request: NativeHandoffRequest) -> NativeHandoffPreparation:
        root, ticket = self.root, request.ticket
        async with root.registry.dispatch:
            record = await self._bound(request)
            if record.get("committedHandoff"):
                await root.finalize_turn(record)
                return NativeHandoffPreparation(result=HandoffResult.model_validate(record["committedHandoff"]))
            await root.load_turn(ticket)
            record, roles = root.context()
            binding = GatewayJob.model_validate(record["roleJobs"]["gameplay"])
            await root.io.call(self._resume_main, binding)
            if not await roles.handoff_review(native=True):
                status = await root.lease()
                return NativeHandoffPreparation(
                    result=HandoffResult(committed=False, turn=status["turn"], handoff=status["handoff"])
                )
            prepared = await root.gateway.prepare_host_effect(
                roles.jobs["gameplay"],
                "player_command",
                {"kind": "END_TURN", **roles.deferral},
                request.call_id,
                host_handoff=True,
            )
            if prepared.result is not None:
                await roles.accept_handoff(prepared.result, effect_id=request.call_id)
                status = await root.lease()
                return NativeHandoffPreparation(
                    result=HandoffResult(committed=False, turn=status["turn"], handoff=status["handoff"])
                )
            effect = FrozenNativeHandoff(request=request, main=binding, prepared=prepared, seal="0" * 64)
            effect.seal = self._sign(effect)
            await root.put("localPendingHandoff:" + ticket.scope.activation, effect.model_dump(mode="json"))
            await root._save(record)
            return NativeHandoffPreparation(effect=effect)

    async def execute(self, effect: FrozenNativeHandoff) -> dict:
        root, ticket = self.root, effect.request.ticket
        async with root.registry.dispatch, root.gateway.dispatch:
            await self._verify(effect)
            payload = effect.prepared.payload
            assert payload is not None

            async def lookup():
                return await root.lookup_receipt(ticket.scope.epoch, payload)

            async def enact():
                connection = await root.load_turn(ticket)
                return await connection.tool("player_command", payload)

            result = await execute_prepared_command(lookup, enact, expected=payload)
            if root.handoff_checkpoint is not None:
                await root.handoff_checkpoint("handoff-execute", "END_TURN")
            return result

    async def lookup(self, effect: FrozenNativeHandoff) -> dict:
        """Observe the original receipt while retiring; never activate or enact."""
        await self._verify(effect)
        return await self.root.lookup_receipt(effect.request.ticket.scope.epoch, effect.prepared.payload)

    async def apply(self, request: NativeHandoffApplication) -> HandoffResult:
        root, effect, receipt = self.root, request.effect, request.receipt
        async with root.registry.dispatch:
            record = await self._verify(effect)
            ticket, payload = effect.request.ticket, effect.prepared.payload
            assert payload is not None
            if (
                receipt_is_uncertain(receipt)
                or receipt.get("error") == "REQUEST_ID_CONFLICT"
                or any(key in receipt and receipt[key] != payload[key] for key in ("requestId", "epoch", "kind"))
                or ("error" not in receipt and not confirmed_handoff(effect, receipt))
            ):
                raise JournalConflict("NATIVE_HANDOFF_RECEIPT_CONFLICT")
            status = await root.lease()
            committed = "error" not in receipt
            if status["epoch"] != ticket.scope.epoch:
                raise JournalConflict("NATIVE_HANDOFF_POSITION_CONFLICT")
            proof = {
                "attempt": effect.request.attempt,
                "main": effect.main.model_dump(mode="json"),
                "operationId": payload["requestId"],
                "requestSha256": digest(["player_command", payload]),
                "receiptSha256": digest(receipt),
            }
            if record.get("nativeHandoff") == proof:
                if record.get("committedHandoff"):
                    return HandoffResult.model_validate(record["committedHandoff"])
                return HandoffResult(committed=committed, turn=status["turn"], handoff=status["handoff"])
            if status["handoff"] != ticket.handoff + int(committed):
                raise JournalConflict("NATIVE_HANDOFF_POSITION_CONFLICT")
            await root.io.call(self._resume_main, effect.main)
            await root.io.call(
                root.memory.record_native_outcome,
                effect.main.job,
                {
                    "operationId": payload["requestId"],
                    "transport": "player_command",
                    "payload": payload,
                    "intent": effect.prepared.intent,
                    "initiativeDecisions": effect.prepared.initiative_decisions,
                    "turn": ticket.turn,
                },
                receipt,
            )
            roles = self._roles(record)
            await roles.accept_handoff(receipt, effect_id=payload["requestId"], save=False)
            record["roleContext"], record["roles"] = capture_roles(roles), roles.state()
            result = HandoffResult(committed=committed, turn=status["turn"], handoff=status["handoff"])
            record["nativeHandoff"] = proof
            if committed:
                record["committedHandoff"] = result.model_dump(mode="json")
            root.records[record["activationId"]] = record
            await root.put("nativeTurn:" + record["activationId"], record)
            if root.handoff_checkpoint is not None:
                await root.handoff_checkpoint("handoff-record", "END_TURN")
            return result

    async def finish(self, effect: FrozenNativeHandoff) -> HandoffResult:
        root = self.root
        async with root.registry.dispatch:
            record = await self._verify(effect)
            proof = record.get("nativeHandoff", {})
            payload = effect.prepared.payload
            assert payload is not None
            if proof.get("operationId") != payload["requestId"]:
                raise JournalConflict("NATIVE_HANDOFF_APPLICATION_REQUIRED")
            await root.put("localPendingHandoff:" + effect.request.ticket.scope.activation, None)
            if record.get("committedHandoff"):
                await root.finalize_turn(record)
                return HandoffResult.model_validate(record["committedHandoff"])
            status = await root.lease()
            return HandoffResult(committed=False, turn=status["turn"], handoff=status["handoff"])
