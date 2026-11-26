import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from civ_agent import memory_schema as tables
from civ_agent.operator_connection import Operator
from civ_agent.orchestration.contracts import GatewayJob, Scope
from civ_agent.orchestration.coordinator import NativeCoordinator
from civ_agent.orchestration.gateway_state import capture_job
from civ_agent.orchestration.preparation import prepare_job
from civ_agent.orchestration.store import JournalConflict, WorkflowStore


@pytest.mark.parametrize("damage", [None, "effect", "call", "completed", "binding", "registered"])
def test_interrupted_startup_retains_snapshot_but_refreshes_reads_before_admission(tmp_path, damage):
    async def run():
        store = WorkflowStore(tmp_path / "worker", player="p0")
        root = NativeCoordinator(
            game="game",
            player="p0",
            civilization="Rome",
            state_dir=tmp_path / "coordinator",
            store=store,
            operator=cast(Operator, SimpleNamespace()),
            host_ca=tmp_path / "ca.pem",
        )
        root.memory.bind("e1")
        binding = GatewayJob(
            scope=Scope(game="game", player="p0", epoch="e1", activation="job1"), job="job1", purpose="communication"
        )
        connection = SimpleNamespace(tool=AsyncMock(), close=AsyncMock())
        try:
            job, prefix = await prepare_job(root, binding, connection)
            assert prefix == "startup:0:"
            root.memory.call(job.id, prefix + "chat_read", "chat_read", {}, {"messages": []})
            original = job.memory.copy()
            if damage == "effect":
                root.memory.queue(job.id, "player_communication", {"op": "message"}, "unexpected")
            with root.memory.transaction() as db:
                db.execute(
                    tables.jobs.update()
                    .where(tables.jobs.c.id == job.id)
                    .values(status="COMPLETED" if damage == "completed" else "INTERRUPTED")
                )
            if damage == "call":
                with root.memory.transaction() as db:
                    db.execute(
                        tables.calls.insert().values(job=job.id, id="model-call", name="chat_read", arguments="{}")
                    )
            elif damage == "binding":
                binding = binding.model_copy(
                    update={"scope": binding.scope.model_copy(update={"activation": "changed"})}
                )
            elif damage == "registered":
                await root.registry.register(binding, capture_job(job, delivered_access={}), [])
            if damage is None:
                recovered, fresh_prefix = await prepare_job(root, binding, connection)
                assert recovered.memory == original
                assert recovered.overview_view == "communication" and recovered.observation is None
                assert fresh_prefix == "startup:1:" and recovered.token != job.token and job.closed
                root.memory.call(recovered.id, fresh_prefix + "chat_read", "chat_read", {})
                with root.memory.transaction() as db:
                    assert db.scalar(tables.jobs.select().with_only_columns(tables.jobs.c.status)) == "RUNNING"
                    assert len(db.execute(tables.calls.select()).all()) == 2
            else:
                with pytest.raises(JournalConflict, match="PREPARATION_"):
                    await prepare_job(root, binding, connection)
        finally:
            await root.close()
            store.close()

    asyncio.run(run())
