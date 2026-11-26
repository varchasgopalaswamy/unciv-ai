"""Native public store artifacts and delivered SDK evidence must agree."""

import asyncio
import copy
import hashlib
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic_ai import Agent, RunContext, ToolOutput
from pydantic_ai.messages import ModelRequest, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness import Planning
from pydantic_ai_harness.memory import SqliteMemoryStore
from pydantic_ai_harness.planning import SqlitePlanStore, TaskStatus
from pydantic_ai_harness.step_persistence import ContinuableSnapshot

from civ_agent import memory_schema as tables
from civ_agent.orchestration.authored_memory import (
    AuthoredMemory,
    AuthoredMetadata,
    authored_prefix,
    contribution_prefix,
)
from civ_agent.orchestration.contracts import (
    GameRunRequest,
    GameRunResult,
    ModelSelection,
    Reference,
    RoleReport,
    RoleRequest,
    RoleResult,
    Scope,
    digest,
)
from civ_agent.orchestration.player_plans import plan_session
from civ_agent.orchestration.scripted_model import STORE_NOTE_PREFIX, respond
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.orchestration.store_verification import _copy_database, verify_native_stores
from civ_agent.orchestration.transcripts import PlayerPersistence, backend, pointer, seal
from civ_agent.sqlite_database import sqlite_engine


async def build(root):
    domain_path = root / "coordinator/notebook/memory.sqlite3"
    domain_path.parent.mkdir(parents=True)
    engine = sqlite_engine(domain_path)
    tables.metadata.create_all(engine)
    engine.dispose()
    workflow = WorkflowStore(root / "player-store", player="p0")
    workflow.bind_epoch("game", "e1")
    memory = SqliteMemoryStore(database=root / "coordinator/authored-memory.sqlite3")
    steps = backend(workflow, create=True)
    roles = []
    for turn in range(2):
        for purpose in ("gameplay", "economy", "military", "communication"):
            job = purpose + "-" + str(turn)
            scope = Scope(game="game", player="p0", epoch="e1", activation=job)
            request = RoleRequest(
                scope=scope,
                job=job,
                purpose=purpose,
                selection=ModelSelection(model="scripted-v1"),
                startup=Reference(sha256=digest("startup")),
                catalogue=Reference(sha256=digest("catalogue")),
            )
            view = {
                "turn": turn,
                "researchQueue": ["Pottery"],
                "canEndTurn": True,
                "cities": [{"handle": "city1", "owner": "economy", "constructionQueue": ["Granary"]}],
                "units": [],
            }
            startup = {
                "role": purpose,
                "nativeStoreExercise": True,
                "nativeStoreJob": job,
                "nativeStoreContributionFile": contribution_prefix(request) + "native-stores.md",
                "reads": {"player_turn": view, "chat_read": {"civilization": "Rome", "messages": []}},
            }
            plans = SqlitePlanStore(str(root / "coordinator/plans.sqlite3"), session=plan_session(request))
            kind: Literal["note", "city_plan", "contribution"] = "note"
            if purpose == "economy":
                kind = "city_plan"
            elif purpose == "military":
                kind = "contribution"

            async def player_turn(
                ctx: RunContext[RoleRequest], arguments: dict, recoverBaseline: str = "missing"
            ) -> dict:
                # This run finishes before advancing the fixture loop.
                return {"receipts": [{"tool": "player_turn", "result": copy.deepcopy(view)}]}  # noqa: B023

            async def military_review(ctx: RunContext[RoleRequest], arguments: dict) -> dict:
                return {
                    "receipts": [
                        {"tool": "military_review", "result": {"militaryCompletion": {"items": [], "nextCursor": None}}}
                    ]
                }

            async def chat_read(ctx: RunContext[RoleRequest], arguments: dict) -> dict:
                return {"receipts": [{"tool": "chat_read", "result": {"civilization": "Rome", "messages": []}}]}

            async def finish_role(ctx: RunContext[RoleRequest], arguments: dict) -> dict:
                assert (await plans.get_items())[0].status is TaskStatus.completed  # noqa: B023
                return {"receipts": [{"tool": "finish_role", "result": {"roleFinished": True}}]}

            agent = Agent(
                FunctionModel(respond),
                name="player_role_v1",
                deps_type=RoleRequest,
                output_type=ToolOutput(RoleReport, name="finish_report"),
                tools=[player_turn, military_review, chat_read, finish_role],
                capabilities=[
                    AuthoredMemory(
                        store=memory,
                        namespace="game/p0/e1",
                        agent_name="notes",
                        inject_memory=False,
                        max_memory_size=8000,
                        write_metadata=AuthoredMetadata(
                            game="game",
                            player="p0",
                            epoch="e1",
                            job=job,
                            purpose=purpose,
                            kind=kind,
                            createdTurn=turn,
                            updatedTurn=turn,
                        ).strings(),
                    ),
                    Planning(store=plans, tools=["read_plan", "write_plan", "update_task_statuses"]),
                    PlayerPersistence(store=steps, capture_frontier=True),
                ],
            )
            run_id = scope.identity("agent-run", "role:0")
            result = await agent.run(
                json.dumps(startup), deps=request, run_id=run_id, conversation_id=scope.identity("conversation", "role")
            )
            history = await seal(workflow, scope, pointer(run_id, result.all_messages()), agent="player_role_v1")
            roles.append(
                RoleResult(scope=scope, job=job, purpose=purpose, status="COMPLETED", history=history, usage={})
            )
    workflow.close()
    return SimpleNamespace(
        root=root,
        memory=memory,
        request=GameRunRequest(
            game="game",
            player="p0",
            turns=2,
            restart_after=1,
            exercise_native_stores=True,
            selection=ModelSelection(model="scripted-v1"),
        ),
        result=GameRunResult(game="game", player="p0", turn=2, handoffs=2, roles=roles),
    )


