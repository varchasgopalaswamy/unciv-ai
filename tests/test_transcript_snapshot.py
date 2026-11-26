"""Native snapshot identity survives replay metadata without relaxing exact hashes."""

import asyncio
import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, SnapshotState

from civ_agent.orchestration.contracts import Scope, canonical
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.orchestration.transcripts import (
    NativeTranscripts,
    SnapshotTranscript,
    backend,
    load,
    pointer,
    read,
    seal,
)
from tests.test_local_review_services import review_setup as review_setup


async def stored_snapshot(store, *, state: SnapshotState = "complete"):
    scope = Scope(game="game", player="p0", epoch="e1", activation="review")
    store.bind_epoch(scope.game, scope.epoch)
    request = SnapshotTranscript(
        scope=scope,
        run_id="run",
        agent="strategic_review_v1",
        conversation_id=scope.identity("conversation", "review"),
    )
    stamp = datetime(2026, 11, 5, tzinfo=UTC)
    messages = [
        ModelRequest([UserPromptPart("Private prompt", timestamp=stamp)], timestamp=stamp, run_id="run"),
        ModelResponse([ToolCallPart("player_details", '{"section":"economy"}', "read")], timestamp=stamp),
        ModelRequest(
            [ToolReturnPart("player_details", {"source": "s1", "data": {"gold": 5.0}}, "read", timestamp=stamp)],
            timestamp=stamp,
            run_id="run",
        ),
    ]
    steps = backend(store, create=True)
    await steps.register_run(
        RunRecord(
            run_id="run",
            agent_name=request.agent,
            conversation_id=request.conversation_id,
            metadata={"scope": canonical(scope.model_dump(mode="json"))},
            registration_id="original-sdk-registration",
        )
    )
    snapshot = ContinuableSnapshot(
        run_id="run",
        agent_name=request.agent,
        conversation_id=request.conversation_id,
        step_index=1,
        messages=messages,
        state=state,
    )
    await steps.save_snapshot(snapshot)
    return request, snapshot


def test_structured_read_snapshot_preserves_original_metadata_and_exact_message_equivalence(tmp_path):
    async def run():
        store = WorkflowStore(tmp_path, player="p0")
        try:
            request, snapshot = await stored_snapshot(store)
            replayed = copy.deepcopy(snapshot.messages)
            for message in replayed:
                if isinstance(message, ModelRequest):
                    assert message.timestamp is not None
                    message.timestamp += timedelta(seconds=5)
                    part = message.parts[0]
                    assert isinstance(part, (UserPromptPart, ToolReturnPart))
                    part.timestamp += timedelta(seconds=5)
            expected = pointer(request.run_id, snapshot.messages)
            assert pointer(request.run_id, replayed) != expected
            actual = await NativeTranscripts(store).read(request)
            assert actual == expected
            reference = await seal(store, request.scope, actual, agent=request.agent)
            assert await read(store, request.scope, reference, agent=request.agent) == snapshot.messages
            # No timestamp stripping, content repair or replacement transcript.
            changed = replace(snapshot, messages=[*snapshot.messages, ModelResponse([TextPart("Changed")])])
            await backend(store).save_snapshot(changed)
            with pytest.raises(JournalConflict, match="TRANSCRIPT_SNAPSHOT_CONFLICT"):
                await load(store, request.scope, actual, agent=request.agent)
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "damage", ["game", "epoch", "player", "activation", "agent", "run", "conversation", "registration", "snapshot"]
)
def test_native_snapshot_pointer_requires_exact_current_registered_owner(tmp_path, damage):
    async def run():
        store = WorkflowStore(tmp_path, player="p0")
        try:
            request, snapshot = await stored_snapshot(store)
            if damage in {"game", "epoch", "player", "activation"}:
                request = request.model_copy(update={"scope": request.scope.model_copy(update={damage: "foreign"})})
            elif damage == "agent":
                request = request.model_copy(update={"agent": "player_role_v1"})
            elif damage == "run":
                request = request.model_copy(update={"run_id": "unknown"})
            elif damage == "conversation":
                request = request.model_copy(update={"conversation_id": "foreign"})
            elif damage == "registration":
                # Invalid producer registration is not a native SDK run.
                import sqlite3

                with sqlite3.connect(store.root / "agent-steps.sqlite3") as db:
                    db.execute("UPDATE runs SET registration_id=NULL")
            else:
                await backend(store).save_snapshot(replace(snapshot, agent_name="player_role_v1"))
            with pytest.raises(JournalConflict):
                await NativeTranscripts(store).read(request)
            with store.engine.connect() as db:
                assert db.exec_driver_sql("SELECT COUNT(*) FROM artifacts").scalar_one() == 0
        finally:
            store.close()

    asyncio.run(run())


def test_interrupted_native_snapshot_requires_explicit_partial_run_permission(tmp_path):
    async def run():
        store = WorkflowStore(tmp_path, player="p0")
        try:
            request, snapshot = await stored_snapshot(store, state="interrupted")
            with pytest.raises(JournalConflict, match="TRANSCRIPT_SNAPSHOT_CONFLICT"):
                await NativeTranscripts(store).read(request)
            allowed = request.model_copy(update={"allow_interrupted": True})
            transcript = await NativeTranscripts(store).read(allowed)
            assert transcript == pointer(request.run_id, snapshot.messages)
            assert await load(store, request.scope, transcript, agent=request.agent) == snapshot.messages
        finally:
            store.close()

    asyncio.run(run())
