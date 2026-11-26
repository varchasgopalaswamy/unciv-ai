"""Dependency-free scripted Code Mode acceptance probe for runtime images.

Run using the image's Python interpreter; this file needs neither pytest nor a
provider account. Its dispatcher records simulated effects in a fresh temp dir.
"""

import asyncio
import json
import tempfile
from pathlib import Path

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models import override_allow_model_requests
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.execution_policy import ExecutionPolicy
from civ_agent.game_commands import COMMANDS
from civ_agent.monty_runtime import verify_runtime
from civ_agent.orchestration.contracts import GatewayReceipt, ModelSelection, Reference, RoleRequest, Scope
from civ_agent.orchestration.store import JournalConflict
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.code_mode_results import RESULT_HELPERS


def returns(messages):
    return [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]


async def probe():
    root = Path(tempfile.mkdtemp(prefix="container-code-mode-"))
    request = RoleRequest(
        scope=Scope(game="container-probe", player="p0", epoch="fresh", activation="probe"),
        job="probe",
        purpose="gameplay",
        startup=Reference(sha256="a" * 64),
        catalogue=Reference(sha256="b" * 64),
        selection=ModelSelection(model="function"),
        execution_policy=ExecutionPolicy.current(),
    )
    schema = COMMANDS.json_schema()
    schema["discriminator"]["mapping"] = {
        "SET_RESEARCH_QUEUE": schema["discriminator"]["mapping"]["SET_RESEARCH_QUEUE"]
    }
    catalogue = [
        {"name": "finish_role", "inputSchema": {}},
        {"name": "player_turn", "inputSchema": {}},
        {"name": "player_command", "inputSchema": schema},
    ]
    effects = []

    async def invoke(_deps, identifier, _step, decision):
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        if decision["kind"] == "inspect":
            return GatewayReceipt(
                result={
                    "decisionId": identifier,
                    "unexecuted": [],
                    "receipts": [
                        {
                            "operationId": identifier + ":0",
                            "tool": "player_turn",
                            "status": "confirmed",
                            "result": {"large": "x" * 30000},
                        }
                    ],
                }
            )
        effects.append(identifier)
        return GatewayReceipt(
            result={
                "decisionId": identifier,
                "unexecuted": [],
                "receipts": [
                    {
                        "operationId": identifier + ":0",
                        "tool": "player_command",
                        "status": "confirmed",
                        "result": {"applied": True},
                    }
                ],
            }
        )

    def model(messages, _info):
        if not returns(messages):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "run_code",
                        {
                            "code": "view = player_turn()\nassert len(view['receipts'][0]['result']['large']) == 30000\n"
                            "execute_commands(commands=[{'kind': 'SET_RESEARCH_QUEUE', 'queue': ['Pottery']}])\n"
                            "view"
                        },
                        "parent",
                    )
                ]
            )
        result = returns(messages)[-1]
        assert len(str(result.content)) < 5000 and "spill_" in str(result.content)
        assert "action_outcomes" in str(result.content) and "confirmed" in str(result.content)
        return ModelResponse(parts=[ToolCallPart("finish_role", {"summary": "Validated."}, "finish")])

    store = SqliteStepStore(database=root / "steps.sqlite3")
    runner = LocalAgentRunner(LocalAgentServices(invoke=invoke, step_store=store, overflow_root=root / "overflow"))
    result = await runner.run_role(
        request,
        startup={"user": "Scripted probe.", "instructions": "Use the frozen permitted tools."},
        catalogue=catalogue,
        model=FunctionModel(model),
    )
    assert effects == ["parent__2"]
    effect = await store.get_tool_effect(run_id=result.run_id, tool_call_id="parent__2")
    assert effect is not None and effect.status == "completed"

    dispatched = []

    async def fatal(_deps, identifier, _step, _decision):
        dispatched.append(identifier)
        raise JournalConflict("PROBE_UNCERTAIN_EFFECT")

    def fatal_model(_messages, _info):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "run_code",
                    {"code": "try:\n    player_turn()\nexcept Exception:\n    pass\nplayer_turn()"},
                    "fatal",
                )
            ]
        )

    runner = LocalAgentRunner(LocalAgentServices(invoke=fatal, step_store=store, overflow_root=root / "fatal-overflow"))
    try:
        await runner.run_role(
            request,
            startup={"user": "Scripted fatal probe.", "instructions": "Use tools."},
            catalogue=catalogue[:2],
            model=FunctionModel(fatal_model),
        )
    except JournalConflict as error:
        assert str(error) == "PROBE_UNCERTAIN_EFFECT"
    else:
        raise AssertionError("Fatal outcome escaped the fence")
    assert dispatched == ["fatal__1"]

    # Exercise the actual generated Monty helper signatures and correction
    # boundary. No native host, account, network or external model is involved.
    helper_dispatches, helper_deliveries = [], []

    async def helper_invoke(_deps, identifier, _step, decision):
        helper_dispatches.append((identifier, decision["kind"]))
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        assert decision["kind"] == "inspect" and identifier == "helper-wrong__1", (
            "A pure result helper attempted an extra native read or observation recovery"
        )
        return GatewayReceipt(
            result={
                "decisionId": identifier,
                "receipts": [
                    {
                        "operationId": identifier + ":0",
                        "tool": "player_turn",
                        "status": "confirmed",
                        "result": {"militaryCompletion": {"items": [{"target": "unit1"}], "total": 1}},
                    }
                ],
                "unexecuted": [],
            }
        )

    async def helper_delivered(_deps, identifier, _step):
        helper_deliveries.append(identifier)

    fake_observation = {
        "stateUpdate": {"kind": "unchanged"},
        "receipts": [
            {
                "operationId": "local-only",
                "tool": "player_command",
                "status": "confirmed",
                "result": {"kind": "MOVE_UNIT"},
            }
        ],
    }

    def helper_model(messages, info):
        instructions = info.instructions or ""
        assert "# Code Mode execution" in instructions and "# Code Mode role completion" in instructions
        advertised = next(tool for tool in info.function_tools if tool.name == "run_code").description or ""
        assert all("def " + name + "(" in advertised for name in RESULT_HELPERS)
        history = returns(messages)
        if not history:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "run_code",
                        {
                            "code": "packet = player_turn()\nrequired_result_field(data=packet, key='militaryCompletion')"
                        },
                        "helper-wrong",
                    )
                ]
            )
        if len(history) == 1:
            assert history[-1].outcome == "retried" and "RESULT_FIELD_MISSING" in str(history[-1].content)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "run_code",
                        {
                            "code": "data = result_data(result=packet)\nassert isinstance(data, dict)\n"
                            "required_result_field(data=data, key='militaryCompletion')"
                        },
                        "helper-correct",
                    )
                ]
            )
        if len(history) == 2:
            assert history[-1].content == {"items": [{"target": "unit1"}], "total": 1}
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "run_code",
                        {"code": "required_result_field(data={'local': " + repr(fake_observation) + "}, key='local')"},
                        "helper-local",
                    )
                ]
            )
        content = history[-1].content
        assert len(history) == 3 and isinstance(content, dict) and content == fake_observation
        assert "action_outcomes" not in content
        return ModelResponse(
            parts=[ToolCallPart("finish_role", {"summary": "Projection boundary validated."}, "helper-finish")]
        )

    runner = LocalAgentRunner(
        LocalAgentServices(
            invoke=helper_invoke,
            step_store=store,
            on_observation_delivered=helper_delivered,
            overflow_root=root / "helper-overflow",
        )
    )
    with override_allow_model_requests(False):
        projected = await runner.run_role(
            request,
            startup={"user": "Scripted projection probe.", "instructions": "Use permitted delivered evidence."},
            catalogue=catalogue[:2],
            model=FunctionModel(helper_model),
        )
    assert helper_dispatches == [("helper-wrong__1", "inspect"), ("helper-finish", "finish")]
    assert helper_deliveries == ["helper-wrong__1"]
    helper_events = [
        event for event in await store.list_events(run_id=projected.run_id) if event.tool_name in RESULT_HELPERS
    ]
    assert helper_events == [], "Pure helper execution must never leave the Monty worker."
    return {
        "nested_structured_objects": True,
        "spill": True,
        "simulated_actions": len(effects),
        "duplicate_effects": 0,
        "caught_fatal_fenced": True,
        "result_helpers": True,
        "projection_error_recovered": True,
        "helper_native_effects": 0,
        "helper_observation_recoveries": 0,
        "execution_guidance": True,
    }


if __name__ == "__main__":
    print(json.dumps({"runtime": verify_runtime(), "code_mode": asyncio.run(probe())}, sort_keys=True))
