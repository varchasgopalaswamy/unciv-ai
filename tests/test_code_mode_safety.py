"""Nested failures cannot hide from the native boundary or erase committed effects."""

import asyncio
import json

import pytest
from pydantic_ai.exceptions import ApprovalRequired, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.orchestration.contracts import GatewayReceipt
from civ_agent.orchestration.store import JournalConflict
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.overflow import ScopedOverflowStore
from tests.domain_fixtures import run_async
from tests.test_code_mode_runtime import CATALOGUE, candidate
from tests.test_local_agents import call, returns


def runner(tmp_path, invoke, **services):
    return LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke,
            step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
            overflow_root=tmp_path / "overflow",
            **services,
        )
    )


async def run_script(agent, script):
    def model(_messages, info):
        return call(info, "run_code", {"code": script}, "program")

    return await agent.run_role(
        candidate(),
        startup={"user": "Resolve permitted role work.", "instructions": "Use your tools."},
        catalogue=CATALOGUE,
        model=FunctionModel(model),
    )


@run_async
async def test_caught_deferred_settlement_failure_fences_subsequent_dispatch(tmp_path):
    dispatched = []

    async def invoke(_deps, identifier, _step, decision):
        dispatched.append((identifier, decision["kind"]))
        return GatewayReceipt(result={"receipts": [{"result": {"economyWait": {"state": "waiting"}}}]})

    async def wait(_deps, requests):
        assert [item.tool_call_id for item in requests.calls] == ["program__1"]
        raise JournalConflict("DEFERRED_SETTLEMENT_UNCERTAIN")

    script = "try:\n    economy_wait()\nexcept Exception:\n    pass\nplayer_turn()"
    with pytest.raises(JournalConflict, match="DEFERRED_SETTLEMENT_UNCERTAIN"):
        await run_script(runner(tmp_path, invoke, on_wait=wait), script)
    assert dispatched == [("program__1", "wait")]


@run_async
async def test_concurrent_gather_of_sequential_functions_is_rejected_before_dispatch(tmp_path):
    dispatched = []

    async def invoke(_deps, identifier, _step, _decision):
        dispatched.append(identifier)
        await asyncio.sleep(0)
        raise JournalConflict("NATIVE_RECEIPT_UNCERTAIN")

    script = (
        "import asyncio\ntry:\n    await asyncio.gather(player_turn(), player_turn())\nexcept Exception:\n    pass\n42"
    )
    # The role's frozen functions are synchronous in the SDK's sequential
    # mode. Monty rejects using their results as gather awaitables before any
    # native call, even when the program tries to catch the runtime failure.
    with pytest.raises(UnexpectedModelBehavior, match="exceeded max retries"):
        await run_script(runner(tmp_path, invoke), script)
    assert dispatched == []


@run_async
async def test_successful_deferred_settlement_completes_original_private_child_effect(tmp_path):
    async def invoke(_deps, _identifier, _step, decision):
        return GatewayReceipt(
            result={"roleFinished": True}
            if decision["kind"] == "finish"
            else {"receipts": [{"result": {"economyWait": {"state": "waiting"}}}]}
        )

    async def wait(_deps, requests):
        return DeferredToolResults(calls={item.tool_call_id: {"waitComplete": True} for item in requests.calls})

    def model(messages, info):
        if not returns(messages):
            return call(info, "run_code", {"code": "economy_wait()"}, "program")
        return call(info, "finish_role", {"summary": "Wait settled."}, "finish")

    result = await runner(tmp_path, invoke, on_wait=wait).run_role(
        candidate(),
        startup={"user": "Resolve permitted role work.", "instructions": "Use your tools."},
        catalogue=CATALOGUE,
        model=FunctionModel(model),
    )
    steps = SqliteStepStore(database=tmp_path / "steps.sqlite3")
    effect = await steps.get_tool_effect(run_id=result.run_id, tool_call_id="program__1")
    assert effect is not None and effect.status == "completed"


