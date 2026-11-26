"""Retry uncertain lease renewals within their original native job lifetime."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from civ_agent.civilization_state import StateConflict
from civ_agent.orchestration.job_contracts import NativeJob
from civ_agent.runtime import player as runtime
from tests.domain_fixtures import run_async
from tests.test_local_agents import request


@pytest.fixture
def renewal(monkeypatch):
    clock = SimpleNamespace(now=20)
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: 0))
    service: Any = object.__new__(runtime.LocalPlayer)
    service.admission = SimpleNamespace(heartbeat=AsyncMock())
    req = request()
    permit = runtime.Permit(
        NativeJob(scope=req.scope, job=req.job, purpose=req.purpose),
        req.selection.model,
        lease_started=0,
        lifecycle=runtime.PermitState.WAITING,
    )
    return service, permit, clock


@pytest.mark.parametrize("error", [StateConflict("HOST_UNAVAILABLE"), TimeoutError()])
@run_async
async def test_transient_heartbeat_loss_keeps_the_original_job_and_deadline(renewal, error):
    service, permit, clock = renewal
    heartbeat = service.admission.heartbeat
    heartbeat.side_effect = [error, SimpleNamespace(granted=True)]
    await service._renew_permit(permit)
    assert permit.lease_started == 0
    clock.now = 25
    await service._renew_permit(permit)
    assert permit.lease_started == 25
    assert [call.args for call in heartbeat.await_args_list] == [(permit.job,), (permit.job,)]
    clock.now = 30
    await service._renew_permit(permit)
    assert heartbeat.await_count == 2


@run_async
async def test_persistent_heartbeat_loss_stops_before_native_expiry(renewal):
    service, permit, clock = renewal
    heartbeat = service.admission.heartbeat
    heartbeat.side_effect = StateConflict("HOST_UNAVAILABLE")
    await service._renew_permit(permit)
    clock.now = runtime.JOB_LEASE_RENEWAL_WINDOW
    with pytest.raises(RuntimeError, match="LOCAL_JOB_LEASE_LOST"):
        await service._renew_permit(permit)
    heartbeat.assert_awaited_once_with(permit.job)


@pytest.mark.parametrize("denied", [True, False])
@run_async
async def test_definitive_heartbeat_failure_surfaces_immediately(renewal, denied):
    service, permit, _ = renewal
    heartbeat = service.admission.heartbeat
    heartbeat.return_value = SimpleNamespace(granted=False)
    if not denied:
        heartbeat.side_effect = StateConflict("HOST_PROTOCOL_ERROR")
    with pytest.raises(RuntimeError if denied else StateConflict, match="LOCAL_JOB_LEASE_LOST|HOST_PROTOCOL_ERROR"):
        await service._renew_permit(permit)
    heartbeat.assert_awaited_once_with(permit.job)
