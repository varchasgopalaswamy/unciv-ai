"""Completion claims use exact committed action receipts across native tool families."""

import copy
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ToolReturn
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.execution_policy import ExecutionPolicy
from civ_agent.gameplay_policy import GameplayPolicy
from civ_agent.orchestration.contracts import GatewayReceipt
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.outcomes import VerifiedOutcomes
from civ_agent.tool_schemas import tool_model
from tests.domain_fixtures import run_async
from tests.test_local_agents import call, request, returns


@pytest.mark.parametrize("tool", ["player_command", "chat_send", "role_message", "trade_decide"])
@run_async
async def test_confirmed_mutations_keep_exact_kinds_and_run_scope(tool):
    cap = VerifiedOutcomes()
    native = {"kind": "MOVE_UNIT"} if tool == "player_command" else {"kind": "internal", "applied": True}
    receipt = {"operationId": "op1", "tool": tool, "status": "confirmed", "result": native}
    original = ToolReturn(return_value={"receipts": [receipt]}, metadata={"kept": True})
    before = copy.deepcopy(original)
    returned = await cap.after_tool_execute(
        None, call=SimpleNamespace(tool_name="act"), tool_def=None, args={}, result=original
    )
    assert isinstance(returned, ToolReturn)
    assert isinstance(returned.return_value, dict)
    kind = "MOVE_UNIT" if tool == "player_command" else tool
    assert returned.return_value["actionOutcomes"][0]["kind"] == kind
    assert returned.metadata == before.metadata and original == before
    claim = {"operationId": "op1", "kind": kind, "summary": "Committed action"}
    cap.validate([claim])
    for invalid in ({**claim, "kind": "movement"}, {**claim, "operationId": "invented"}):
        with pytest.raises(ModelRetry):
            cap.validate([invalid])
    fresh = await cap.for_run(None)
    with pytest.raises(ModelRetry):
        fresh.validate([claim])
    for status, result in (("rejected", native), ("unexecuted", {}), ("confirmed", {"error": "STALE"})):
        await cap.after_tool_execute(
            None,
            call=SimpleNamespace(tool_name="act"),
            tool_def=None,
            args={},
            result={"receipts": [{**receipt, "status": status, "result": result}]},
        )
        with pytest.raises(ModelRetry):
            cap.validate([claim])


@run_async
async def test_inspections_do_not_establish_effects():
    cap = VerifiedOutcomes()
    value = {"receipts": [{"operationId": "read", "tool": "player_turn", "status": "confirmed", "result": {}}]}
    for tool in ("player_turn", "inspect_bundle", "act"):
        assert (
            await cap.after_tool_execute(
                None, call=SimpleNamespace(tool_name=tool), tool_def=None, args={}, result=value
            )
            == value
        )
    assert cap.receipts == {}


@pytest.mark.parametrize("code_mode,late_failure", [(False, False), (True, False), (True, True)])
@run_async
async def test_message_receipt_can_finish_role_through_sdk_without_repeating_action(tmp_path, code_mode, late_failure):
    invoked = []

    async def invoke(_deps, _identifier, _step, decision):
        invoked.append(decision)
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        assert decision["kind"] == "act" and decision["operations"][0]["tool"] == "chat_send"
        return GatewayReceipt(
            result={
                "receipts": [
                    {"operationId": "sent", "tool": "chat_send", "status": "confirmed", "result": {"delivered": True}}
                ],
                "unexecuted": [],
            }
        )

    operation = {"tool": "chat_send", "arguments": {"text": "Hello", "to": "Greece"}}

    def model(messages, info):
        if not returns(messages):
            if code_mode:
                code = f"act(operations=[{operation!r}])"
                if late_failure:
                    code += "\nraise ValueError('Later program statement failed')"
                return call(info, "run_code", {"code": code}, "send")
            return call(info, "act", {"operations": [operation]}, "send")
        return call(
            info,
            "finish_role",
            {
                "summary": "Sent a message",
                "effectClaims": [{"operationId": "sent", "kind": "chat_send", "summary": "Sent"}],
            },
            "finish",
        )

    result = await LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke,
            step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
            overflow_root=tmp_path / "overflow",
        )
    ).run_role(
        request(purpose="communication").model_copy(
            update={
                "execution_policy": ExecutionPolicy.current() if code_mode else ExecutionPolicy(),
                "gameplay_policy": GameplayPolicy(),
            }
        ),
        startup={"user": "Send the chosen message", "instructions": "Use native receipts"},
        catalogue=[
            {"name": "finish_role", "inputSchema": {}},
            {"name": "chat_send", "inputSchema": tool_model("chat_send", "communication").model_json_schema()},
        ],
        model=FunctionModel(model),
    )
    assert result.usage.requests == 2 and len(invoked) == 2
