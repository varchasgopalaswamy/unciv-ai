"""Round capture drains tool writes, while model work can continue across rounds."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent import service_recovery
from civ_agent.agent_service import AgentService
from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from tests.test_proposal_gateway import Host
from tests.test_structured_memory import create, setup


@asynccontextmanager
async def reflecting_service(tmp_path):
    memory = CivilizationState(tmp_path / "civilization", "Rome")
    io = ServiceIO()
    gateway = ProposalGateway(memory, io, tmp_path / "workspace")
    service = SimpleNamespace(
        memory=memory,
        io=io,
        gateway=gateway,
        state_dir=tmp_path,
        civilization="Rome",
        match={"epoch": "g1"},
        chat=SimpleNamespace(pending=asyncio.Event(), reconcile=AsyncMock()),
    )
    job = await gateway.open("game", "gameplay", "g1", Host(), {}, 0)
    job.memory_after_handoff = True
    await gateway.call(job, "player_turn", {}, "read")
    await gateway.call(job, "player_command", {"kind": "END_TURN"}, "end")
    assert job.gameplay_ended.is_set()
    try:
        yield service, job
    finally:
        await service_recovery.barrier(service, False)
        await gateway.close(job, True)
        await io.close()


def note(text):
    return {"changes": [{"op": "create", "content": {"type": "note", "text": text}}]}


def test_model_memory_call_waits_for_checkpoint_without_an_error_or_retry(tmp_path):
    async def run():
        async with reflecting_service(tmp_path) as (service, job):
            assert (await service_recovery.barrier(service, True))["ready"]
            saved = await service_recovery.capture(service, "round-1")
            writing = asyncio.create_task(service.gateway.call(job, "memory_update", note("After capture"), "note"))
            await asyncio.sleep(0)
            assert not writing.done() and service.gateway.calls_in_flight == 0
            await service_recovery.barrier(service, False)
            # A new hold may start before a waiter from the old hold resumes.
            service.gateway.request_checkpoint()
            await asyncio.sleep(0)
            assert not writing.done() and service.gateway.calls_in_flight == 0
            assert (await service_recovery.barrier(service, True))["ready"]
            await service_recovery.barrier(service, False)
            result = await asyncio.wait_for(writing, 3)
            assert result["changes"][0]["accepted"]
            assert await service.gateway.call(job, "memory_update", note("After capture"), "note") == result
            checkpoint = service.memory.verify(service_recovery.checkpoint_path(service, "round-1"), saved["sha256"])
            assert checkpoint["jobs"][0]["status"] == "RUNNING"
            assert json.loads(checkpoint["jobs"][0]["snapshot"])["memoryOnly"]
            assert "After capture" not in json.dumps(checkpoint)
            assert len(service.memory.requests_for_job(job.id)) == 1  # END_TURN was never replayed.

    asyncio.run(run())


def test_checkpoint_drains_an_accepted_memory_write_before_freezing_state(tmp_path):
    async def run():
        async with reflecting_service(tmp_path) as (service, job):
            entered, release = threading.Event(), threading.Event()
            original = service.memory.patch

            def slow_write(*args):
                entered.set()
                assert release.wait(5)
                return original(*args)

            with patch.object(service.memory, "patch", slow_write):
                writing = asyncio.create_task(
                    service.gateway.call(job, "memory_update", note("Before capture"), "note")
                )
                try:
                    assert await asyncio.to_thread(entered.wait, 3)
                    assert not (await service_recovery.barrier(service, True))["ready"]
                    assert not service.gateway.paused
                    with pytest.raises(ValueError, match="BARRIER_REQUIRED"):
                        await service_recovery.capture(service, "too-early")
                finally:
                    release.set()
                assert (await asyncio.wait_for(writing, 3))["changes"][0]["accepted"]
            assert (await service_recovery.barrier(service, True))["ready"]
            saved = await service_recovery.capture(service, "round-1")
            checkpoint = service.memory.verify(service_recovery.checkpoint_path(service, "round-1"), saved["sha256"])
            assert any(key.startswith("mem") for key, version in checkpoint["heads"])

    asyncio.run(run())


def test_late_job_completion_is_deferred_until_checkpoint_release(tmp_path):
    async def run():
        async with reflecting_service(tmp_path) as (service, job):
            await service_recovery.barrier(service, True)
            await service.gateway.close(job, True)
            saved = await service_recovery.capture(service, "round-1")
            checkpoint = service.memory.verify(service_recovery.checkpoint_path(service, "round-1"), saved["sha256"])
            assert checkpoint["jobs"][0]["status"] == "RUNNING"
            await service_recovery.barrier(service, False)
            with service.memory.transaction() as db:
                assert (
                    db.execute(select(tables.jobs.c.status).where(tables.jobs.c.id == job.id)).scalar_one()
                    == "COMPLETED"
                )

    asyncio.run(run())


def test_service_restore_regenerates_history_and_rejects_a_waiting_old_job_write(tmp_path):
    async def run():
        service = AgentService(
            "Rome", tmp_path / "workspace", tmp_path / "service", "S" * 43, "https://localhost:8765/mcp", None
        )
        # Exercise recovery and gateway isolation without starting a model or an
        # activation's separate history/accounting lifecycle.
        service.gateway.history = None
        service.gateway.accounting = None
        service.match = {"epoch": "g1"}
        try:
            job = await service.gateway.open("game", "gameplay", "g1", Host(), {}, 0)
            job.memory_after_handoff = True
            await service.gateway.call(job, "player_turn", {}, "read")
            await service.gateway.call(job, "player_command", {"kind": "END_TURN"}, "end")
            await service.gateway.call(job, "memory_update", note("Before capture"), "before")
            await service_recovery.barrier(service, True)
            saved = await service_recovery.capture(service, "round-1")
            await service_recovery.barrier(service, False)
            await service.gateway.call(job, "memory_update", note("ABANDONED FUTURE"), "after")
            await service_recovery.barrier(service, True)
            waiting = asyncio.create_task(service.gateway.call(job, "memory_update", note("Late old write"), "late"))
            await asyncio.sleep(0)
            assert not waiting.done()
            result = await service_recovery.restore(
                service, {"id": saved["id"], "sha256": saved["sha256"], "epoch": "g2"}
            )
            assert result["interruptedMemoryUpdates"] == [{"job": "game", "gameTurn": 0}]
            await service_recovery.barrier(service, False)
            with pytest.raises(StateConflict, match="JOB_PAUSED_OR_EXPIRED"):
                await asyncio.wait_for(waiting, 3)
            summary = (service.workspace / "turn_0/summary.json").read_text()
            assert "Before capture" in summary and "ABANDONED FUTURE" not in summary and "Late old write" not in summary
            assert "interrupted by checkpoint restoration" in summary
            assert len(json.loads(summary)["actions"]) == 1
            fresh = await service.gateway.open("new", "gameplay", "g2", Host(), {}, 1)
            assert "ABANDONED FUTURE" not in service.memory.context(fresh.memory)
            await service.gateway.close(fresh, True)
        finally:
            await service.close()

    asyncio.run(run())


@pytest.mark.parametrize("complete_after_capture", [False, True])
def test_restore_keeps_captured_memory_and_receipts_and_marks_unfinished_reflection(tmp_path, complete_after_capture):
    memory = setup(tmp_path)
    memory.call("game", "end", "player_command", {"kind": "END_TURN"})
    request = memory.queue("game", "player_command", {"kind": "END_TURN"}, {}, "end")
    memory.receipt(request["id"], {"kind": "END_TURN", "isYourTurn": False})
    memory.begin_memory_phase("game")
    key = create(memory, {"type": "note", "text": "Committed before capture"})
    saved = tmp_path / "round.json"
    checksum = memory.backup(saved)
    memory.patch("game", [{"op": "revise", "id": key, "content": {"type": "note", "text": "ABANDONED FUTURE"}}])
    memory.finish_job("game", complete_after_capture)
    # Restore is repeatable, including after a completion in the abandoned timeline.
    for _ in range(2):
        assert memory.restore(saved, checksum, "restored") == [{"job": "game", "gameTurn": 4}]
        with memory.transaction() as db:
            assert (
                db.execute(select(tables.jobs.c.status).where(tables.jobs.c.id == "game")).scalar_one() == "INTERRUPTED"
            )
        summary = memory.summary(4)
        assert len(summary["actions"]) == 1 and summary["actions"][0]["outcome"] == "confirmed"
        assert "ABANDONED FUTURE" not in json.dumps(summary)
        assert "Committed before capture" in json.dumps(summary)
        assert any("interrupted by checkpoint restoration" in line for line in summary["unresolved"])
    memory.pause(False)
    memory.start_job("next", "gameplay", {}, game_turn=5)
    actions = memory.history(4, job="next")["actions"]
    assert isinstance(actions, list)
    assert len(actions) == 1
    assert actions[0]["command"] == summary["actions"][0]["command"]
    assert actions[0]["receipt"] == summary["actions"][0]["receipt"]
    with pytest.raises(StateConflict):
        memory.patch("game", [{"op": "create", "content": {"type": "note", "text": "Late old write"}}])
    memory.export(tmp_path / "workspace")
    assert "ABANDONED FUTURE" not in (tmp_path / "workspace/turn_4/summary.json").read_text()