@pytest.fixture
def artifacts(tmp_path):
    return asyncio.run(build(tmp_path))


def verify(artifacts):
    return asyncio.run(verify_native_stores(artifacts.root, artifacts.result, artifacts.request))


def test_real_sdk_tools_and_reopened_native_stores_verify_without_changing_artifacts(artifacts):
    before = {
        path.relative_to(artifacts.root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in artifacts.root.rglob("*")
        if path.is_file() and not path.name.endswith("-shm")
    }
    report = verify(artifacts)
    assert report == {
        "verified": True,
        "roleRuns": 8,
        "completedRoleTasks": 8,
        "rolePlanSessions": 4,
        "memoryFiles": 4,
        "mainNoteJobs": 2,
        "notesAcrossRestart": True,
        "cityPlans": 1,
        "militaryContributions": 2,
        "nativeMemoryToolEffects": 20,
        "nativePlanningToolEffects": 24,
        "legacyAuthoredRows": 0,
    }
    after = {
        path.relative_to(artifacts.root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in artifacts.root.rglob("*")
        if path.is_file() and not path.name.endswith("-shm")
    }
    assert before == after


@pytest.mark.parametrize("change", ["content", "owner", "epoch", "role", "job", "extra_file"])
def test_native_memory_tampering_fails_closed(artifacts, change):
    async def mutate():
        path = authored_prefix(artifacts.result.roles[0].scope) + "MEMORY.md"
        current = await artifacts.memory.read(path, max_chars=8000)
        metadata = dict(current.metadata)
        if change == "content":
            content = current.content.replace(STORE_NOTE_PREFIX, "tampered")
        else:
            content = current.content
        if change in {"owner", "epoch", "role", "job"}:
            metadata[{"owner": "player", "epoch": "epoch", "role": "purpose", "job": "job"}[change]] = {
                "owner": "p1",
                "epoch": "e2",
                "role": "economy",
                "job": "foreign",
            }[change]
        if change == "extra_file":
            path, expected = "foreign/p0/e1/notes/MEMORY.md", None
        else:
            expected = current.version
        await artifacts.memory.write_with_metadata(path, content, expected_version=expected, metadata=metadata)

    asyncio.run(mutate())
    with pytest.raises(JournalConflict, match="NATIVE_STORES_"):
        verify(artifacts)


@pytest.mark.parametrize("change", ["pending", "foreign_role", "old_job"])
def test_native_plan_session_tampering_fails_closed(artifacts, change):
    async def mutate():
        role = artifacts.result.roles[4]
        store = SqlitePlanStore(str(artifacts.root / "coordinator/plans.sqlite3"), session=plan_session(role))
        items = await store.get_items()
        if change == "pending":
            items[0].status = TaskStatus.pending
        elif change == "foreign_role":
            other = SqlitePlanStore(
                str(artifacts.root / "coordinator/plans.sqlite3"), session=plan_session(artifacts.result.roles[5])
            )
            items = await other.get_items()
        else:
            items[0].content = items[0].content.replace("gameplay-1", "gameplay-0")
        await store.set_items(items)

    asyncio.run(mutate())
    with pytest.raises(JournalConflict, match="PLAN_SESSION"):
        verify(artifacts)


@pytest.mark.parametrize("change", ["missing_effect", "wrong_scope", "wrong_identity", "run_metadata"])
def test_native_upstream_scope_and_tool_effect_tampering_fails_closed(artifacts, change):
    async def mutate():
        workflow = WorkflowStore(artifacts.root / "player-store", player="p0")
        try:
            role = artifacts.result.roles[0]
            steps = backend(workflow)
            run = (await steps.list_runs(conversation_id=role.scope.identity("conversation", "role")))[0]
            events = await steps.list_events(run_id=run.run_id)
            call_id = next(event.tool_call_id for event in events if event.kind == "tool_call_started")
            assert call_id is not None
            effect = await steps.get_tool_effect(run_id=run.run_id, tool_call_id=call_id)
            assert effect is not None
            if change == "run_metadata":
                metadata = dict(run.metadata)
                scope = json.loads(metadata["scope"])
                scope["player"] = "p1"
                metadata["scope"] = json.dumps(scope, sort_keys=True, separators=(",", ":"))
                with sqlite3.connect(artifacts.root / "player-store/agent-steps.sqlite3") as db:
                    db.execute("UPDATE runs SET metadata=? WHERE run_id=?", (json.dumps(metadata), run.run_id))
            elif change == "wrong_scope":
                # Public registration is immutable; a foreign declared role is equally invalid.
                artifacts.result.roles[0] = role.model_copy(
                    update={"scope": role.scope.model_copy(update={"player": "p1"})}
                )
            elif change == "missing_effect":
                await steps.record_tool_effect(replace(effect, status="failed"))
            else:
                await steps.record_tool_effect(replace(effect, idempotency_key="foreign-native-operation"))
        finally:
            workflow.close()

    asyncio.run(mutate())
    with pytest.raises(JournalConflict, match="NATIVE_STORES_|TRANSCRIPT_SCOPE_CONFLICT"):
        verify(artifacts)


@pytest.mark.parametrize("change", ["failed_read", "lost_prior_notes", "wrong_update", "read_type", "duplicate_return"])
def test_coherent_resealed_sdk_history_cannot_forge_success_or_task_completion(artifacts, change):
    async def mutate():
        workflow = WorkflowStore(artifacts.root / "player-store", player="p0")
        try:
            role = artifacts.result.roles[4]
            steps = backend(workflow)
            run = (await steps.list_runs(conversation_id=role.scope.identity("conversation", "role")))[0]
            snapshot = await steps.latest_snapshot(run_id=run.run_id, include_interrupted=True)
            assert snapshot is not None
            history = copy.deepcopy(snapshot.messages)
            if change in {"failed_read", "lost_prior_notes", "read_type", "duplicate_return"}:
                part = next(
                    part
                    for message in history
                    if isinstance(message, ModelRequest)
                    for part in message.parts
                    if isinstance(part, ToolReturnPart) and part.tool_name == "read_memory"
                )
                if change == "failed_read":
                    part.outcome = "failed"
                elif change == "read_type":
                    part.content = {"notes": part.content}
                elif change == "duplicate_return":
                    history.append(ModelRequest(parts=[copy.deepcopy(part)]))
                else:
                    part.content = "Unrelated notes."
            else:
                part = next(
                    part
                    for message in history
                    if message.kind == "response"
                    for part in message.parts
                    if isinstance(part, ToolCallPart) and part.tool_name == "update_task_statuses"
                )
                part.args = {"updates": [{"task_id": "forged", "status": "completed"}]}
            await steps.save_snapshot(
                ContinuableSnapshot(run_id=run.run_id, step_index=snapshot.step_index + 1, messages=history)
            )
            history_ref = await seal(workflow, role.scope, pointer(run.run_id, history), agent="player_role_v1")
            artifacts.result.roles[4] = role.model_copy(update={"history": history_ref})
        finally:
            workflow.close()

    asyncio.run(mutate())
    with pytest.raises(JournalConflict, match="NATIVE_STORES_"):
        verify(artifacts)


def test_legacy_authored_row_is_not_accepted_as_native_storage(artifacts):
    engine = sqlite_engine(artifacts.root / "coordinator/notebook/memory.sqlite3")
    with engine.begin() as db:
        db.execute(tables.records.insert().values(key="old-note", version=1, category="note", body="{}"))
    engine.dispose()
    with pytest.raises(JournalConflict, match="LEGACY_AUTHORED_ROWS"):
        verify(artifacts)


def test_missing_native_store_is_not_created_by_the_audit(artifacts):
    path = artifacts.root / "coordinator/authored-memory.sqlite3"
    path.rename(path.with_suffix(".missing"))
    with pytest.raises(JournalConflict, match="ARTIFACT_INVALID"):
        verify(artifacts)
    assert not path.exists()


def test_backup_uses_committed_wal_data_without_modifying_the_authoritative_database(tmp_path):
    source, copied = tmp_path / "source.sqlite3", tmp_path / "audit.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE facts(value TEXT)")
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("INSERT INTO facts VALUES('committed in WAL')")
        db.commit()
        wal = source.with_name(source.name + "-wal")
        assert wal.stat().st_size > 0
        before = source.read_bytes(), wal.read_bytes()
        _copy_database(source, copied)
        assert (source.read_bytes(), wal.read_bytes()) == before
        with sqlite3.connect(copied) as audit:
            assert audit.execute("SELECT value FROM facts").fetchall() == [("committed in WAL",)]
