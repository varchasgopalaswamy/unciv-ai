"""The opt-in fixture uses real native Memory/Planning tools around unchanged choices."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, RunContext, ToolOutput
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness import Planning
from pydantic_ai_harness.memory import InMemoryStore
from pydantic_ai_harness.planning import InMemoryPlanStore, TaskStatus

from civ_agent.game_commands import PlayerCommand
from civ_agent.memory_records import CityPlan
from civ_agent.orchestration.authored_memory import AUTHORED_AGENT, AuthoredMemory, authored_prefix, contribution_prefix
from civ_agent.orchestration.contracts import ModelSelection, Reference, RoleReport, RoleRequest, Scope, digest
from civ_agent.orchestration.scripted_model import STORE_CITY_REASON, STORE_NOTE_PREFIX, STORE_TASK_PREFIX, respond


def request(role, job):
    return RoleRequest(
        scope=Scope(game="game", player="p0", epoch="g1", activation=job),
        job=job,
        purpose=role,
        selection=ModelSelection(model="scripted-v1"),
        startup=Reference(sha256=digest("startup")),
        catalogue=Reference(sha256=digest("catalogue")),
    )


async def exercise(role, *, enabled=True, memory=None, plans=None, job="first", contribution=True, turn=0):
    memory, plans = memory or InMemoryStore(), plans or InMemoryPlanStore()
    deps = request(role, job)
    view = {
        "turn": turn,
        "researchQueue": ["Pottery"],
        "canEndTurn": True,
        "cities": [
            {"handle": "city1", "owner": "gameplay", "constructionQueue": ["Granary"]},
            {"handle": "city2", "owner": "economy", "constructionQueue": []},
        ],
        "units": [{"handle": "settler1", "canFoundCity": role == "gameplay" and turn == 0}],
    }
    startup = {
        "role": role,
        "reads": {"player_turn": copy.deepcopy(view), "chat_read": {"civilization": "Rome", "messages": []}},
        "nativeStoreExercise": enabled,
        "nativeStoreJob": job,
    }
    if role == "military" and contribution:
        startup["nativeStoreContributionFile"] = contribution_prefix(deps) + "native-stores.md"
    invoked, executed = [], []

    def envelope(name, result):
        return {"receipts": [{"tool": name, "result": result}]}

    async def player_turn(ctx: RunContext[RoleRequest], arguments: dict, recoverBaseline: str = "missing") -> dict:
        return envelope("player_turn", copy.deepcopy(view))

    async def construction_details(
        ctx: RunContext[RoleRequest], arguments: dict, recoverBaseline: str = "missing"
    ) -> dict:
        return envelope(
            "construction_details",
            {
                "query": {"arguments": arguments},
                "data": {
                    "detailLevel": "named",
                    "constructions": [
                        {
                            "command": {
                                "kind": "QUEUE_CONSTRUCTION",
                                "target": arguments["target"],
                                "selection": "Monument",
                            }
                        }
                    ],
                },
            },
        )

    async def execute_commands(ctx: RunContext[RoleRequest], commands: list[PlayerCommand]) -> dict:
        if enabled:
            items = await plans.get_items()
            assert len(items) == 1 and items[0].status is TaskStatus.in_progress
        receipts = []
        for command in commands:
            body = command.model_dump(exclude_unset=True)
            executed.append(body)
            if body["kind"] == "FOUND_CITY":
                view["units"][0]["canFoundCity"] = False
            elif body["kind"] == "QUEUE_CONSTRUCTION":
                next(city for city in view["cities"] if city["handle"] == body["target"])["constructionQueue"] = [
                    body["selection"]
                ]
            receipts.append({"tool": "player_command", "status": "confirmed", "result": {"applied": True}})
        return {"receipts": receipts}

    async def military_review(ctx: RunContext[RoleRequest], arguments: dict) -> dict:
        return envelope("military_review", {"militaryCompletion": {"items": [], "nextCursor": None}})

    async def chat_read(ctx: RunContext[RoleRequest], arguments: dict) -> dict:
        return envelope("chat_read", {"civilization": "Rome", "messages": []})

    async def finish_role(ctx: RunContext[RoleRequest], arguments: dict) -> dict:
        if enabled:
            items = await plans.get_items()
            assert len(items) == 1 and items[0].status is TaskStatus.completed
        return envelope("finish_role", {"roleFinished": True})

    def model(messages, info):
        result = respond(messages, info)
        invoked.extend(part for part in result.parts if isinstance(part, ToolCallPart))
        return result

    agent = Agent(
        FunctionModel(model),
        deps_type=RoleRequest,
        retries=0,
        output_type=ToolOutput(RoleReport, name="finish_report"),
        tools=[player_turn, construction_details, execute_commands, military_review, chat_read, finish_role],
        capabilities=[
            AuthoredMemory(
                store=memory,
                namespace="game/p0/g1",
                agent_name=AUTHORED_AGENT,
                inject_memory=False,
                max_memory_size=8000,
            ),
            Planning(store=plans, tools=["write_plan", "read_plan", "update_task_statuses"]),
        ],
    )
    result = await agent.run(json.dumps(startup), deps=deps)
    return SimpleNamespace(
        memory=memory, plans=plans, request=deps, startup=startup, invoked=invoked, executed=executed, result=result
    )


@pytest.mark.parametrize("role", ["gameplay", "economy", "military", "communication"])
@pytest.mark.parametrize("enabled", [False, True])
def test_script_uses_native_stores_only_when_requested_and_still_finishes_each_role(role, enabled):
    async def run():
        result = await exercise(role, enabled=enabled)
        names = [call.tool_name for call in result.invoked]
        assert names[-1] == "finish_report" and "finish_role" in names
        assert isinstance(result.result.output, RoleReport)
        expected = (
            [{"kind": "FOUND_CITY", "target": "settler1"}]
            if role == "gameplay"
            else [{"kind": "QUEUE_CONSTRUCTION", "target": "city2", "selection": "Monument"}]
            if role == "economy"
            else []
        )
        assert result.executed == expected
        if not enabled:
            assert not set(names) & {
                "write_plan",
                "read_plan",
                "update_task_statuses",
                "write_memory",
                "read_memory",
                "search_memory",
            }
            assert await result.plans.get_items() == []
            return
        assert names[0:2] == ["write_plan", "read_plan"]
        items = await result.plans.get_items()
        assert len(items) == 1 and items[0].status is TaskStatus.completed
        assert items[0].content.startswith(STORE_TASK_PREFIX)
        plan = next(call.args_as_dict() for call in result.invoked if call.tool_name == "write_plan")
        assert plan["items"] == [{"content": items[0].content, "status": "in_progress"}]
        update = next(call.args_as_dict() for call in result.invoked if call.tool_name == "update_task_statuses")
        assert update == {"updates": [{"task_id": items[0].id, "status": "completed"}]}
        assert names.index("update_task_statuses") < names.index("finish_role")
        if "execute_commands" in names:
            assert names.index("execute_commands") < names.index("update_task_statuses")
        prefix = authored_prefix(result.request.scope)
        if role == "gameplay":
            document = await result.memory.read(prefix + "MEMORY.md", max_chars=8000)
            assert document is not None and STORE_NOTE_PREFIX in document.content
            assert result.executed == [{"kind": "FOUND_CITY", "target": "settler1"}]
        elif role == "economy":
            document = await result.memory.read(prefix + "city-city2.md", max_chars=8000)
            assert document is not None
            plan = CityPlan.model_validate_json(document.content)
            assert plan.city == "city2" and plan.reasons["construction"] == STORE_CITY_REASON
            assert await result.memory.read(prefix + "city-city1.md", max_chars=8000) is None
            assert result.executed == [{"kind": "QUEUE_CONSTRUCTION", "target": "city2", "selection": "Monument"}]
        elif role == "military":
            document = await result.memory.read(prefix + result.startup["nativeStoreContributionFile"], max_chars=8000)
            assert document is not None and STORE_NOTE_PREFIX in document.content
            writes = [call for call in result.invoked if call.tool_name == "write_memory"]
            assert len(writes) == 1 and "old_text" not in writes[0].args_as_dict()
        else:
            assert not set(names) & {"read_memory", "write_memory"}

    asyncio.run(run())


def test_main_notes_append_after_read_and_survive_the_next_sdk_job():
    async def run():
        memory = InMemoryStore()
        prefix = authored_prefix(request("gameplay", "first").scope)
        await memory.write(prefix + "MEMORY.md", "Prior private note.\n", expected_version=None)
        first = await exercise("gameplay", memory=memory, job="first")
        second = await exercise("gameplay", memory=memory, plans=first.plans, job="second", turn=1)
        document = await memory.read(prefix + "MEMORY.md", max_chars=8000)
        assert document is not None and document.content.startswith("Prior private note.")
        assert document.content.count(STORE_NOTE_PREFIX) == 2
        assert "gameplay, first" in document.content and "gameplay, second" in document.content
        for run in (first, second):
            names = [call.tool_name for call in run.invoked]
            assert names.index("read_memory") < names.index("write_memory")
            write = next(call for call in run.invoked if call.tool_name == "write_memory")
            assert "old_text" not in write.args_as_dict()

    asyncio.run(run())


def test_economy_replaces_one_typed_city_plan_using_the_full_native_read():
    async def run():
        memory = InMemoryStore()
        path = authored_prefix(request("economy", "first").scope) + "city-city2.md"
        original = (
            json.dumps({"type": "city_plan", "city": "city2", "reasons": {"construction": "Earlier authored intent."}})
            + "\n"
        )
        await memory.write(path, original, expected_version=None)
        result = await exercise("economy", memory=memory)
        writes = [call.args_as_dict() for call in result.invoked if call.tool_name == "write_memory"]
        assert len(writes) == 1 and writes[0]["old_text"] == original
        document = await memory.read(path, max_chars=8000)
        assert (
            document is not None
            and CityPlan.model_validate_json(document.content).reasons["construction"] == STORE_CITY_REASON
        )

    asyncio.run(run())


def test_military_skips_contribution_when_no_trusted_filename_is_supplied():
    async def run():
        result = await exercise("military", contribution=False)
        assert not {call.tool_name for call in result.invoked} & {"read_memory", "write_memory", "search_memory"}
        assert (await result.plans.get_items())[0].status is TaskStatus.completed

    asyncio.run(run())


def test_resumed_military_reads_its_existing_immutable_contribution_without_writing_again():
    async def run():
        memory = InMemoryStore()
        deps = request("military", "resumed")
        path = authored_prefix(deps.scope) + contribution_prefix(deps) + "native-stores.md"
        content = f"{STORE_NOTE_PREFIX}: military, resumed.\n"
        await memory.write(path, content, expected_version=None)
        result = await exercise("military", memory=memory, job="resumed")
        assert "read_memory" in [call.tool_name for call in result.invoked]
        assert "write_memory" not in [call.tool_name for call in result.invoked]
        document = await memory.read(path, max_chars=8000)
        assert document is not None and document.content == content

    asyncio.run(run())
