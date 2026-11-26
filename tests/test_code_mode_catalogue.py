"""Real role catalogues stay complete and role-scoped after SDK function generation."""

import ast
import re

import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.decision_contracts import DecisionContract
from civ_agent.execution_policy import ExecutionPolicy
from civ_agent.gateway_batches import BatchExecutor
from civ_agent.orchestration.authored_memory import authored_prefix
from civ_agent.orchestration.authored_plan_tools import (
    AUTHORED_PLAN_TOOLS,
    CITY_MUTATION_TOOLS,
    PLAN_READ_TOOLS,
    STRATEGY_MUTATION_TOOLS,
)
from civ_agent.orchestration.contracts import (
    GatewayCall,
    GatewayJob,
    GatewayReceipt,
    ModelSelection,
    PrepareRole,
    RoleRequest,
)
from civ_agent.orchestration.gateway_state import capture_job
from civ_agent.orchestration.gateway_tools import gateway_tools
from civ_agent.orchestration.player_plans import PLAN_TOOLS
from civ_agent.orchestration.store import JournalConflict
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.code_mode import CheckedCodeMode
from civ_agent.runtime.code_mode_results import RESULT_HELPERS
from civ_agent.schemas import BoundaryModel
from tests.domain_fixtures import run_async
from tests.memory_helpers import stored_records
from tests.test_code_mode_runtime import candidate
from tests.test_local_agents import call, returns
from tests.test_native_player_stores import production_stores


def generated_functions(info):
    code_tool = next(tool for tool in info.function_tools if tool.name == "run_code")
    for source in re.findall(r"```python\n(.*?)```", code_tool.description or "", re.S):
        try:
            ast.parse(source)
        except SyntaxError as error:
            lines = source.splitlines()
            location = error.lineno or 1
            raise AssertionError(
                "Invalid generated catalogue:\n" + "\n".join(lines[max(0, location - 8) : location + 2])
            ) from error
    return {
        name: bool(asynchronous)
        for asynchronous, name in re.findall(
            r"^(async )?def ([A-Za-z_][A-Za-z_0-9]*)\(", code_tool.description or "", re.M
        )
    }


async def prepare(fixture, purpose):
    if purpose == "communication":
        scope = fixture.ticket.scope.model_copy(update={"activation": "communication-code"})
        identity = GatewayJob(scope=scope, job="communication-code", purpose="communication")
        job = await fixture.root.gateway.open(identity.job, identity.purpose, scope.epoch, fixture.host, {}, 0)
        catalogue = await fixture.root.gateway.tools(job)
        catalogue.append({"name": "finish_role", "inputSchema": BoundaryModel.model_json_schema()})
        job.catalogue = catalogue
        access = BatchExecutor(fixture.root.gateway, job, DecisionContract(purpose, catalogue)).delivered_access
        await fixture.root.registry.register(identity, capture_job(job, delivered_access=access), catalogue)
        return RoleRequest(
            **identity.model_dump(),
            execution_policy=ExecutionPolicy.current(),
            typed_authored_plans=True,
            selection=ModelSelection(model="test"),
            startup=fixture.store.put(
                scope, {"user": "Handle private messages.", "instructions": "Use permitted tools."}
            ),
            catalogue=fixture.store.put(scope, catalogue),
        )
    main = await fixture.root.prepare_role(PrepareRole(ticket=fixture.ticket, purpose="gameplay"))
    assert main is not None
    if purpose == "gameplay":
        request = main
    else:
        fixture.root.turn_roles.ready[purpose] = False
        request = await fixture.root.prepare_role(PrepareRole(ticket=fixture.ticket, purpose=purpose))
        assert request is not None
    return request.model_copy(update={"execution_policy": ExecutionPolicy.current()})


