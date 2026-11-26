import asyncio
import json

from pydantic_ai import Agent, ToolOutput
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import UsageLimits

from civ_agent.economy_completion import worker_completion_problem
from civ_agent.orchestration.contracts import RoleReport
from civ_agent.orchestration.scripted_model import STORE_CITY_REASON, respond, scripted_store_setup, store_task
from civ_agent.tool_schemas import EconomyFinishRole


def messages(role, view, *replies):
    result: list[ModelRequest | ModelResponse] = [
        ModelRequest(parts=[UserPromptPart(json.dumps({"role": role, "reads": {"player_turn": view}}))])
    ]
    for index, (name, value) in enumerate((("player_turn", view), *replies)):
        call_id = "call" + str(index)
        result += [
            ModelResponse(parts=[ToolCallPart(name, {}, call_id)]),
            ModelRequest(parts=[ToolReturnPart(name, {"receipts": [{"tool": name, "result": value}]}, call_id)]),
        ]
    return result


def decision(history):
    part = respond(history, None).parts[0]
    assert isinstance(part, ToolCallPart)
    return part.tool_name, part.args_as_dict()


def test_script_uses_the_delivered_alert_command_and_research_offer():
    command = {"kind": "ACKNOWLEDGE_ALERT", "target": "alert1"}
    name, args = decision(messages("gameplay", {"alerts": [{"canAcknowledge": True, "command": command}]}))
    assert name == "execute_commands" and args["commands"] == [command]
    view = {"researchOptions": ["Pottery"], "researchQueue": []}
    assert decision(messages("gameplay", view)) == ("research_details", {"arguments": {"names": ["Pottery"]}})
    offered = {"kind": "SET_RESEARCH_QUEUE", "queue": ["Pottery"]}
    name, args = decision(messages("gameplay", view, ("research_details", {"technologies": [{"command": offered}]})))
    assert name == "execute_commands" and args["commands"] == [offered]


def test_script_reads_named_construction_before_using_its_command():
    view = {"cities": [{"handle": "city1", "constructionQueue": []}]}
    catalogue = {
        "query": {"arguments": {"target": "city1"}},
        "data": {
            "detailLevel": "catalogue",
            "constructions": [
                {"name": "Locked", "canQueue": False},
                {"name": "Monument", "canQueue": True},
            ],
        },
    }
    name, args = decision(messages("economy", view, ("construction_details", catalogue)))
    assert name == "construction_details" and args["arguments"] == {"target": "city1", "names": ["Monument"]}
    offered = {"kind": "QUEUE_CONSTRUCTION", "target": "city1", "selection": "Monument"}
    named = {"query": catalogue["query"], "data": {"detailLevel": "named", "constructions": [{"command": offered}]}}
    name, args = decision(messages("economy", view, ("construction_details", named)))
    assert name == "execute_commands" and args["commands"] == [offered]


def test_communication_stops_after_a_direct_finish_receipt():
    history = [
        ModelRequest(parts=[UserPromptPart(json.dumps({"role": "communication", "reads": {"chat_read": {}}}))]),
        ModelResponse(parts=[ToolCallPart("finish_role", {"arguments": {}}, "finish")]),
        ModelRequest(parts=[ToolReturnPart("finish_role", {"roleFinished": True}, "finish")]),
    ]
    name, _ = decision(history)
    assert name == "finish_report"


def test_script_does_not_repeat_a_confirmed_native_command_after_a_refresh():
    offered = {"kind": "ACKNOWLEDGE_ALERT", "target": "alert1"}
    view = {"alerts": [{"canAcknowledge": True, "command": offered}]}
    history = messages("gameplay", view)
    history.extend(
        [
            ModelResponse(parts=[ToolCallPart("execute_commands", {"commands": [offered]}, "native")]),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        "execute_commands",
                        {"receipts": [{"tool": "player_command", "status": "confirmed", "result": {"applied": True}}]},
                        "native",
                    )
                ]
            ),
            ModelResponse(parts=[ToolCallPart("player_turn", {"arguments": {}}, "refresh")]),
            ModelRequest(
                parts=[
                    ToolReturnPart("player_turn", {"receipts": [{"tool": "player_turn", "result": view}]}, "refresh")
                ]
            ),
        ]
    )
    assert decision(history) == ("finish_role", {"arguments": {}})


