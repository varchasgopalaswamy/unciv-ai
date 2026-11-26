"""Real SDK role loops use native completion checks and queued command receipts."""

import copy
import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.decision_contracts import DecisionContract
from civ_agent.execution_policy import ExecutionPolicy
from civ_agent.gateway_batches import BatchExecutor
from civ_agent.military_schedule import MilitarySchedule
from civ_agent.orchestration.contracts import GatewayReceipt, ModelSelection, PlayerRole, Reference, RoleRequest, Scope
from civ_agent.orchestration.gateway_execution import NativeGatewayExecutor, operation_identity
from civ_agent.orchestration.gateway_state import capture_job
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from tests.domain_fixtures import managed, run_async


def request(purpose: PlayerRole = "gameplay", *, job="local-job"):
    return RoleRequest(
        scope=Scope(game="game", player="player", epoch="g1", activation="turn"),
        job=job,
        purpose=purpose,
        startup=Reference(sha256="a" * 64),
        catalogue=Reference(sha256="b" * 64),
        selection=ModelSelection(model="test"),
        typed_authored_plans=True,
    )


def call(info, name, args, identifier):
    tool = next(tool for tool in [*info.function_tools, *info.output_tools] if tool.name == name)
    Draft202012Validator(tool.parameters_json_schema).validate(args)
    return ModelResponse(parts=[ToolCallPart(name, args, identifier)])


def returns(messages):
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