@pytest.mark.parametrize("purpose", ["gameplay", "economy", "military", "communication"])
@run_async
async def test_real_role_catalogue_memory_and_planning_have_generated_functions(tmp_path, purpose):
    async with production_stores(tmp_path) as fixture:
        request = await prepare(fixture, purpose)
        catalogue = fixture.store.get(request.scope, request.catalogue)
        ordinary = [entry for entry in catalogue if entry["name"] not in {"finish_role", "player_command"}]

        async def invoke(*_args):
            return GatewayReceipt(result={"roleFinished": True})

        direct = {tool.name for tool in gateway_tools(purpose, ordinary, invoke, include_controls=True)}

        def respond(_messages, info):
            generated = generated_functions(info)
            assert {tool.name for tool in info.function_tools} == {"run_code"}
            assert [tool.name for tool in info.output_tools] == ["finish_role"]
            assert "finish_role" not in generated
            assert RESULT_HELPERS <= generated.keys()
            assert not any(generated[name] for name in RESULT_HELPERS)
            assert direct <= set(generated)
            if purpose != "communication":
                assert {"execute_commands", "inspect_bundle", "command_help"} <= set(generated)
            else:
                assert not {"execute_commands", "inspect_bundle", "command_help"} & set(generated)
            assert set(PLAN_TOOLS) <= set(generated)
            allowed_plans = PLAN_READ_TOOLS
            if purpose in {"gameplay", "economy"}:
                allowed_plans |= CITY_MUTATION_TOOLS
            if purpose == "gameplay":
                allowed_plans |= STRATEGY_MUTATION_TOOLS
            assert set(generated) & AUTHORED_PLAN_TOOLS == allowed_plans
            assert {"read_memory", "search_memory"} <= set(generated)
            assert ("write_memory" in generated) == (purpose != "economy")
            assert ("delete_memory" in generated) == (purpose != "economy")
            return call(info, "finish_role", {"summary": "Catalogue parity verified."}, "finish")

        result = await LocalAgentRunner(
            LocalAgentServices(
                invoke=invoke,
                gateway=fixture.gateway,
                step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
                overflow_root=tmp_path / "overflow",
            )
        ).run_role(
            request,
            startup=fixture.store.get(request.scope, request.startup),
            catalogue=catalogue,
            model=FunctionModel(respond),
        )
        assert result.usage.requests == 1


@run_async
async def test_inspection_bundle_and_help_execute_with_canonical_nested_validation(tmp_path):
    async with production_stores(tmp_path) as fixture:
        request = await prepare(fixture, "gameplay")
        catalogue = fixture.store.get(request.scope, request.catalogue)
        dispatched = []

        async def invoke(_deps, identifier, step, decision):
            dispatched.append(decision)
            if decision["kind"] == "finish":
                return GatewayReceipt(result={"roleFinished": True})
            return GatewayReceipt(
                result={"decisionId": identifier, "receipts": [{"status": "confirmed", "result": {"turn": 0}}]}
            )

        def respond(messages, info):
            if not returns(messages):
                return call(
                    info,
                    "run_code",
                    {
                        "code": (
                            "rejected = False\n"
                            "try:\n"
                            "    inspect_bundle(queries=[{'tool': 'city_details', 'arguments': {'target': ''}}])\n"
                            "except Exception:\n"
                            "    rejected = True\n"
                            "assert rejected\n"
                            "bundle = inspect_bundle(queries=[{'tool': 'player_turn', 'arguments': {}}])\n"
                            "assert bundle['results'][0]['result']['turn'] == 0\n"
                            "help = command_help(kinds=['SET_RESEARCH_QUEUE'])\n"
                            "assert 'inputSchema' in help\n"
                            "denied = command_help(kinds=['NOT_A_NATIVE_COMMAND'])\n"
                            "assert denied['error'] == 'COMMAND_NOT_PERMITTED'\n"
                            "None"
                        )
                    },
                    "controls",
                )
            metadata = returns(messages)[-1].metadata
            assert metadata["tool_calls"]["controls__1"].tool_name == "inspect_bundle"
            assert "controls__1" not in metadata["tool_returns"]
            assert set(metadata["tool_returns"]) == {"controls__2", "controls__3", "controls__4"}
            return call(info, "finish_role", {"summary": "Nested validation retained."}, "finish")

        await LocalAgentRunner(
            LocalAgentServices(
                invoke=invoke,
                step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
                overflow_root=tmp_path / "overflow",
            )
        ).run_role(
            request,
            startup=fixture.store.get(request.scope, request.startup),
            catalogue=catalogue,
            model=FunctionModel(respond),
        )
        assert len(dispatched) == 2
        assert dispatched[0] == {"kind": "inspect", "operations": [{"tool": "player_turn", "arguments": {}}]}


