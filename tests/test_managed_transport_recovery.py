"""Native retirement resolution preserves original envelopes and bounded outputs."""

import asyncio
import copy
import threading
from unittest.mock import AsyncMock

import pytest

from civ_agent.orchestration.handoff_contracts import NativeHandoffRequest
from civ_agent.orchestration.store import JournalConflict
from civ_agent.protocol import JsonObject


class Host:
    """Synthetic serialized operator semantics, shared with late wire packets."""

    def __init__(self):
        self.status = {
            "epoch": "g1",
            "serverInstance": "host",
            "phase": "PAUSED",
            "turn": 0,
            "handoff": 0,
            "activeCivilization": "Rome",
            "activeController": "AGENT",
            "scheduled": True,
            "leaseId": "lease",
            "leaseExpiresInMs": 90000,
        }
        self.snapshot = {
            "formatVersion": 9,
            "epoch": "g1",
            "handoff": 0,
            "paused": True,
            "communicationPaused": True,
            "scheduled": True,
            "gameJson": "PRIVATE_OTHER_PLAYER_CONTENT",
        }
        self.receipts, self.calls, self.effects = {}, [], 0
        self.lock, self.lose_ack = threading.Lock(), False
        self.receipt_changes = {}

    def call(self, operation, **fields):
        self.calls.append((operation, copy.deepcopy(fields)))
        if operation == "snapshot":
            return {"status": copy.deepcopy(self.status), "checkpoint": copy.deepcopy(self.snapshot)}
        assert operation == "resolve_request"
        assert fields["leaseId"] == "lease" and fields["epoch"] == "g1" and fields["civilization"] == "Rome"
        payload = fields["request"]
        with self.lock:
            if payload["requestId"] not in self.receipts:
                receipt: JsonObject = {"error": "REQUEST_CANCELLED"}
                if fields["transport"] == "player_command":
                    receipt.update(
                        requestId=payload["requestId"],
                        epoch="g1",
                        kind="END_TURN",
                        revision="revision",
                        turn=0,
                        isYourTurn=True,
                        actionSummary="PRIVATE_NATIVE_DETAIL",
                    )
                self.receipts[payload["requestId"]] = copy.deepcopy(payload), receipt
            original, receipt = self.receipts[payload["requestId"]]
            if original != payload:
                raise JournalConflict("REQUEST_ID_CONFLICT")
            if self.lose_ack:
                self.lose_ack = False
                raise OSError("Synthetic lost operator ACK")
            return {"status": copy.deepcopy(self.status), "receipt": {**copy.deepcopy(receipt), **self.receipt_changes}}


def test_handoff_lookup_only_observes_original_native_receipt_without_activation_or_projection(tmp_path, monkeypatch):
    from civ_agent.economy_roles import TurnRoles
    from tests.test_native_handoff import Ledger, assert_no_handoff_journal, setup

    async def run():
        monkeypatch.setattr(TurnRoles, "synchronize_war", AsyncMock())
        monkeypatch.setattr(TurnRoles, "synchronize_economy", AsyncMock())
        host = Ledger()
        root, store, handoffs, ticket, _, operations = await setup(tmp_path, host)
        try:
            preparation = await handoffs.prepare(NativeHandoffRequest(ticket=ticket, attempt=0))
            effect = preparation.effect
            assert effect is not None and effect.prepared.payload is not None
            before = len(operations)
            assert await handoffs.lookup(effect) == {"error": "NOT_ENACTED"}
            payload = effect.prepared.payload
            receipt = {
                "error": "REQUEST_CANCELLED",
                "requestId": payload["requestId"],
                "epoch": "g1",
                "kind": "END_TURN",
            }
            host.requests[payload["requestId"]] = (copy.deepcopy(payload), receipt)
            assert await handoffs.lookup(effect) == receipt
            assert "activate" not in operations[before:] and host.effects == 0
            assert root.record is not None and not root.record.get("nativeHandoff")
            assert_no_handoff_journal(root)
        finally:
            await root.close()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["command", "communication"])
@pytest.mark.parametrize("damage", [None, "requestId", "epoch", "kind", "missing_command_identity", "unknown"])
def test_shared_execution_consumes_only_bound_authoritative_cancellation_without_enacting(transport, damage):
    from civ_agent.orchestration.host_effect_service import execute_prepared_command

    async def run():
        expected = {"requestId": "original", "epoch": "g1"}
        if transport == "command":
            expected["kind"] = "END_TURN"
        receipt = (
            {"error": "REQUEST_CANCELLED", **expected} if transport == "command" else {"error": "REQUEST_CANCELLED"}
        )
        if damage == "unknown":
            receipt = {"error": "UNKNOWN_QUERY"}
        elif damage == "missing_command_identity":
            receipt = {"error": "REQUEST_CANCELLED"}
        elif damage is not None:
            receipt[damage] = "changed"
        enact = AsyncMock()
        rejected = (
            damage == "unknown"
            or damage in {"requestId", "epoch", "kind"}
            or transport == "command"
            and damage == "missing_command_identity"
        )
        if rejected:
            with pytest.raises(JournalConflict):
                await execute_prepared_command(AsyncMock(return_value=receipt), enact, expected=expected)
        else:
            assert await execute_prepared_command(AsyncMock(return_value=receipt), enact, expected=expected) == receipt
        enact.assert_not_awaited()

    asyncio.run(run())


def test_original_native_command_consumes_tombstone_through_apply_and_finish_without_dispatch(tmp_path):
    from civ_agent.orchestration.host_effect_contracts import NativeCommandApplication, NativeCommandFinish
    from tests.test_native_command_service import native, step
    from tests.test_prepare_host_effect import counts

    async def run():
        async with native(tmp_path) as m:
            request = step(m.identity)
            preparation = await m.service.prepare(request)
            effect = preparation.effect
            assert effect is not None and effect.prepared.payload is not None
            payload = effect.prepared.payload
            receipt = {
                "requestId": payload["requestId"],
                "epoch": payload["epoch"],
                "kind": payload["kind"],
                "error": "REQUEST_CANCELLED",
                "turn": 0,
                "isYourTurn": True,
                "revision": payload["revision"],
                "actionSummary": "Original native request cancelled",
            }
            m.host.seen[payload["requestId"]] = copy.deepcopy(receipt)
            baseline = counts(m.memory)
            assert await m.service.execute(effect) == receipt
            applied = await m.service.apply(NativeCommandApplication(effect=effect, receipt=receipt))
            assert applied.result["error"] == "REQUEST_CANCELLED" and applied.stop == "OPERATION_REJECTED"
            await m.service.finish(NativeCommandFinish(identity=m.identity, call=request.call))
            assert m.owner.load(m.identity).pending is None and not m.host.executed
            assert counts(m.memory) == baseline

    asyncio.run(run())
