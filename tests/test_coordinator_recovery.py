import asyncio
from unittest.mock import AsyncMock

import pytest

from civ_agent.operator_connection import OperatorError
from tests.test_native_handoff import Ledger, setup


def test_local_retirement_visits_each_unfinished_sqlite_job_once(tmp_path, monkeypatch):
    from civ_agent.orchestration.gateway_registry import job_key

    async def run():
        ledger = Ledger()
        root, store, _, ticket, identity, _ = await setup(tmp_path, ledger)
        ledger.status["phase"] = "PAUSED"
        try:
            # Keep another selected row after the unfinished job. Replacing
            # that job during a live SQLite scan can otherwise revisit it.
            with root.memory.transaction() as db:
                root.memory._set(db, "nativeGatewayJob:archived-tail", {"archive": True})
            original = root.memory._set
            writes = 0

            def bounded_replace(db, key, value):
                nonlocal writes
                if key == job_key(identity):
                    writes += 1
                    assert writes <= 2, "Recovery revisited an unfinished job while replacing SQLite rows"
                return original(db, key, value)

            monkeypatch.setattr(root.memory, "_set", bounded_replace)
            await root.retire_local_attempt(ticket)
            with root.memory.transaction() as db:
                retired = root.memory._meta(db, job_key(identity))
            assert writes == 2
            assert retired["retired"] and retired["state"]["data"]["closed"]
            assert not root.registry.jobs and not ledger.effects
        finally:
            await root.close()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("rejected", [False, True])
def test_heartbeat_tolerates_host_outage_but_stops_on_authorization_failure(tmp_path, monkeypatch, rejected):

    async def run():
        ledger = Ledger()
        coordinator, store, _, _, _, _ = await setup(tmp_path, ledger)
        status = ledger.status
        try:
            error = OperatorError(
                "host unavailable", None if rejected else "TRANSPORT_UNAVAILABLE", status=401 if rejected else None
            )
            coordinator.op = AsyncMock(side_effect=[error, {"status": status}, {}])
            sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
            monkeypatch.setattr(asyncio, "sleep", sleep)
            with pytest.raises(OperatorError if rejected else asyncio.CancelledError):
                await coordinator.heartbeat()
            assert coordinator.op.await_count == (1 if rejected else 3)
            assert sleep.await_count == (0 if rejected else 2)
        finally:
            await coordinator.close()
            store.close()

    asyncio.run(run())