@run_async
async def test_all_memory_and_planning_functions_use_real_stores_inside_one_program(tmp_path):
    async with production_stores(tmp_path) as fixture:
        request = await prepare(fixture, "gameplay")
        catalogue = fixture.store.get(request.scope, request.catalogue)
        invocations = [
            ("read_strategy", {}),
            ("save_strategy", {"assessment": "Develop before expanding."}),
            ("read_strategy", {}),
            ("delete_strategy", {}),
            ("read_city_plan", {"city": "city1"}),
            ("save_city_plan", {"city": "city1", "reasons": {"construction": "Grow the city."}}),
            ("read_city_plan", {"city": "city1"}),
            ("delete_city_plan", {"city": "city1"}),
            ("write_memory", {"file": "note-code.md", "content": "Growth remains an authored assessment."}),
            ("read_memory", {"file": "note-code.md"}),
            ("search_memory", {"query": "Growth"}),
            ("delete_memory", {"file": "note-code.md"}),
            (
                "write_plan",
                {
                    "items": [
                        {"id": "first", "content": "Inspect queues", "status": "pending"},
                        {"id": "second", "content": "Choose construction", "status": "pending"},
                    ]
                },
            ),
            ("read_plan", {}),
            ("add_task", {"content": "Check research"}),
            ("add_subtask", {"parent_id": "first", "content": "Inspect capital"}),
            ("set_dependency", {"task_id": "second", "depends_on_id": "first"}),
            ("get_available_tasks", {}),
            ("update_task_status", {"task_id": "first", "status": "completed"}),
            ("update_task_statuses", {"updates": [{"task_id": "second", "status": "completed"}]}),
            ("remove_task", {"task_id": "first"}),
            ("read_role_plan", {"role": "military"}),
        ]

        async def invoke(*_args):
            return GatewayReceipt(result={"roleFinished": True})

        def respond(messages, info):
            if not returns(messages):
                generated = generated_functions(info)
                lines = []
                for index, (name, arguments) in enumerate(invocations):
                    arguments = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
                    expression = ("await " if generated[name] else "") + f"{name}({arguments})"
                    lines.append(f"result_{index} = {expression}")
                    if name in {"save_strategy", "delete_strategy", "save_city_plan", "delete_city_plan"}:
                        lines.append(f"assert result_{index}['applied'] is True")
                    if name == "read_memory":
                        lines.append(f"assert 'Growth' in result_{index}")
                lines.append("None")
                return call(info, "run_code", {"code": "\n".join(lines)}, "authored-program")
            returned = returns(messages)[-1]
            assert returned.outcome == "success"
            nested = returned.metadata["tool_returns"]
            assert [result.tool_name for result in nested.values()] == [name for name, _ in invocations]
            assert all(result.outcome == "success" for result in nested.values())
            return call(info, "finish_role", {"summary": "Authored tools preserved scoped stores."}, "finish")

        await LocalAgentRunner(
            LocalAgentServices(
                invoke=invoke,
                gateway=fixture.gateway,
                step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
                overflow_root=tmp_path / "overflow",
            )
        ).run_role(
            request,
            startup=fixture.store.get(request.scope, request.startup),
            catalogue=catalogue,
            model=FunctionModel(respond),
        )
        identity = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
        tasks = await fixture.plans.store(identity).get_items()
        assert {task.content for task in tasks} == {"Choose construction", "Check research"}
        assert next(task for task in tasks if task.id == "second").status.value == "completed"
        prefix = authored_prefix(request.scope)
        assert await fixture.authored.store.read(prefix + "note-code.md", max_chars=8000) is None