@run_async
async def test_approval_settles_owning_child_and_runs_approved_effect_once(tmp_path):
    requests_seen, effects = [], []

    async def invoke(_deps, identifier, _step, decision):
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        if not requests_seen:
            raise ApprovalRequired(metadata={"approval": "original-owner"})
        effects.append(identifier)
        return GatewayReceipt(result={"facts": "approved result"})

    async def wait(deps, requests):
        assert deps.job == candidate().job
        requests_seen.extend(requests.approvals)
        assert requests.metadata["program__1"] == {"approval": "original-owner"}
        return DeferredToolResults(approvals={item.tool_call_id: True for item in requests.approvals})

    def model(messages, info):
        if not returns(messages):
            return call(info, "run_code", {"code": "player_turn()"}, "program")
        return call(info, "finish_role", {"summary": "Approval settled."}, "finish")

    result = await runner(tmp_path, invoke, on_wait=wait).run_role(
        candidate(),
        startup={"user": "Resolve permitted role work.", "instructions": "Use your tools."},
        catalogue=CATALOGUE,
        model=FunctionModel(model),
    )
    assert [item.tool_call_id for item in requests_seen] == ["program__1"]
    assert effects == ["program__1"]
    effect = await SqliteStepStore(database=tmp_path / "steps.sqlite3").get_tool_effect(
        run_id=result.run_id, tool_call_id="program__1"
    )
    assert effect is not None and effect.status == "completed"


@run_async
async def test_lost_observation_acknowledgement_fences_later_dispatch(tmp_path):
    dispatched, delivered = [], []

    async def invoke(_deps, identifier, _step, decision):
        dispatched.append((identifier, decision["kind"]))
        return GatewayReceipt(result={"facts": "delivered natively"})

    async def acknowledge(_deps, identifier, step):
        delivered.append((identifier, step))
        raise JournalConflict("DELIVERY_ACKNOWLEDGEMENT_UNCERTAIN")

    script = "try:\n    player_turn()\nexcept Exception:\n    pass\neconomy_wait()"
    with pytest.raises(JournalConflict, match="DELIVERY_ACKNOWLEDGEMENT_UNCERTAIN"):
        await run_script(runner(tmp_path, invoke, on_observation_delivered=acknowledge), script)
    assert dispatched == [("program__1", "inspect")] and delivered == [("program__1", 1)]


@run_async
async def test_partial_commits_are_retained_and_never_replayed_after_uncertain_result(tmp_path):
    dispatched = []
    steps = SqliteStepStore(database=tmp_path / "steps.sqlite3")

    async def invoke(_deps, identifier, _step, _decision):
        dispatched.append(identifier)
        if len(dispatched) == 2:
            raise JournalConflict("NATIVE_RECEIPT_UNCERTAIN")
        return GatewayReceipt(result={"receipts": [{"operationId": "committed-effect", "status": "completed"}]})

    script = "economy_wait()\ntry:\n    economy_wait()\nexcept Exception:\n    pass\nplayer_turn()"
    with pytest.raises(JournalConflict, match="NATIVE_RECEIPT_UNCERTAIN"):
        await run_script(runner(tmp_path, invoke), script)
    assert dispatched == ["program__1", "program__2"]
    runs = await steps.list_runs()
    assert len(runs) == 1
    effect = await steps.get_tool_effect(run_id=runs[0].run_id, tool_call_id="program__1")
    assert effect is not None and effect.status == "completed" and effect.idempotency_key is not None
    events = await steps.list_events(run_id=runs[0].run_id)
    committed = [event for event in events if event.tool_call_id == "program__1" and "nested_result" in event.metadata]
    assert len(committed) == 1 and "committed-effect" in committed[0].metadata["nested_result"]


