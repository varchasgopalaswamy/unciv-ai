"""Real SDK/Monty runs retain full objects after sharing their displayed copies."""

import json

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.execution_policy import ExecutionPolicy
from civ_agent.orchestration.contracts import GatewayReceipt
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.tool_output import expand_output_values
from tests.domain_fixtures import run_async
from tests.test_code_mode_catalogue import prepare
from tests.test_code_mode_runtime import candidate
from tests.test_local_agents import call, returns
from tests.test_native_player_stores import production_stores
from tests.test_tool_output_compaction import movement_packet


@pytest.mark.parametrize("shape", ["bundle", "bundle_query", "movement_options", "receipt_result"])
@run_async
async def test_full_selected_objects_survive_shared_display_and_a_later_snippet(tmp_path, shape):
    native_calls = []

    async def invoke(_deps, identifier, _step, decision):
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        native_calls.append(decision)
        packet = movement_packet()
        packet["decisionId"] = identifier
        packet["receipts"][0]["operationId"] = identifier + ":0"
        return GatewayReceipt(result=packet)

    query = "{'tool':'unit_details','arguments':{'target':'unit1','sections':['movement']}}"
    if shape in {"bundle", "bundle_query"}:
        program = f"packet = inspect_bundle(queries=[{query}])\n"
        program += (
            "selected = packet\n"
            if shape == "bundle"
            else (
                "query_id = packet['receiptEntries'][0]['queryId']\nassert isinstance(query_id, str)\n"
                "selected = bundle_query(packet, query_id=query_id)\n"
            )
        )
        later = "len(selected['receiptEntries'])" if shape == "bundle" else "len(selected['bundle']['receiptEntries'])"
        expected = 1
    else:
        program = "packet = unit_details(target='unit1',sections=['movement'])\n"
        if shape == "movement_options":
            program += "selected = movement_options(packet)\n"
            later, expected = "len(selected['options'])", 12
        else:
            program += (
                "operation_id = packet['receipts'][0]['operationId']\nassert isinstance(operation_id, str)\n"
                "selected = receipt_result(packet, operation_id=operation_id)\n"
            )
            later, expected = "len(selected['feedback']['receipts'])", 1

    def model(messages, info):
        history = returns(messages)
        if not history:
            return call(info, "run_code", {"code": program + "selected"}, "display")
        if len(history) == 1:
            content = history[-1].content
            assert isinstance(content, dict) and "sharedValues" in content
            expanded = expand_output_values(content)
            assert len(json.dumps(content)) < len(json.dumps(expanded))
            return call(info, "run_code", {"code": later}, "retained")
        assert history[-1].content == expected
        return call(info, "finish_role", {"summary": "Inspected without replaying native reads."}, "finish")

    catalogue = [{"name": name, "inputSchema": {}} for name in ["finish_role", "unit_details", "inspect_bundle"]]
    steps = SqliteStepStore(database=tmp_path / "steps.sqlite3")
    result = await LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke,
            step_store=steps,
            overflow_root=tmp_path / "overflow",
        )
    ).run_role(
        candidate().model_copy(update={"execution_policy": ExecutionPolicy.workflows(code_mode=True)}),
        startup={"user": "Inspect the advertised unit.", "instructions": "Use permitted tools."},
        catalogue=catalogue,
        model=FunctionModel(model),
    )
    assert len(native_calls) == 1
    first = returns(result.messages)[0]
    assert all("sharedValues" not in child.content for child in first.metadata["tool_returns"].values())


@run_async
async def test_native_planning_display_does_not_inject_the_same_checklist_twice(tmp_path):
    async with production_stores(tmp_path) as fixture:
        request = (await prepare(fixture, "gameplay")).model_copy(
            update={"execution_policy": ExecutionPolicy.workflows(code_mode=True)}
        )

        async def invoke(_deps, _identifier, _step, decision):
            assert decision["kind"] == "finish"
            return GatewayReceipt(result={"roleFinished": True})

        def model(messages, info):
            if not returns(messages):
                return call(
                    info,
                    "run_code",
                    {
                        "code": (
                            "plan = write_plan(items=[{'id':'one','content':'Keep exact authored wording?','status':'in_progress'}])\nplan"
                        )
                    },
                    "plan",
                )
            content = returns(messages)[-1].content
            assert "<plan_reminder>" in str(content)
            injected = [
                part
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
                for item in part.content
                if "<plan_reminder>" in str(item)
            ]
            assert not injected, "Code already returned the current complete plan."
            return call(info, "finish_role", {"summary": "Recorded the role's plan."}, "finish")

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
            catalogue=fixture.store.get(request.scope, request.catalogue),
            model=FunctionModel(model),
        )
