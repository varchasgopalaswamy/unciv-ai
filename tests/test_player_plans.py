"""Role authority wraps native Planning CRUD, bulk updates and dependencies."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.planning import PlanItem, SqlitePlanStore, TaskStatus

from civ_agent.civilization_state import StateConflict
from civ_agent.orchestration.contracts import GatewayJob, ModelSelection, PlayerRole, Reference, RoleRequest, Scope
from civ_agent.orchestration.gateway_registry import GatewayRegistry
from civ_agent.orchestration.gateway_state import capture_job
from civ_agent.orchestration.player_plans import (
    PLAN_TOOLS,
    PlayerPlanReply,
    PlayerPlanRequest,
    PlayerPlanService,
    ReadPlan,
    ReadRolePlan,
    WritePlan,
    plan_session,
    player_planning,
    sequential_plan_tools,
)
from civ_agent.orchestration.store import JournalConflict
from tests.test_diplomacy_contracts import opened


@asynccontextmanager
async def plans(tmp_path, *, purpose: PlayerRole = "gameplay"):
    async with opened(tmp_path / "domain", purpose=purpose) as (gateway, job, host):
        identity = GatewayJob(
            scope=Scope(game="game", player="p0", epoch="g1", activation="first"), job=job.id, purpose=purpose
        )

        async def resolve(registered):
            return next(current for current in gateway.jobs.values() if current.id == registered.identity.job)

        registry = GatewayRegistry(gateway, game="game", player="p0", resolve=resolve)
        await registry.register(identity, capture_job(job, delivered_access={}), [])
        service = PlayerPlanService(registry, tmp_path / "native" / "plans.sqlite3")

        class Client:
            async def post(self, route, body):
                assert route == "plans"
                return (await service.invoke(PlayerPlanRequest.model_validate(body))).model_dump(mode="json")

        try:
            yield SimpleNamespace(
                gateway=gateway,
                job=job,
                host=host,
                registry=registry,
                service=service,
                identity=identity,
                client=Client(),
            )
        finally:
            await registry.close()


def write(identity, text="Build the current plan"):
    return PlayerPlanRequest(identity=identity, operation=WritePlan(items=[PlanItem(id="task", content=text)]))


def role_request(identity):
    return RoleRequest(
        **identity.model_dump(),
        startup=Reference(sha256="a" * 64),
        catalogue=Reference(sha256="b" * 64),
        selection=ModelSelection(model="test"),
    )


@pytest.mark.parametrize("purpose", ["gameplay", "economy", "military", "communication"])
def test_role_plan_persists_between_jobs_and_retired_writer_cannot_replace_it(tmp_path, purpose):
    async def run():
        async with plans(tmp_path, purpose=purpose) as p:
            assert await p.service.invoke(write(p.identity)) == PlayerPlanReply()
            await p.registry.retire(p.identity)
            later_job = await p.gateway.open("later", purpose, "g1", p.host, {}, 1)
            later = p.identity.model_copy(
                update={"scope": p.identity.scope.model_copy(update={"activation": "later"}), "job": "later"}
            )
            await p.registry.register(later, capture_job(later_job, delivered_access={}), [])
            loaded = await p.service.invoke(PlayerPlanRequest(identity=later, operation=ReadPlan()))
            assert loaded.items and loaded.items[0].content == "Build the current plan"
            await p.service.invoke(write(later, "New authored choice"))
            with pytest.raises(JournalConflict, match="RETIRED"):
                await p.service.invoke(write(p.identity))
            result = await p.service.invoke(PlayerPlanRequest(identity=later, operation=ReadPlan()))
            assert result.items and result.items[0].content == "New authored choice" and not p.host.writes

    asyncio.run(run())


def test_committed_plan_survives_lost_ack_without_a_second_journal_or_write_fence(tmp_path):
    async def run():
        async with plans(tmp_path) as p:
            store = p.service.store(p.identity)
            original = store.set_items

            async def lost_ack(items):
                await original(items)
                raise OSError("Lost acknowledgement after SQLite commit")

            store.set_items = lost_ack
            with pytest.raises(OSError):
                await p.service.invoke(write(p.identity))
            reopened = PlayerPlanService(p.registry, p.service.database)
            result = await reopened.invoke(PlayerPlanRequest(identity=p.identity, operation=ReadPlan()))
            assert result.items and result.items[0].content == "Build the current plan"
            await reopened.invoke(write(p.identity, "Reconsidered fresh choice"))
            actual = await SqlitePlanStore(str(reopened.database), session=plan_session(p.identity)).get_items()
            assert actual[0].content == "Reconsidered fresh choice"
            with p.gateway.memory.transaction() as db:
                assert p.gateway.memory._meta(db, "nativePlanningPending:" + plan_session(p.identity)) is None

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["player", "game", "role", "epoch"])
def test_invalid_authority_cannot_read_or_mutate_plans(tmp_path, damage):
    async def run():
        async with plans(tmp_path) as p:
            await p.service.invoke(write(p.identity))
            identity = p.identity
            if damage == "role":
                identity = identity.model_copy(update={"purpose": "economy"})
            else:
                value = {"player": "p1", "game": "other", "epoch": "g2"}[damage]
                identity = identity.model_copy(update={"scope": identity.scope.model_copy(update={damage: value})})
            for operation in (ReadPlan(), WritePlan(items=[])):
                with pytest.raises(JournalConflict):
                    await p.service.invoke(PlayerPlanRequest(identity=identity, operation=operation))
            assert (await p.service.store(p.identity).get_items())[0].content == "Build the current plan"

    asyncio.run(run())


def test_latest_role_writer_and_checkpoint_guard_prevent_stale_mutations(tmp_path):
    async def run():
        async with plans(tmp_path) as p:
            p.gateway.role_coordinator = SimpleNamespace(jobs={"gameplay": SimpleNamespace(id="newer-job")})
            with pytest.raises(JournalConflict, match="WRITER_CHANGED"):
                await p.service.invoke(write(p.identity))
            p.gateway.role_coordinator = None
            p.gateway.checkpoint_requested = True
            with pytest.raises(JournalConflict, match="CHECKPOINT"):
                await p.service.invoke(write(p.identity))
            assert await p.service.invoke(
                PlayerPlanRequest(identity=p.identity, operation=ReadPlan())
            ) == PlayerPlanReply(items=[])

    asyncio.run(run())


@pytest.mark.parametrize("purpose", ["gameplay", "communication"])
def test_bulk_updates_and_dependency_propagation_use_full_native_crud(tmp_path, purpose):
    async def run():
        async with plans(tmp_path, purpose=purpose) as p:
            if purpose == "communication":
                # Communication can run while this player's turn roles are active.
                p.gateway.role_coordinator = SimpleNamespace(jobs={"gameplay": SimpleNamespace(id="main-job")})
            script = [
                (
                    "write_plan",
                    {
                        "items": [
                            {"id": "parent", "content": "Develop"},
                            {"id": "a", "content": "Inspect"},
                            {"id": "b", "content": "Choose"},
                        ]
                    },
                ),
                ("add_task", {"content": "Remove this later"}),
                ("set_dependency", {"task_id": "b", "depends_on_id": "a"}),
                ("add_subtask", {"parent_id": "parent", "content": "One child"}),
                (
                    "update_task_statuses",
                    {
                        "updates": [
                            {"task_id": "a", "status": "completed"},
                            {"task_id": "parent", "status": "completed"},
                        ]
                    },
                ),
                ("get_available_tasks", {}),
                ("update_task_status", {"task_id": "b", "status": "completed"}),
                ("remove_task", {"task_id": "parent"}),
                ("read_plan", {}),
            ]

            async def model(messages, info):
                assert {t.name for t in info.function_tools} == set(PLAN_TOOLS)
                assert all(t.sequential for t in info.function_tools)
                returned = [
                    part
                    for m in messages
                    if isinstance(m, ModelRequest)
                    for part in m.parts
                    if isinstance(part, ToolReturnPart)
                ]
                assert all(part.outcome == "success" for part in returned)
                current = {item.id: item for item in await p.service.store(p.identity).get_items()}
                if len(returned) == 3:
                    assert current["b"].status is TaskStatus.blocked
                if len(returned) == 4:
                    assert any(item.parent_id == "parent" for item in current.values())
                if len(returned) == 5:
                    assert current["a"].status is TaskStatus.completed
                    assert current["parent"].status is TaskStatus.completed
                    assert current["b"].status is TaskStatus.pending
                if len(returned) < len(script):
                    name, args = script[len(returned)]
                    return ModelResponse(parts=[ToolCallPart(name, args, "plan" + str(len(returned)))])
                assert "2 completed" in str(returned[-1].content)
                return ModelResponse(parts=[TextPart("Done")])

            agent = Agent(
                FunctionModel(model),
                deps_type=RoleRequest,
                capabilities=[player_planning(lambda: p.client), PrepareTools(sequential_plan_tools)],
            )
            result = await agent.run("Coordinate native tasks", deps=role_request(p.identity))
            assert result.output == "Done" and result.usage.requests == len(script) + 1
            items = {item.id: item for item in await p.service.store(p.identity).get_items()}
            assert items["a"].status is TaskStatus.completed and items["b"].status is TaskStatus.completed
            assert all(item.parent_id is None for item in items.values())

    asyncio.run(run())


def test_cross_role_read_is_scoped_and_cannot_write_the_other_plan(tmp_path):
    async def run():
        async with plans(tmp_path) as p:
            await p.service.invoke(write(p.identity, "Main's work"))
            economy = p.identity.model_copy(update={"purpose": "economy"})
            await p.service.store(economy).set_items([PlanItem(id="city", content="Economy's work")])

            def model(messages, info):
                returned = [
                    part
                    for m in messages
                    if isinstance(m, ModelRequest)
                    for part in m.parts
                    if isinstance(part, ToolReturnPart)
                ]
                if not returned:
                    return ModelResponse(parts=[ToolCallPart("read_role_plan", {"role": "economy"}, "read")])
                assert "Economy's work" in str(returned[0].content)
                return ModelResponse(parts=[TextPart("Coordinate without editing")])

            agent = Agent(FunctionModel(model), deps_type=RoleRequest, capabilities=[player_planning(lambda: p.client)])
            result = await agent.run("Read colleague's plan", deps=role_request(p.identity))
            assert result.output == "Coordinate without editing"
            assert (await p.service.store(p.identity).get_items())[0].content == "Main's work"
            assert (await p.service.store(economy).get_items())[0].content == "Economy's work"
            with pytest.raises(ValidationError):
                PlayerPlanRequest.model_validate(
                    {
                        "identity": p.identity.model_dump(),
                        "operation": {
                            "kind": "update_item",
                            "item_id": "city",
                            "role": "economy",
                            "content": "Overwrite",
                        },
                    }
                )
            for field in ("game", "player", "epoch", "session"):
                with pytest.raises(ValidationError):
                    ReadRolePlan.model_validate({"role": "economy", field: "other"})

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["paused", "run_failure"])
def test_plan_mutation_obeys_native_gateway_suspension(tmp_path, failure):
    async def run():
        async with plans(tmp_path) as p:
            await p.service.invoke(write(p.identity))
            if failure == "paused":
                p.gateway.paused = True
            else:
                p.gateway.run_failure = {"error": "NATIVE_INSPECTION_REQUIRED"}
            with pytest.raises(StateConflict):
                await p.service.invoke(write(p.identity, "Must not commit"))
            assert (await p.service.store(p.identity).get_items())[0].content == "Build the current plan"

    asyncio.run(run())