@run_async
async def test_python_failure_retains_full_child_result_before_parent_retry(tmp_path):
    calls = []

    async def invoke(_deps, identifier, _step, decision):
        calls.append(identifier)
        return GatewayReceipt(result={"roleFinished": True} if decision["kind"] == "finish" else {"facts": "x" * 30000})

    model_calls = []

    def model(_messages, info):
        model_calls.append(True)
        if len(model_calls) == 1:
            return call(info, "run_code", {"code": "player_turn()\n1 / 0"}, "program")
        return call(info, "finish_role", {"summary": "Used retained evidence."}, "finish")

    result = await runner(tmp_path, invoke).run_role(
        candidate(),
        startup={"user": "Resolve permitted role work.", "instructions": "Use your tools."},
        catalogue=CATALOGUE,
        model=FunctionModel(model),
    )
    steps = SqliteStepStore(database=tmp_path / "steps.sqlite3")
    events = await steps.list_events(run_id=result.run_id)
    child_events = [event for event in events if event.tool_call_id == "program__1"]
    started = next(event for event in child_events if "nested_call" in event.metadata)
    completed = next(event for event in child_events if "nested_result" in event.metadata)
    assert started.metadata["parent_tool_call_id"] == "program"
    assert started.metadata["native_call_id"] == "program__1"
    assert started.metadata["native_operation_id"].startswith("native:")
    assert completed.metadata["scope"] == started.metadata["scope"]
    retained = ModelMessagesTypeAdapter.validate_json(completed.metadata["nested_result"])
    assert isinstance(retained[0].parts[0], ToolReturnPart)
    assert retained[0].parts[0].content == {"facts": "x" * 30000}
    assert calls == ["program__1", "finish"]


@run_async
async def test_python_error_after_commit_keeps_action_outcome_in_correction_feedback(tmp_path):
    effects, model_calls = [], []

    async def invoke(_deps, identifier, _step, decision):
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        effects.append(identifier)
        return GatewayReceipt(result={"receipts": [{"operationId": "committed-effect", "status": "completed"}]})

    def model(messages, info):
        model_calls.append(True)
        if len(model_calls) == 1:
            return call(info, "run_code", {"code": "economy_wait()\n1 / 0"}, "program")
        assert "committed-effect" in str(messages)
        content = returns(messages)[-1].content
        assert isinstance(content, str)
        feedback = json.loads(content)
        assert feedback["status"] == "needs_recovery"
        assert feedback["error"]["code"] == "CODE_RUNTIME_ERROR"
        assert feedback["action_outcomes"][0]["receipts"][0]["operationId"] == "committed-effect"
        return call(info, "finish_role", {"summary": "Kept the confirmed action without replay."}, "finish")

    result = await runner(tmp_path, invoke).run_role(
        candidate(),
        startup={"user": "Resolve permitted role work.", "instructions": "Use your tools."},
        catalogue=CATALOGUE,
        model=FunctionModel(model),
    )
    assert effects == ["program__1"]
    assert result.report.summary == "Kept the confirmed action without replay."


@run_async
async def test_invalid_nested_arguments_return_structured_feedback_and_stop_after_prior_commit(tmp_path):
    invoked, model_calls = [], []

    async def invoke(_deps, identifier, _step, decision):
        invoked.append((identifier, decision["kind"]))
        return GatewayReceipt(
            result={"roleFinished": True}
            if decision["kind"] == "finish"
            else {
                "decisionId": "decision1",
                "receipts": [
                    {"operationId": "committed-effect", "status": "confirmed", "tool": "economy_wait", "result": {}}
                ],
                "unexecuted": [],
            }
        )

    def model(messages, info):
        model_calls.append(True)
        if len(model_calls) == 1:
            return call(info, "run_code", {"code": "player_turn()"}, "warmup")
        if len(model_calls) == 2:
            return call(
                info,
                "run_code",
                {"code": "economy_wait()\nplayer_turn(unknown='PRIVATE_INPUT')\neconomy_wait()"},
                "program",
            )
        content = returns(messages)[-1].content
        assert isinstance(content, str)
        feedback = json.loads(content)
        assert feedback["error"]["code"] == "INVALID_TOOL_ARGUMENTS"
        assert feedback["error"]["tool"] == "player_turn"
        assert feedback["recovery"]["invalidFields"]
        assert "PRIVATE_INPUT" not in json.dumps(feedback)
        assert feedback["action_outcomes"][0]["receipts"][0]["operationId"] == "committed-effect"
        return call(info, "finish_role", {"summary": "Kept the committed action."}, "finish")

    await runner(tmp_path, invoke).run_role(
        candidate(),
        startup={"user": "Work", "instructions": "Use tools"},
        catalogue=CATALOGUE,
        model=FunctionModel(model),
    )
    assert invoked == [("warmup__1", "inspect"), ("program__1", "wait"), ("finish", "finish")]


