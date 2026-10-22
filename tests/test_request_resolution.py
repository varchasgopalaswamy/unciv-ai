"""An interrupted request settles only through a durable host-authored outcome."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from civ_agent import service_recovery
from civ_agent.chat_runtime import ChatRuntime
from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from tests.test_match_recovery import setup as coordinated
from tests.test_proposal_gateway import Host


@asynccontextmanager
async def abandoned(tmp_path, *, active=False, transport="player_communication", receipt=None):
    io = ServiceIO()
    memory = CivilizationState(tmp_path / "private", "Rome")
    gateway = ProposalGateway(memory, io, tmp_path / "workspace")
    host = Host()
    original = host.tool

    async def tool(name, args):
        if name == "player_receipt" or name == "player_communication" and args.get("op") == "receipt":
            return receipt if receipt is not None else {"error": "NOT_ENACTED"}
        return await original(name, args)

    host.tool = tool
    service = SimpleNamespace(
        io=io, memory=memory, gateway=gateway, civilization="Rome", state_dir=tmp_path, match=None, records={}
    )
    service.chat = ChatRuntime(service)
    service.chat.connection, service.chat.epoch = host, "g1"
    job = await gateway.open("interrupted", "communication", "g1", host, {})
    payload = (
        {"op": "trade_offer", "partner": "Greece", "give": [], "receive": []}
        if transport == "player_communication"
        else {"kind": "SET_RESEARCH_QUEUE", "revision": "rev1", "queue": ["Pottery"]}
    )
    request = memory.queue(job.id, transport, {"epoch": "g1", **payload}, "offer")
    if not active:
        await gateway.close(job, False)
    gateway.paused = True
    memory.pause(True)
    try:
        yield service, job, request
    finally:
        await io.close()
        memory.engine.dispose()


@pytest.mark.parametrize("transport", ["player_communication", "player_command"])
def test_missing_receipt_is_not_final_but_host_cancellation_releases_checkpoint(tmp_path, transport):
    async def run():
        async with abandoned(tmp_path, transport=transport) as (service, job, request):
            pending = await service_recovery.pending_requests(service)
            assert [item["id"] for item in pending["requests"]] == [request["id"]]
            assert len(service.memory.pending()) == 1
            with pytest.raises(StateConflict, match="CHECKPOINT_REQUEST_UNSETTLED"):
                await service_recovery.capture(service, "before-resolution")
            with pytest.raises(StateConflict, match="RECEIPT_UNCERTAIN"):
                await service_recovery.settle_request(
                    service, {"requestId": request["id"], "receipt": {"error": "NOT_ENACTED"}}
                )
            receipt = {"error": "REQUEST_CANCELLED"}
            result = await service_recovery.settle_request(service, {"requestId": request["id"], "receipt": receipt})
            assert result["settled"] == request["id"]
            # Retrying after a lost service response is idempotent.
            await service_recovery.settle_request(service, {"requestId": request["id"], "receipt": receipt})
            assert not service.memory.pending()
            saved = await service_recovery.capture(service, "resolved")
            path = service_recovery.checkpoint_path(service, saved["id"])
            service.memory.restore(path, saved["sha256"], "g2")
            row = service.memory.requests_for_job(job.id)[0]
            assert row["id"] == request["id"] and json.loads(row["result"]) == receipt
            with pytest.raises(StateConflict, match="RECEIPT_CONFLICT"):
                service.memory.receipt(request["id"], {"trade": {"status": "ENACTED"}})

    asyncio.run(run())


def test_cancel_during_queue_persistence_leaves_request_for_host_resolution(tmp_path, monkeypatch):
    async def run():
        async with abandoned(tmp_path, active=True) as (service, job, original_request):
            service.memory.receipt(original_request["id"], {"error": "REQUEST_CANCELLED"})
            service.gateway.paused = False
            service.memory.pause(False)
            committed, release = threading.Event(), threading.Event()
            queue = service.memory.queue

            def delayed_queue(*args, **kwargs):
                result = queue(*args, **kwargs)
                committed.set()
                assert release.wait(3)
                return result

            monkeypatch.setattr(service.memory, "queue", delayed_queue)
            action = asyncio.create_task(
                service.gateway.call(
                    job, "chat_send", {"text": "Interrupted before dispatch", "to": "Greece"}, "cancelled-send"
                )
            )
            assert await asyncio.to_thread(committed.wait, 3)
            action.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await action
            await service.gateway.close(job, False)
            service.gateway.paused = True
            service.memory.pause(True)
            pending = await service_recovery.pending_requests(service)
            assert len(pending["requests"]) == 1
            request = pending["requests"][0]
            assert request["payload"]["text"] == "Interrupted before dispatch"
            await service_recovery.settle_request(
                service, {"requestId": request["id"], "receipt": {"error": "REQUEST_CANCELLED"}}
            )
            assert not service.memory.pending()
            await service_recovery.capture(service, "after-interrupted-queue")

    asyncio.run(run())


def test_active_owner_and_missing_barrier_cannot_cancel_request(tmp_path):
    async def run():
        async with abandoned(tmp_path, active=True) as (service, _, request):
            with pytest.raises(StateConflict, match="REQUEST_OWNER_ACTIVE"):
                await service_recovery.pending_requests(service)
            with pytest.raises(StateConflict, match="REQUEST_OWNER_ACTIVE"):
                await service_recovery.settle_request(
                    service, {"requestId": request["id"], "receipt": {"error": "REQUEST_CANCELLED"}}
                )
            service.gateway.paused = False
            with pytest.raises(StateConflict, match="BARRIER_REQUIRED"):
                await service_recovery.pending_requests(service)
            assert len(service.memory.pending()) == 1

    asyncio.run(run())


def test_committed_action_is_reconciled_without_cancelling_or_replaying_it(tmp_path):
    receipt = {"trade": {"proposal": "existing", "status": "PENDING"}}

    async def run():
        async with abandoned(tmp_path, receipt=receipt) as (service, job, request):
            result = await service_recovery.pending_requests(service)
            assert result["requests"] == []
            row = service.memory.requests_for_job(job.id)[0]
            assert json.loads(row["result"]) == receipt and row["id"] == request["id"]

    asyncio.run(run())


@pytest.mark.parametrize("lookup_error", ["INTERNAL_ERROR", "COMMUNICATION_UNAVAILABLE"])
def test_unavailable_receipt_lookup_remains_unsettled(tmp_path, lookup_error):
    async def run():
        async with abandoned(tmp_path, receipt={"error": lookup_error}) as (service, _, _):
            pending = await service_recovery.pending_requests(service)
            assert len(pending["requests"]) == 1
            with pytest.raises(StateConflict, match="CHECKPOINT_REQUEST_UNSETTLED"):
                await service_recovery.capture(service, "still-uncertain")

    asyncio.run(run())


@pytest.mark.parametrize("failure_point", ["none", "host_response", "local_response"])
def test_host_resolution_precedes_both_snapshots_and_survives_lost_responses(tmp_path, monkeypatch, failure_point):
    host, players, state, coordinator = coordinated(tmp_path)
    request = {
        "id": "request1",
        "transport": "player_communication",
        "payload": {"epoch": "g1", "requestId": "request1", "op": "send", "text": "Original"},
    }
    receipt = {"error": "REQUEST_CANCELLED"}
    trace = []
    pending = True
    interrupted = False
    host_call = host.call
    player_call = players["Rome"].call

    def operator(operation, **fields):
        nonlocal interrupted
        trace.append("host:" + operation)
        if operation == "resolve_request":
            assert host.paused
            host.world["cancelled"] = fields["request"]["requestId"]
            if failure_point == "host_response" and not interrupted:
                interrupted = True
                raise OSError("Lost host resolution response after commit")
            return {"receipt": receipt}
        return host_call(operation, **fields)

    def player(operation, **fields):
        nonlocal pending, interrupted
        trace.append("player:" + operation)
        if operation == "pending_requests":
            return {"civilization": "Rome", "requests": [request] if pending else []}
        if operation == "settle_request":
            assert host.world["cancelled"] == request["id"]
            pending = False
            if failure_point == "local_response" and not interrupted:
                interrupted = True
                raise OSError("Lost local resolution response after commit")
            return {"settled": fields["requestId"]}
        if operation == "snapshot":
            assert not pending
        return player_call(operation, **fields)

    monkeypatch.setattr(host, "call", operator)
    monkeypatch.setattr(players["Rome"], "call", player)
    if failure_point != "none":
        with pytest.raises(OSError):
            coordinator.capture({"epoch": "g1", "turn": 0, "handoff": 0}, state)
        assert not (coordinator.root / "current.json").exists() and host.paused
    coordinator.capture({"epoch": "g1", "turn": 0, "handoff": 0}, state)
    saved = json.loads((coordinator.root / "current.json").read_text())
    assert saved["host"]["cancelled"] == request["id"]
    assert trace.index("host:resolve_request") < trace.index("host:snapshot") < trace.index("player:snapshot")
    assert not host.paused