@run_async
async def test_omitted_request_review_date_gets_its_null_wire_default_before_native_completion(tmp_path):
    calls = []
    catalogue = [{"name": "finish_role", "inputSchema": {}}]
    contract = DecisionContract("economy", catalogue)

    def model(messages, info):
        # The runtime model explicitly accepts omission for compatibility, even
        # though the strict canonical wire shape requires the nullable key.
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "finish_role",
                    {
                        "summary": "Completed the production request.",
                        "requests": [
                            {
                                "id": "request1",
                                "status": "completed",
                                "reason": "Retained the chosen native construction.",
                            }
                        ],
                    },
                    "finish-with-default",
                )
            ]
        )

    async def invoke(deps, identifier, step, decision):
        parsed = contract.parse(contract.encode(decision))
        assert parsed["arguments"]["requests"][0]["reviewTurn"] is None
        calls.append(parsed)
        return GatewayReceipt(result={"roleFinished": True})

    runner = LocalAgentRunner(
        LocalAgentServices(invoke=invoke, step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"))
    )
    result = await runner.run_role(
        request("economy"),
        startup={"user": "Complete the request.", "instructions": "Use native tools."},
        catalogue=catalogue,
        model=FunctionModel(model),
    )
    assert result.usage.requests == 1 and len(calls) == 1


@pytest.mark.parametrize("purpose", ["gameplay", "economy", "military", "communication"])
@run_async
async def test_single_terminal_output_has_only_its_role_fields(tmp_path, purpose):
    calls = []

    async def invoke(deps, identifier, step, decision):
        calls.append((deps, identifier, step, decision))
        return GatewayReceipt(result={"roleFinished": True})

    def model(messages, info):
        assert not any(tool.name in {"finish_role", "finish_report"} for tool in info.function_tools)
        assert [tool.name for tool in info.output_tools] == ["finish_role"]
        schema = info.output_tools[0].parameters_json_schema
        fields = schema["properties"]
        assert "summary" in fields
        assert ("deferChoices" in fields) == (purpose == "gameplay")
        assert ("escalateChoices" in fields) == (purpose == "economy")
        assert ("decisions" in fields) == (purpose == "military")
        return call(info, "finish_role", {"summary": "Reviewed the current position."}, "finish1")

    store = SqliteStepStore(database=tmp_path / "steps.sqlite3")
    runner = LocalAgentRunner(LocalAgentServices(invoke=invoke, step_store=store))
    result = await runner.run_role(
        request(purpose),
        startup={"user": "Review this position.", "instructions": "Use native tools."},
        catalogue=[{"name": "finish_role", "inputSchema": {}}],
        model=FunctionModel(model),
    )
    assert result.report.summary == "Reviewed the current position."
    assert result.usage.requests == 1 and len(calls) == 1
    assert calls[0][3] == {"kind": "finish", "arguments": {}}
    snapshot = await store.latest_snapshot(run_id=result.run_id)
    assert snapshot is not None and "finish_role" in json.dumps(snapshot.messages, default=str)


@run_async
async def test_native_required_choice_retries_output_then_commits_queued_action(tmp_path):
    async with managed(tmp_path) as m:
        job = m.main
        job.memory["nativeRoleContract"] = 1
        m.host.view.update(canEndTurn=False, pending=[{"kind": "Research", "supported": True}])
        await m.gateway.call(job, "player_turn", {}, "local-start")
        catalogue = await m.gateway.tools(job)
        contract = DecisionContract(job.purpose, catalogue)
        state = capture_job(job, delivered_access=BatchExecutor(m.gateway, job, contract).delivered_access)
        executor = NativeGatewayExecutor(m.gateway, job, contract, state)
        original = m.host.tool

        async def host(name, args):
            value = await original(name, args)
            if name == "player_command" and args["kind"] == "SET_RESEARCH_QUEUE":
                m.host.view.update(canEndTurn=True, pending=[])
            return value

        m.host.tool = host
        rejected = []

        async def invoke(_request, identifier, step, decision):
            result = await executor.execute(identifier, decision, model_step=step)
            if decision["kind"] == "finish" and not m.roles.ready["gameplay"]:
                rejected.append(copy.deepcopy(result.result))
                assert not job.role_finished.is_set()
            return GatewayReceipt(result=result.result)

        def model(messages, info):
            history = returns(messages)
            if not history:
                return call(info, "finish_role", {"summary": "Early finish must be rejected."}, "early")
            if len(history) == 1:
                assert history[0].outcome == "retried"
                assert "NATIVE_ROLE_DECISIONS_REQUIRED" in str(history[0].content)
                return call(
                    info,
                    "execute_commands",
                    {"commands": [{"kind": "SET_RESEARCH_QUEUE", "queue": ["Pottery"]}]},
                    "research",
                )
            assert len(m.host.executed) == 1
            return call(info, "finish_role", {"summary": "Selected Pottery research."}, "finished")

        store = SqliteStepStore(database=tmp_path / "steps.sqlite3")
        runner = LocalAgentRunner(LocalAgentServices(invoke=invoke, step_store=store))
        result = await runner.run_role(
            request(job=job.id),
            startup={"user": "Choose research.", "instructions": "Only code advances the turn."},
            catalogue=catalogue,
            model=FunctionModel(model),
        )
        assert result.usage.requests == 3 and len(rejected) == 1
        assert m.roles.ready["gameplay"] and job.role_finished.is_set()
        assert len(m.host.executed) == 1
        assert m.host.executed[0]["kind"] == "SET_RESEARCH_QUEUE"
        effect = await store.get_tool_effect(run_id=result.run_id, tool_call_id="research")
        assert effect is not None and effect.status == "completed"
        assert effect.idempotency_key == operation_identity(job.id, job.epoch, 2, "research")


@run_async
async def test_military_completion_feedback_corrects_extra_roster_target_without_repeating_review_or_actions(tmp_path):
    async with managed(tmp_path) as m:
        exposed = m.host.view["map"]["units"][0]
        exposed["nearbyHostiles"] = [{"distance": 1}]
        quiet = copy.deepcopy(exposed)
        quiet.update(handle="unit2", nearbyHostiles=[])
        m.host.view["map"]["units"].append(quiet)
        m.host.view["diplomacy"] = {"knownCivilizations": [], "cityStates": []}
        job = await m.gateway.open("military", "military", "g1", m.host, {}, 0)
        m.roles.register(job)
        m.roles.military_schedule = MilitarySchedule(m.memory)
        m.roles.resume("military", "Review the exposed unit.")
        opening = await m.gateway.call(job, "player_turn", {}, "military-start")
        catalogue = await m.gateway.tools(job)
        contract = DecisionContract("military", catalogue)
        state = capture_job(job, delivered_access=BatchExecutor(m.gateway, job, contract).delivered_access)
        executor = NativeGatewayExecutor(m.gateway, job, contract, state)
        rejected = []

        async def invoke(_request, identifier, step, decision):
            result = await executor.execute(identifier, decision, model_step=step)
            if decision["kind"] == "finish" and not m.roles.ready["military"]:
                rejected.append(copy.deepcopy(result.result))
                assert not job.role_finished.is_set()
            return GatewayReceipt(result=result.result)

        def model(messages, info):
            history = returns(messages)
            if not history:
                return call(info, "run_code", {"code": "military_review()"}, "review")
            if len(history) == 1:
                return call(
                    info,
                    "finish_role",
                    {
                        "summary": "Hold both military units.",
                        "decisions": [{"targets": ["unit1", "unit2"], "reason": "Hold current positions."}],
                    },
                    "overbroad-finish",
                )
            assert len(history) == 2 and history[-1].outcome == "retried"
            feedback = rejected[-1]
            assert feedback["error"] == "MILITARY_REVIEW_NOT_DELIVERED"
            assert feedback["deliveredTargets"] == ["unit1"]
            assert feedback["undeliveredTargets"] == ["unit2"]
            assert [item["target"] for item in feedback["militaryCompletion"]["items"]] == ["unit1"]
            assert "omit it from decisions" in feedback["recovery"]["instruction"]
            assert not m.host.executed
            return call(
                info,
                "finish_role",
                {
                    "summary": "Accept the exposed unit's risk. Keep the quiet unit in reserve.",
                    "decisions": [{"targets": ["unit1"], "reason": "Hold and accept the current exposure."}],
                },
                "corrected-finish",
            )

        runner = LocalAgentRunner(
            LocalAgentServices(
                invoke=invoke,
                step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
                overflow_root=tmp_path / "overflow",
            )
        )
        result = await runner.run_role(
            request("military", job=job.id).model_copy(update={"execution_policy": ExecutionPolicy.current()}),
            startup={
                "user": json.dumps({"role": "military", "reads": {"player_turn": opening}}),
                "instructions": "Use permitted tools.",
            },
            catalogue=catalogue,
            model=FunctionModel(model),
        )
        assert result.usage.requests == 3 and len(rejected) == 1
        assert m.roles.ready["military"] and job.role_finished.is_set()
        assert not m.host.executed


@run_async
async def test_catalogue_is_verified_once_per_run_and_each_restart_has_fresh_history(tmp_path):
    seen = []
    catalogue = [
        {"name": "finish_role", "inputSchema": {}},
        {"name": "player_turn", "inputSchema": {"type": "object", "properties": {}}},
    ]

    class Gateway:
        async def catalogue(self, _request):
            seen.append("catalogue")
            return SimpleNamespace(catalogue=catalogue)

        async def post(self, route, body):
            if route == "plans":
                return {"items": []}
            assert route == "memory"
            action = body.call.action
            return {"result": [] if action == "list_paths" else None}

    async def invoke(*_):
        return GatewayReceipt(result={"roleFinished": True})

    def model(messages, info):
        history = returns(messages)
        if not history:
            return call(info, "player_turn", {}, "read")
        assert len(history) == 1
        return call(info, "finish_role", {"summary": "Fresh review."}, "finish")

    services = LocalAgentServices(
        invoke=invoke, step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"), gateway=Gateway()
    )
    runner = LocalAgentRunner(services)
    # The immutable catalogue verification is independent of optional memory
    # services; the second run must neither inherit history nor reuse a run ID.
    outputs = []
    for _ in range(2):
        outputs.append(
            await runner.run_role(
                request(),
                startup={"user": "Fresh.", "instructions": "Review."},
                catalogue=catalogue,
                model=FunctionModel(model),
            )
        )
    assert outputs[0].run_id != outputs[1].run_id
    assert seen == ["catalogue", "catalogue"]
