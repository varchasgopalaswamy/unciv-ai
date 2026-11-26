"""Measure native Memory/Planning reuse before changing the production notebook."""

import asyncio
import json
import sqlite3
from typing import Literal

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.exceptions import UnexpectedModelBehavior, UserError
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai_harness import Memory, Planning
from pydantic_ai_harness.memory import (
    MemoryConflictError,
    MemoryOperation,
    MemoryOperationConflictError,
    SqliteMemoryStore,
)
from pydantic_ai_harness.planning import PlanItem, SqlitePlanStore, TaskStatus

from civ_agent.orchestration.contracts import Scope, digest
from civ_agent.schemas import BoundaryModel
from civ_agent.sqlite_database import sqlite_engine


class MemoryDeps(BoundaryModel):
    scope: Scope
    role: Literal["main", "economy", "communication"] = "main"
    admitted_epoch: str = "e1"
    database: str
    plans_database: str


def namespace(ctx: RunContext[MemoryDeps]) -> str:
    # This is an application policy sample, not native authorization. A real
    # coordinator must mint and fence these trusted dependencies itself.
    scope = ctx.deps.scope
    if scope.epoch != ctx.deps.admitted_epoch:
        raise UserError("STALE_MEMORY_EPOCH")
    return f"{scope.game}/{scope.player}/{scope.epoch}"


def provenance(ctx: RunContext[MemoryDeps]):
    if ctx.deps.role != "main":
        raise UserError("READ_ONLY_SHARED_MEMORY")
    return {
        "game": ctx.deps.scope.game,
        "player": ctx.deps.scope.player,
        "epoch": ctx.deps.scope.epoch,
        "role": ctx.deps.role,
    }


async def role_tools(ctx: RunContext[MemoryDeps], tools: list[ToolDefinition]) -> list[ToolDefinition]:
    return (
        tools if ctx.deps.role == "main" else [tool for tool in tools if tool.name in {"read_memory", "search_memory"}]
    )


def memory_capability() -> Memory[MemoryDeps]:
    return Memory[MemoryDeps](
        store_resolver=lambda ctx: SqliteMemoryStore(database=ctx.deps.database),
        namespace=namespace,
        agent_name="shared",
        write_metadata=provenance,
    )


def planning_capability() -> Planning[MemoryDeps]:
    return Planning[MemoryDeps](
        store_resolver=lambda ctx: SqlitePlanStore(ctx.deps.plans_database, session=namespace(ctx)),
        tools=["write_plan", "read_plan", "update_task_status"],
    )