@run_async
async def test_rejected_native_result_keeps_its_refresh_reads_in_structured_correction(tmp_path):
    async def invoke(_deps, _identifier, _step, decision):
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        return GatewayReceipt(
            result={
                "decisionId": "decision1",
                "receipts": [
                    {
                        "operationId": "rejected-read",
                        "tool": "player_turn",
                        "status": "rejected",
                        "result": {
                            "error": "OBSERVATION_REQUIRED",
                            "recovery": {
                                "reads": [{"tool": "player_turn", "arguments": {}}],
                                "instruction": "Read player_turn again.",
                            },
                        },
                    }
                ],
                "unexecuted": [],
            }
        )

    def model(messages, info):
        if not returns(messages):
            return call(info, "run_code", {"code": "result_data(result=player_turn())"}, "program")
        content = returns(messages)[-1].content
        assert isinstance(content, str)
        feedback = json.loads(content)
        assert feedback["error"]["code"] == "OBSERVATION_REQUIRED"
        assert feedback["recovery"]["reads"] == [{"tool": "player_turn", "arguments": {}}]
        assert feedback["recovery"]["receipt"] == "rejected-read"
        return call(info, "finish_role", {"summary": "Observed the recovery requirement."}, "finish")

    await runner(tmp_path, invoke).run_role(
        candidate(),
        startup={"user": "Work", "instructions": "Use tools"},
        catalogue=CATALOGUE,
        model=FunctionModel(model),
    )


@run_async
async def test_spill_failure_is_explicit_and_keeps_action_outcomes_inline(tmp_path, monkeypatch):
    async def unavailable(*_args):
        raise OSError("storage unavailable")

    monkeypatch.setattr(ScopedOverflowStore, "write", unavailable)

    async def invoke(_deps, _identifier, _step, decision):
        return GatewayReceipt(
            result={"roleFinished": True}
            if decision["kind"] == "finish"
            else {"receipts": [{"operationId": "committed-effect", "status": "completed"}]}
        )

    def model(messages, info):
        if not returns(messages):
            return call(info, "run_code", {"code": "economy_wait()\n'x' * 30000"}, "program")
        output = returns(messages)[-1]
        assert "x" * 30000 in str(output.content)
        assert "committed-effect" in str(output.content)
        assert "action_outcomes" in str(output.content)
        assert len(str(output.content)) > 30000
        assert "program__1" in output.metadata["tool_returns"]
        return call(info, "finish_role", {"summary": "Committed outcome retained."}, "finish")

    result = await runner(tmp_path, invoke).run_role(
        candidate(),
        startup={"user": "Resolve permitted role work.", "instructions": "Use your tools."},
        catalogue=CATALOGUE,
        model=FunctionModel(model),
    )
    assert result.report.summary == "Committed outcome retained."


@run_async
async def test_cancellation_stops_nested_program_without_dispatching_followups(tmp_path):
    entered = asyncio.Event()
    dispatched = []

    async def invoke(_deps, identifier, _step, _decision):
        dispatched.append(identifier)
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        run_script(runner(tmp_path, invoke), "try:\n    player_turn()\nexcept Exception:\n    pass\neconomy_wait()")
    )
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert dispatched == ["program__1"]
