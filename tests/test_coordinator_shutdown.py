"""Coordinator shutdown owns every MCP transport created by concurrent activities."""

import asyncio
import threading
from types import SimpleNamespace
from typing import cast

import pytest

from civ_agent.host_connection import HostConnection
from civ_agent.operator_connection import Operator, OperatorError
from civ_agent.orchestration.communication import NativeCommunication
from civ_agent.orchestration.coordinator import NativeCoordinator
from civ_agent.orchestration.store import WorkflowStore
from tests.test_host_connection import endpoint, result
from tests.test_host_connection import material as material


def task_location(task):
    return [f"{frame.f_code.co_name}:{frame.f_code.co_filename}:{frame.f_lineno}" for frame in task.get_stack()]


def test_concurrent_communication_connection_has_one_owner_and_shutdown_leaves_no_mcp_task(material):
    async def run():
        connected, release = asyncio.Event(), asyncio.Event()
        created, connect_calls = [], []

        async def call(request, body):
            return result(body, {"ok": True})

        async with endpoint(material, call) as (url, _):

            async def lease():
                return {"epoch": "e1", "serverInstance": "s1"}

            async def op(name, **fields):
                assert name == "connect"
                connect_calls.append(fields)
                connected.set()
                await release.wait()
                return {"playerToken": "P" * 43}

            def factory(*args):
                connection = HostConnection(*args)
                created.append(connection)
                return connection

            root = SimpleNamespace(
                external_lease_id=None,
                lease=lease,
                op=op,
                game_state={"lease": "sole-lease"},
                civilization="Rome",
                operator=SimpleNamespace(host_url=url.removesuffix("/mcp")),
                host_ca=material["ca_file"],
                connection_factory=factory,
            )
            communication = NativeCommunication(root, None)
            try:
                first = asyncio.create_task(communication.connection("e1"))
                await connected.wait()
                second = asyncio.create_task(communication.connection("e1"))
                await asyncio.sleep(0.05)
                release.set()
                connections = await asyncio.gather(first, second)
                assert (
                    await asyncio.gather(*(connection.tool("read", {}) for connection in connections))
                    == [{"ok": True}] * 2
                )
                await communication.close()
                pending = [
                    task_location(connection._owner)
                    for connection in created
                    if connection._owner is not None and not connection._owner.done()
                ]
                assert not pending, f"MCP owner orphaned after coordinator transport close: {pending}"
                assert len(created) == len(connect_calls) == 1
            finally:
                release.set()
                await asyncio.gather(*(connection.close() for connection in created))

    asyncio.run(run())


@pytest.mark.parametrize("failure", [False, True])
def test_shutdown_joins_accepted_heartbeat_read_then_stops_even_when_host_read_fails(tmp_path, failure):
    async def run():
        entered, release = threading.Event(), threading.Event()
        calls = []

        def read(operation, **fields):
            assert operation == "status"
            calls.append(operation)
            entered.set()
            assert release.wait(5)
            if failure:
                raise OperatorError("Host stopped during accepted read", "TRANSPORT_TIMEOUT")
            return {"status": {"phase": "PAUSED", "leaseId": "owner"}}

        store = WorkflowStore(tmp_path / "worker", player="p0")
        coordinator = NativeCoordinator(
            game="game",
            player="p0",
            civilization="Rome",
            state_dir=tmp_path / "state",
            store=store,
            operator=cast(Operator, SimpleNamespace(call=read)),
            host_ca=tmp_path / "ca",
            external_lease_id="owner",
        )
        heartbeat = asyncio.create_task(coordinator.heartbeat())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            heartbeat.cancel()
            await asyncio.sleep(0.05)
            assert not heartbeat.done(), "An accepted operator read must settle before shutdown"
            release.set()
            await asyncio.sleep(0.1)
            assert heartbeat.done(), (
                f"Canceled heartbeat kept retrying after its read settled: {task_location(heartbeat)}"
            )
            with pytest.raises(asyncio.CancelledError):
                await heartbeat
            assert calls == ["status"]
        finally:
            release.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await coordinator.close()
            store.close()

    asyncio.run(run())