@run_async
async def test_nested_typed_plan_sources_reach_real_native_command_accounting(tmp_path):
    async with production_stores(tmp_path, native_commands=True) as fixture:
        request = await prepare(fixture, "gameplay")
        catalogue = fixture.store.get(request.scope, request.catalogue)
        identity = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
        path = fixture.store.root / "agent-steps.sqlite3"
        path.touch(mode=0o600)

        async def invoke(deps, identifier, step, decision):
            if decision["kind"] == "finish":
                return GatewayReceipt(result={"roleFinished": True})
            return await fixture.gateway(deps, identifier, step, decision)

        async def execute(_deps, run_id, identifier, step, operations):
            executed = await fixture.commands.execute_local(
                identity,
                GatewayCall(call_id=identifier, model_step=step, decision={"kind": "act", "operations": operations}),
                run_id=run_id,
            )
            return executed.result

        async def delivered(_deps, identifier, step):
            await fixture.root.registry.acknowledge_delivery(identity, identifier, step)

        def respond(messages, info):
            if not returns(messages):
                return call(
                    info,
                    "run_code",
                    {
                        "code": (
                            "city_details(target='city1')\n"
                            "await read_city_plan(city='city1')\n"
                            "saved = await save_city_plan(city='city1', reasons={'focus': 'Grow before production.'})\n"
                            "assert saved['applied']\n"
                            "await read_city_plan(city='city1')\n"
                            "outcome = execute_commands(commands=[{'kind': 'SET_CITY_FOCUS', 'target': 'city1', "
                            "'selection': 'FoodFocus', 'intent': {'cityPlan': 'city-city1.md'}}])\n"
                            "assert outcome['receipts'][0]['status'] == 'confirmed'\nNone"
                        )
                    },
                    "source-program",
                )
            returned = returns(messages)[-1]
            assert returned.outcome == "success", str(returned.content)
            return call(info, "finish_role", {"summary": "Native receipt links the delivered authored plan."}, "finish")

        result = await LocalAgentRunner(
            LocalAgentServices(
                invoke=invoke,
                execute_commands=execute,
                on_observation_delivered=delivered,
                gateway=fixture.gateway,
                step_store=SqliteStepStore(database=path),
                overflow_root=tmp_path / "overflow",
            )
        ).run_role(
            request,
            startup=fixture.store.get(request.scope, request.startup),
            catalogue=catalogue,
            model=FunctionModel(respond),
        )
        assert result.usage.requests == 2 and len(fixture.host.executed) == 1
        decisions = [
            record["body"]
            for record in stored_records(fixture.root.memory).values()
            if record["category"] == "decision" and record["body"].get("native")
        ]
        source = decisions[-1]["_pinned"]["cityPlan"]["source"]
        assert source["runId"] == result.run_id and source["toolCallId"] == "source-program__4"
        assert "unverifiedCityPlan" not in decisions[-1]["_pinned"]


@pytest.mark.parametrize(
    "names",
    [
        ("run-code",),
        ("a-b", "a_b"),
        ("class", "class_"),
        ("1tool", "_1tool"),
        ("result_data",),
        ("result-data",),
        ("result_feedback",),
        ("required_result_field",),
        ("result_entity",),
        ("_cm_version",),
    ],
)
@run_async
async def test_reserved_and_sanitized_collisions_fail_before_model_dispatch(names):
    requested = []

    def respond(_messages, _info):
        requested.append(True)
        return ModelResponse(parts=[TextPart("Cannot be reached.")])

    def tool():
        return {}

    agent = Agent(
        FunctionModel(respond),
        tools=[Tool(tool, name=name) for name in names],
        capabilities=[CheckedCodeMode()],
    )
    with pytest.raises(JournalConflict, match="CODE_MODE_CATALOGUE_COLLISION"):
        await agent.run("Prepare the tool catalogue.")
    assert requested == []


@run_async
async def test_missing_generated_function_fails_preparation_before_model_request():
    requested = []

    def respond(_messages, _info):
        requested.append(True)
        return ModelResponse(parts=[TextPart("Cannot be reached.")])

    agent = Agent(FunctionModel(respond), capabilities=[CheckedCodeMode(required=frozenset({"player_turn"}))])
    with pytest.raises(JournalConflict, match="CODE_MODE_CATALOGUE_INCOMPLETE"):
        await agent.run("Prepare the tool catalogue.", deps=candidate())
    assert requested == []
