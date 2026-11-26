"""A disposable JVM game completes the fixed role flow through real Monty calls."""

import asyncio
import inspect

from pydantic import TypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.execution_policy import ExecutionPolicy
from civ_agent.operator_connection import Operator
from civ_agent.orchestration.host_effect_service import NativeCommandService
from civ_agent.player_paths import PlayerPaths
from civ_agent.runtime import scripted_model as scripted
from civ_agent.runtime.code_mode_results import LOCAL_HELPER_VERSION, RESULT_HELPERS
from civ_agent.runtime.controller import run_controller
from civ_agent.tls import write_private_json
from tests.test_local_controller_native import local_config, native_game


def test_disposable_native_game_completes_code_mode_roles_and_handoff_without_duplicate_effects(tmp_path, monkeypatch):
    original_model, execute = scripted.scripted_model, NativeCommandService.execute
    prepared, committed = [], []
    focused_sent = False
    focus_code = """packet = player_turn()
view = result_data(result=packet)
assert isinstance(view, dict)
units = required_result_field(data=view, key='units')
assert isinstance(units, list)
founders = [unit for unit in units if isinstance(unit, dict) and unit.get('canFoundCity') is True]
assert len(founders) == 1 and isinstance(founders[0].get('handle'), str)
answer = execute_commands(commands=[{'kind': 'FOUND_CITY', 'target': founders[0]['handle']}])
result_feedback(result=answer)"""

    def model(request):
        prepared.append(request)
        base = original_model(request)
        if request.purpose != "gameplay":
            return base
        assert base.function is not None

        async def respond(messages, info):
            nonlocal focused_sent
            if not focused_sent:
                focused_sent = True
                return ModelResponse(
                    parts=[ToolCallPart("run_code", {"focused": True, "code": focus_code}, "focused-found-city")]
                )
            result = base.function(messages, info)
            return await result if inspect.isawaitable(result) else result

        return FunctionModel(respond, model_name=base.model_name)

    async def capture(service, effect):
        result = await execute(service, effect)
        committed.append((effect.prepared.payload, result))
        return result

    monkeypatch.setattr(scripted, "scripted_model", model)
    monkeypatch.setattr(NativeCommandService, "execute", capture)
    root = tmp_path / "game"
    with native_game(root) as (url, ca, token):
        config = local_config()
        config.agents.execution_policy = ExecutionPolicy.current()
        write_private_json(root / "resolved.json", config.model_dump(mode="json"))
        write_private_json(
            root / "container.json",
            {
                "id": "monty-native-test",
                "runtime": "pydantic-local-v1",
                "launch": "initial",
                "native": {"leaseId": "monty-native-test-lease"},
            },
        )
        state = asyncio.run(run_controller(root, resume=False, host_url=url, host_ca=ca))
        assert state.mode in {"STOPPED", "FINISHED"}, (
            state.reason,
            (root / "native-runtime/failure.json").read_text()
            if (root / "native-runtime/failure.json").exists()
            else "",
        )
        assert state.handoff >= 1 and state.completed_attempts
        completed = state.completed_attempts[0]
        assert completed.status == "COMPLETED" and completed.execution_policy == ExecutionPolicy.current()
        assert {request.purpose for request in prepared} >= {"gameplay", "economy", "military"}
        assert all(request.execution_policy == ExecutionPolicy.current() for request in prepared)
        assert committed
        request_ids = [payload["requestId"] for payload, _result in committed]
        assert len(request_ids) == len(set(request_ids))
        checkpoint = Operator(url, token, ca).call("snapshot", leaseId=state.lease_id)["checkpoint"]
        receipts = [entry for player in checkpoint["players"].values() for entry in player["receipts"]]
        for payload, result in committed:
            matching = [entry for entry in receipts if entry["request"]["requestId"] == payload["requestId"]]
            assert len(matching) == 1 and matching[0]["receipt"] == result

    async def check_children():
        store = SqliteStepStore(database=PlayerPaths(root, 0, "Rome").store / "agent-steps.sqlite3")
        effects, generated, focused = [], [], []
        for run in await store.list_runs():
            if run.agent_name != "player_role_v1":
                continue
            snapshot = await store.latest_snapshot(run_id=run.run_id, include_interrupted=True)
            assert snapshot is not None and snapshot.state == "complete"
            for message in snapshot.messages:
                if isinstance(message, ModelResponse):
                    assert all(
                        part.tool_name in {"run_code", "finish_role"}
                        for part in message.parts
                        if isinstance(part, ToolCallPart)
                    ), "Production Code Mode must send native tools through run_code"
                if isinstance(message, ModelRequest):
                    for part in message.parts:
                        if isinstance(part, ToolReturnPart) and part.tool_call_id == "focused-found-city":
                            assert part.outcome == "success", part.content
                            metadata = part.metadata
                            assert metadata["local_result_helpers"] == LOCAL_HELPER_VERSION
                            assert metadata["focused_window"]["acquired"] is True
                            assert metadata["focused_window"]["operation_count"] == 2
                            assert metadata["focused_window"]["end_reason"] == "native_batch_acknowledged"
                            calls = {
                                key: TypeAdapter(ToolCallPart).validate_python(value)
                                for key, value in metadata["tool_calls"].items()
                            }
                            results = {
                                key: TypeAdapter(ToolReturnPart).validate_python(value)
                                for key, value in metadata["tool_returns"].items()
                            }
                            assert [item.tool_name for item in calls.values()] == ["player_turn", "execute_commands"]
                            observed_packet = results["focused-found-city__1"].content
                            assert isinstance(observed_packet, dict)
                            delivered = observed_packet["receipts"][0]["result"]
                            target = calls["focused-found-city__2"].args_as_dict()["commands"][0]["target"]
                            assert any(unit["handle"] == target and unit["canFoundCity"] for unit in delivered["units"])
                            command_packet = results["focused-found-city__2"].content
                            assert isinstance(command_packet, dict)
                            command_receipts = command_packet["receipts"]
                            assert any(
                                row["tool"] == "player_command" and row["status"] == "confirmed"
                                for row in command_receipts
                            )
                            focused.append(part)
            for event in await store.list_events(run_id=run.run_id):
                assert event.tool_name not in RESULT_HELPERS
                if event.metadata.get("native_nested_version") == "1" and "nested_result" in event.metadata:
                    assert event.tool_call_id is not None
                    child = await store.get_tool_effect(run_id=run.run_id, tool_call_id=event.tool_call_id)
                    assert child is not None and child.status == "completed"
                    assert event.metadata["native_operation_id"] == child.idempotency_key
                    parent = await store.get_tool_effect(
                        run_id=run.run_id, tool_call_id=event.metadata["parent_tool_call_id"]
                    )
                    assert parent is not None and parent.tool_name == "run_code" and parent.status == "completed"
                    generated.append((run.metadata["purpose"], child.tool_name))
                    effects.append(child)
        assert any(name == "execute_commands" for _purpose, name in generated)
        assert any(effect.tool_name == "execute_commands" and "__" in effect.tool_call_id for effect in effects)
        assert len(focused) == 1

    asyncio.run(check_children())
