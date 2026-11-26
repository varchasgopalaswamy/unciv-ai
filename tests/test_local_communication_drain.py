"""Communication holds tolerate permit settlement without cancelling inference."""

import asyncio
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest

from civ_agent.orchestration.contracts import GatewayJob, Scope
from civ_agent.orchestration.job_contracts import HostUsage, JobTelemetry, NativeJob, NativeJobReport
from civ_agent.orchestration.store import JournalConflict
from civ_agent.runtime.player import LocalPlayer, Permit, PermitState


def owner(name: str, purpose: Literal["communication", "gameplay"] = "communication"):
    return NativeJob(
        scope=Scope(game="game", player="p1", epoch="epoch", activation=name),
        job=name,
        purpose=purpose,
    )


def fixture_player():
    player: Any = object.__new__(LocalPlayer)
    player.root = SimpleNamespace(communication=SimpleNamespace(set_held=AsyncMock()))
    player.state = SimpleNamespace(role=AsyncMock())
    player.permits = {}
    player.communication_gate = asyncio.Event()
    player.communication_gate.set()
    player.communication_idle = asyncio.Event()
    player.communication_idle.set()
    player.communication_active = 0
    player._caller = ContextVar("drain_test_caller", default=None)
    player._request_permits = {}
    player.admission = SimpleNamespace(
        claim=AsyncMock(return_value=SimpleNamespace(granted=True)),
        report=AsyncMock(return_value=SimpleNamespace(granted=True)),
        release=AsyncMock(return_value=SimpleNamespace(granted=True)),
    )
    return player


def test_hold_acknowledges_original_pending_release_while_it_removes_current_permit():
    async def run():
        player = fixture_player()
        permit = Permit(owner("communication"), "test", lifecycle=PermitState.RELEASE_UNCERTAIN)
        pending = NativeJobReport(
            job=permit.job,
            usage=HostUsage(),
            telemetry=JobTelemetry(
                sequence=3,
                phase="COMPLETED",
                startedAtMs=0,
                endedAtMs=1,
                elapsedMs=1,
                usageReported=True,
                usageComplete=True,
            ),
        )
        permit.pending_report, permit.pending_release = pending, True
        main = Permit(owner("main", "gameplay"), "test", lifecycle=PermitState.COMPUTING)
        player.permits = {permit.job.job: permit, main.job.job: main}
        await player.hold_communication()
        player.admission.release.assert_awaited_once_with(pending)
        player.admission.report.assert_not_awaited()
        player.admission.claim.assert_not_awaited()
        assert permit.released and permit.pending_report is None
        assert player.permits == {main.job.job: main} and main.computing
        assert player.state.role.await_args.args[0].native_job == permit.job.model_dump(mode="json")
        assert player.state.role.await_args.args[0].released is True
        assert not player.communication_gate.is_set()

    asyncio.run(run())


def test_hold_keeps_new_communication_queued_when_registered_during_compute_release():
    async def run():
        player = fixture_player()
        original = Permit(owner("original"), "test", lifecycle=PermitState.COMPUTING)
        queued = Permit(owner("queued"), "test")
        entered, acknowledge = asyncio.Event(), asyncio.Event()

        async def report(envelope):
            assert envelope.job == original.job
            entered.set()
            await acknowledge.wait()
            return SimpleNamespace(granted=True)

        player.admission.report.side_effect = report
        player.permits = {original.job.job: original}
        holding = asyncio.create_task(player.hold_communication())
        await entered.wait()
        player.permits[queued.job.job] = queued
        acknowledge.set()
        await holding
        assert original.lifecycle == PermitState.WAITING
        assert queued.lifecycle == PermitState.NEW and queued.job.job in player.permits
        player.admission.claim.assert_not_awaited()
        player.admission.release.assert_not_awaited()
        assert player.admission.report.await_count == 1
        assert not player.communication_gate.is_set()

    asyncio.run(run())