def scripted(*calls, observed=None):
    def respond(messages, info):
        if observed is not None:
            observed.append((messages, info.function_tools))
        returns = [
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if len(returns) < len(calls):
            name, arguments = calls[len(returns)]
            return ModelResponse(parts=[ToolCallPart(name, arguments, f"call{len(returns)}")])
        return ModelResponse(parts=[TextPart("Native notes and plan saved.")])

    return respond


def deps(tmp_path, *, player="p0", epoch="e1", role: Literal["main", "economy", "communication"] = "main"):
    return MemoryDeps(
        scope=Scope(game="game", player=player, epoch=epoch, activation="probe"),
        role=role,
        admitted_epoch=epoch,
        database=str(tmp_path / "native-memory.sqlite3"),
        plans_database=str(tmp_path / "native-plans.sqlite3"),
    )


def test_upstream_sqlite_compare_and_swap_and_mutation_receipts_survive_reopen(tmp_path):
    async def run():
        database = tmp_path / "memory.sqlite3"
        store = SqliteMemoryStore(database=database)
        path = "game/p0/e1/shared/MEMORY.md"
        operation = MemoryOperation(id="original-write", fingerprint=digest("original content"))
        first = await store.write_with_metadata(
            path, "Original authored note", expected_version=None, operation=operation, metadata={"role": "main"}
        )
        assert first.version is not None and not first.replayed
        competing = await asyncio.gather(
            *[
                SqliteMemoryStore(database=database).write(
                    path,
                    f"Competing note {index}",
                    expected_version=first.version,
                    operation=MemoryOperation(id=f"competing-{index}", fingerprint=digest(index)),
                )
                for index in range(2)
            ],
            return_exceptions=True,
        )
        assert sum(isinstance(value, MemoryConflictError) for value in competing) == 1
        reopened = SqliteMemoryStore(database=database)
        current = await reopened.read(path, max_chars=1000)
        assert current is not None and current.content.startswith("Competing note")
        replayed = await reopened.write_with_metadata(
            path, "Original authored note", expected_version=None, operation=operation, metadata={"role": "forged"}
        )
        assert replayed.replayed and replayed.version == first.version
        assert await reopened.read(path, max_chars=1000) == current  # A replay cannot undo a newer revision.
        with pytest.raises(MemoryOperationConflictError):
            await reopened.write(
                path,
                "Changed arguments",
                expected_version=current.version,
                operation=MemoryOperation(id=operation.id, fingerprint=digest("changed content")),
            )
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT count(*) FROM memory_operations").fetchone()[0] == 2

    asyncio.run(run())


def test_upstream_store_does_not_commit_or_join_an_active_application_transaction(tmp_path):
    async def run():
        connection = sqlite3.connect(tmp_path / "memory.sqlite3", check_same_thread=False)
        store = SqliteMemoryStore(connection=connection)
        try:
            await store.write("scope/main/MEMORY.md", "Before", expected_version=None)
            connection.execute("CREATE TABLE application_outbox (id TEXT)")
            connection.commit()
            connection.execute("BEGIN")
            connection.execute("INSERT INTO application_outbox VALUES ('uncommitted')")
            with pytest.raises(RuntimeError, match="must be idle"):
                await store.write("scope/main/other.md", "Must not escape the transaction", expected_version=None)
            assert connection.in_transaction
            connection.rollback()
            assert connection.execute("SELECT count(*) FROM application_outbox").fetchone()[0] == 0
            assert await store.read("scope/main/other.md", max_chars=1000) is None
        finally:
            connection.close()

    asyncio.run(run())


def test_pinned_native_mutation_primitive_can_join_the_existing_sqlalchemy_transaction(tmp_path):
    async def run():
        database = tmp_path / "memory.sqlite3"
        store = SqliteMemoryStore(database=database)
        path = "game/p0/e1/shared/MEMORY.md"
        await store.read(path, max_chars=1000)  # Upstream owns its schema initialization.
        engine = sqlite_engine(database)
        operation = MemoryOperation(id="atomic-note", fingerprint=digest("authored note"))
        try:
            with engine.begin() as db:
                db.exec_driver_sql("CREATE TABLE application_outbox (id TEXT PRIMARY KEY)")
            with pytest.raises(RuntimeError, match="abort both"):
                with engine.begin() as db:
                    connection = db.connection.driver_connection
                    assert isinstance(connection, sqlite3.Connection)
                    # Harness0.64 private primitive, deliberately version-pinned.
                    # No copied SQL or second mutation/receipt implementation.
                    store._write(connection, path, "Authored note", None, operation, json.dumps({"role": "main"}))
                    db.exec_driver_sql("INSERT INTO application_outbox VALUES ('notice')")
                    raise RuntimeError("abort both")
            assert await store.read(path, max_chars=1000) is None and await store.get_operation(operation) is None
            with engine.begin() as db:
                connection = db.connection.driver_connection
                assert isinstance(connection, sqlite3.Connection)
                receipt = store._write(connection, path, "Authored note", None, operation, json.dumps({"role": "main"}))
                db.exec_driver_sql("INSERT INTO application_outbox VALUES ('notice')")
            reopened = SqliteMemoryStore(database=database)
            recovered = await reopened.get_operation(operation)
            assert recovered is not None and recovered.replayed and recovered.version == receipt.version
            with engine.begin() as db:
                connection = db.connection.driver_connection
                assert isinstance(connection, sqlite3.Connection)
                with pytest.raises(MemoryConflictError):
                    store._write(connection, path, "Stale overwrite", None, None, None)
                assert db.exec_driver_sql("SELECT count(*) FROM application_outbox").scalar_one() == 1
                assert db.exec_driver_sql("SELECT count(*) FROM memory_operations").scalar_one() == 1
            current = await reopened.read(path, max_chars=1000)
            assert current is not None and current.content == "Authored note" and current.metadata == {"role": "main"}
        finally:
            engine.dispose()

    asyncio.run(run())


def test_native_tools_keep_namespace_and_write_provenance_out_of_model_control(tmp_path):
    async def run():
        observed = []
        secrets = {("p0", "e1"): "ONLY_P0_E1", ("p1", "e1"): "ONLY_P1_E1", ("p0", "e2"): "ONLY_P0_E2"}
        for (player, epoch), content in secrets.items():
            writer = Agent(
                FunctionModel(scripted(("write_memory", {"file": "MEMORY.md", "content": content}))),
                deps_type=MemoryDeps,
                retries=0,
                capabilities=[memory_capability(), PrepareTools(role_tools)],
            )
            await writer.run("Remember this authored fixture note.", deps=deps(tmp_path, player=player, epoch=epoch))
        reader = Agent(
            FunctionModel(scripted(("read_memory", {"file": "MEMORY.md"}), observed=observed)),
            deps_type=MemoryDeps,
            retries=0,
            capabilities=[memory_capability(), PrepareTools(role_tools)],
        )
        await reader.run("Read shared notes.", deps=deps(tmp_path, role="economy"))
        serialized = json.dumps(ModelMessagesTypeAdapter.dump_python(observed[-1][0], mode="json"))
        assert "ONLY_P0_E1" in serialized and "ONLY_P1_E1" not in serialized and "ONLY_P0_E2" not in serialized
        assert {tool.name for tool in observed[0][1]} == {"read_memory", "search_memory"}
        for _, tools in observed:
            for tool in tools:
                assert (
                    not {"namespace", "scope", "metadata", "write_metadata"}
                    & tool.parameters_json_schema.get("properties", {}).keys()
                )
        stored = await SqliteMemoryStore(database=tmp_path / "native-memory.sqlite3").read(
            "game/p0/e1/shared/MEMORY.md", max_chars=1000
        )
        assert stored is not None and stored.metadata == {"game": "game", "player": "p0", "epoch": "e1", "role": "main"}

    asyncio.run(run())


@pytest.mark.parametrize("role", ["economy", "communication"])
def test_role_tool_policy_blocks_model_requested_shared_writes(tmp_path, role):
    async def run():
        agent = Agent(
            FunctionModel(scripted(("write_memory", {"file": "MEMORY.md", "content": "Unauthorized change"}))),
            deps_type=MemoryDeps,
            retries=0,
            capabilities=[memory_capability(), PrepareTools(role_tools)],
        )
        with pytest.raises(UnexpectedModelBehavior):
            await agent.run("A model attempts a hidden tool.", deps=deps(tmp_path, role=role))
        assert (
            await SqliteMemoryStore(database=tmp_path / "native-memory.sqlite3").read(
                "game/p0/e1/shared/MEMORY.md", max_chars=1000
            )
            is None
        )

    asyncio.run(run())


def test_application_epoch_policy_rejects_stale_dependencies_before_memory_loading(tmp_path):
    async def run():
        agent = Agent(
            FunctionModel(scripted()),
            deps_type=MemoryDeps,
            capabilities=[memory_capability(), PrepareTools(role_tools)],
        )
        stale = deps(tmp_path).model_copy(update={"admitted_epoch": "e2"})
        with pytest.raises(UserError, match="STALE_MEMORY_EPOCH"):
            await agent.run("Resume stale work.", deps=stale)
        assert not (tmp_path / "native-memory.sqlite3").exists()

    asyncio.run(run())


def test_native_planning_persists_typed_tasks_for_another_run_and_session(tmp_path):
    async def run():
        items = [
            {"id": "inspect", "content": "Inspect the permitted frontier", "status": "in_progress"},
            {"id": "consider", "content": "Reconsider the authored plan", "status": "pending"},
        ]
        agent = Agent(
            FunctionModel(
                scripted(
                    ("write_plan", {"items": items}),
                    ("update_task_status", {"task_id": "inspect", "status": "completed"}),
                )
            ),
            deps_type=MemoryDeps,
            retries=0,
            capabilities=[planning_capability()],
        )
        request = deps(tmp_path)
        await agent.run("Plan this bounded task.", deps=request)
        reopened = SqlitePlanStore(request.plans_database, session="game/p0/e1")
        stored = await reopened.get_items()
        assert all(isinstance(item, PlanItem) for item in stored)
        assert [item.status for item in stored] == [TaskStatus.completed, TaskStatus.pending]
        assert not await SqlitePlanStore(request.plans_database, session="game/p1/e1").get_items()
        reader = Agent(
            FunctionModel(scripted(("read_plan", {}))), deps_type=MemoryDeps, capabilities=[planning_capability()]
        )
        result = await reader.run("Continue this task.", deps=request)
        assert "Inspect the permitted frontier" in json.dumps(
            ModelMessagesTypeAdapter.dump_python(result.all_messages(), mode="json")
        )

    asyncio.run(run())
