import asyncio
import stat

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai_harness.step_persistence import ContinuableSnapshot

from civ_agent.orchestration.contracts import Scope
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.orchestration.transcripts import backend, load, read, seal
from tests.step_history import save_history


def test_scoped_pointer_loads_upstream_messages_without_copying_the_transcript(tmp_path):
    async def run():
        store = WorkflowStore(tmp_path, player="p0")
        store.bind_epoch("game", "e1")
        scope = Scope(game="game", player="p0", epoch="e1", activation="a1")
        messages = [ModelResponse(parts=[TextPart("private evidence " * 500)])]
        try:
            transcript = await save_history(store, scope, messages)
            reference = await seal(store, scope, transcript, agent="strategic_review_v1")
            assert store.get(scope, reference) == transcript.model_dump(mode="json")
            assert await read(store, scope, reference, agent="strategic_review_v1") == messages
            with store.engine.connect() as db:
                bodies = db.exec_driver_sql("SELECT body FROM artifacts").scalars().all()
                assert len(bodies) == 1 and len(bodies[0]) < 250
                assert "private evidence" not in bodies[0]
            assert stat.S_IMODE((tmp_path / "agent-steps.sqlite3").stat().st_mode) == 0o600
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["player", "epoch", "activation", "game", "agent", "run", "digest", "snapshot"])
def test_snapshot_read_rejects_cross_scope_references_and_changed_evidence(tmp_path, damage):
    async def run():
        store = WorkflowStore(tmp_path, player="p0")
        store.bind_epoch("game", "e1")
        scope = Scope(game="game", player="p0", epoch="e1", activation="a1")
        agent = "strategic_review_v1"
        try:
            transcript = await save_history(store, scope, [ModelResponse(parts=[TextPart("original evidence")])])
            if damage in {"player", "epoch", "activation", "game"}:
                scope = scope.model_copy(update={damage: "changed"})
            elif damage == "agent":
                agent = "player_role_v1"
            elif damage == "run":
                transcript = transcript.model_copy(update={"run_id": "unknown"})
            elif damage == "digest":
                transcript = transcript.model_copy(update={"sha256": "0" * 64})
            else:
                await backend(store).save_snapshot(
                    ContinuableSnapshot(
                        run_id=transcript.run_id,
                        step_index=1,
                        messages=[ModelResponse(parts=[TextPart("changed evidence")])],
                        agent_name=agent,
                    )
                )
            with pytest.raises(JournalConflict):
                await seal(store, scope, transcript, agent=agent)
            with store.engine.connect() as db:
                assert db.exec_driver_sql("SELECT count(*) FROM artifacts").scalar_one() == 0
        finally:
            store.close()

    asyncio.run(run())


def test_epoch_advance_fences_existing_transcript_pointer(tmp_path):
    async def run():
        store = WorkflowStore(tmp_path, player="p0")
        store.bind_epoch("game", "e1")
        scope = Scope(game="game", player="p0", epoch="e1", activation="a1")
        try:
            transcript = await save_history(store, scope, [])
            store.bind_epoch("game", "e2", previous="e1")
            with pytest.raises(JournalConflict, match="EPOCH"):
                await load(store, scope, transcript, agent="strategic_review_v1")
        finally:
            store.close()

    asyncio.run(run())