def test_script_honors_delivered_idle_worker_requirement_and_finishes_within_four_requests():
    async def run():
        view = {
            "cities": [],
            "units": [
                {
                    "handle": "worker4",
                    "name": "Worker",
                    "military": False,
                    "canBuildImprovements": True,
                    "movement": "2",
                },
                {
                    "handle": "worker2",
                    "name": "Worker",
                    "military": False,
                    "canBuildImprovements": True,
                    "movement": "2",
                },
                {
                    "handle": "working",
                    "name": "Worker",
                    "military": False,
                    "canBuildImprovements": True,
                    "movement": "2",
                    "worker": {"currentJob": "Farm"},
                },
                {"handle": "warrior", "name": "Warrior", "military": True, "movement": "2"},
            ],
        }
        completions = []

        def envelope(name, value):
            return {"receipts": [{"tool": name, "result": value}]}

        async def player_turn(arguments: dict, recoverBaseline: str = "missing") -> dict:
            return envelope("player_turn", view)

        async def finish_role(arguments: dict) -> dict:
            parsed = EconomyFinishRole.model_validate(arguments)
            completions.append(parsed)
            problem = worker_completion_problem(view, {}, parsed.model_dump()["workerSkips"])
            return envelope("finish_role", problem or {"roleFinished": True})

        agent = Agent(
            FunctionModel(respond),
            retries=0,
            output_type=ToolOutput(RoleReport, name="finish_report"),
            tools=[player_turn, finish_role],
        )
        result = await agent.run(
            json.dumps({"role": "economy", "reads": {"player_turn": view}}), usage_limits=UsageLimits(request_limit=4)
        )
        assert isinstance(result.output, RoleReport) and result.usage.requests == 4
        assert len(completions) == 2 and completions[0].workerSkips == []
        assert len(completions[1].workerSkips) == 1
        skip = completions[1].workerSkips[0]
        assert skip.targets == ["worker2", "worker4"] and skip.reason.strip()
        assert worker_completion_problem(view, {}, completions[1].model_dump()["workerSkips"]) is None

    asyncio.run(run())


def test_script_drops_prior_idle_worker_requirement_after_a_new_delivered_overview():
    view = {"cities": []}
    problem = {"error": "ECONOMY_WORKER_SKIPS_REQUIRED", "roleCompletion": {"idleWorkers": ["old-worker"]}}
    history = messages("economy", view, ("finish_role", problem), ("player_turn", view))
    assert decision(history) == ("finish_role", {"arguments": {}})


def test_script_clears_skips_when_latest_completion_feedback_has_no_idle_worker():
    problem = {"error": "ECONOMY_WORKER_SKIPS_REQUIRED", "roleCompletion": {"idleWorkers": []}}
    name, arguments = decision(messages("economy", {"cities": []}, ("finish_role", problem)))
    assert name == "finish_role"
    assert EconomyFinishRole.model_validate(arguments["arguments"]).workerSkips == []


def test_store_fixture_uses_flat_typed_city_plan_tools_only_when_advertised():
    startup = {
        "role": "economy",
        "reads": {"player_turn": {"turn": 1, "cities": [{"handle": "city1", "owner": "economy"}]}},
    }
    task = store_task(startup)
    calls = {}
    history = []

    def record(name, arguments, result):
        key = "call" + str(len(calls))
        calls[key] = ToolCallPart(name, arguments, key)
        history.append(ModelRequest(parts=[ToolReturnPart(name, result, key)]))

    record("write_plan", {"items": [{"content": task, "status": "in_progress"}]}, {})
    record("read_plan", {}, f"1. [in_progress] [abcdef12] {task}")

    def emit(name, arguments):
        return name, arguments

    assert scripted_store_setup(startup, history, calls, emit) == ("search_memory", {"query": "city-city1.md"})
    assert scripted_store_setup(startup, history, calls, emit, typed_plans=True) == (
        "read_city_plan",
        {"city": "city1"},
    )
    record("read_city_plan", {"city": "city1"}, {"existed": False})
    selected = scripted_store_setup(startup, history, calls, emit, typed_plans=True)
    assert selected is not None
    name, plan = selected
    assert name == "save_city_plan" and plan["city"] == "city1"
    assert plan["reasons"] == {"construction": STORE_CITY_REASON}
    assert not {"type", "plan", "file", "old_text", "version", "operation"} & set(plan)
    record(name, plan, {"applied": True})
    assert scripted_store_setup(startup, history, calls, emit, typed_plans=True) == (
        "read_city_plan",
        {"city": "city1"},
    )
    record("read_city_plan", {"city": "city1"}, {"content": plan})
    assert scripted_store_setup(startup, history, calls, emit, typed_plans=True) is None