def test_hold_skips_snapshot_entry_released_concurrently_under_its_existing_lock():
    async def run():
        player = fixture_player()
        first = Permit(owner("first"), "test", lifecycle=PermitState.COMPUTING)
        second = Permit(owner("second"), "test", lifecycle=PermitState.COMPUTING)
        entered, acknowledge = asyncio.Event(), asyncio.Event()

        async def report(envelope):
            if envelope.job == first.job:
                entered.set()
                await acknowledge.wait()
            return SimpleNamespace(granted=True)

        player.admission.report.side_effect = report
        player.permits = {first.job.job: first, second.job.job: second}
        holding = asyncio.create_task(player.hold_communication())
        await entered.wait()
        await player._settle(second, release=True, completed=True)
        assert second.released and second.job.job not in player.permits
        acknowledge.set()
        await holding
        assert first.lifecycle == PermitState.WAITING
        assert player.admission.report.await_count == 2
        player.admission.release.assert_awaited_once()
        assert player.admission.release.await_args.args[0].job == second.job
        player.admission.claim.assert_not_awaited()

    asyncio.run(run())


def test_hold_preserves_running_dispatch_then_blocks_the_shared_direct_and_code_gateway_path():
    async def run():
        player = fixture_player()
        permit = Permit(owner("communication"), "test", lifecycle=PermitState.COMPUTING)
        player.permits = {permit.job.job: permit}
        player._request_permits[permit.job.job] = permit
        request = GatewayJob(scope=permit.job.scope, job=permit.job.job, purpose="communication")
        entered, acknowledge = asyncio.Event(), asyncio.Event()
        invocations = []

        async def dispatch(identity, call_id, _step, _decision):
            assert identity.scope == request.scope
            invocations.append(call_id)
            if call_id == "direct-in-flight":
                entered.set()
                await acknowledge.wait()
            return SimpleNamespace(result={"call": call_id})

        player.root.registry = dispatch
        running = asyncio.create_task(player._invoke(request, "direct-in-flight", 0, {}))
        await entered.wait()
        holding = asyncio.create_task(player.hold_communication())
        await asyncio.sleep(0)
        queued = asyncio.create_task(player._invoke(request, "code-gateway-queued", 1, {}))
        await asyncio.sleep(0)
        assert not holding.done() and not queued.done() and not running.cancelled()
        player.root.communication.set_held.assert_not_awaited()
        acknowledge.set()
        assert (await running).result == {"call": "direct-in-flight"}
        await holding
        assert invocations == ["direct-in-flight"] and not queued.done()
        assert permit.lifecycle == PermitState.WAITING
        player.root.communication.set_held.assert_awaited_once_with(True)
        await player.release_communication()
        assert (await queued).result == {"call": "code-gateway-queued"}
        assert invocations == ["direct-in-flight", "code-gateway-queued"]

    asyncio.run(run())


def test_hold_does_not_swallow_rejected_native_report_or_reacquire_compute():
    async def run():
        player = fixture_player()
        permit = Permit(owner("communication"), "test", lifecycle=PermitState.COMPUTING)
        player.permits = {permit.job.job: permit}
        player.admission.report.return_value = SimpleNamespace(granted=False)
        with pytest.raises(JournalConflict, match="LOCAL_JOB_REPORT_REJECTED"):
            await player.hold_communication()
        assert permit.pending_report is not None and not permit.released
        assert permit.lifecycle == PermitState.REPORT_UNCERTAIN
        assert player.permits[permit.job.job] is permit
        player.admission.claim.assert_not_awaited()
        player.admission.release.assert_not_awaited()
        assert not player.communication_gate.is_set()

    asyncio.run(run())


def test_cancelled_hold_keeps_gate_closed_without_native_admissions():
    async def run():
        player = fixture_player()
        player.communication_idle.clear()
        holding = asyncio.create_task(player.hold_communication())
        await asyncio.sleep(0)
        holding.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holding
        assert not player.communication_gate.is_set()
        player.root.communication.set_held.assert_not_awaited()
        player.admission.claim.assert_not_awaited()
        player.admission.report.assert_not_awaited()

    asyncio.run(run())
