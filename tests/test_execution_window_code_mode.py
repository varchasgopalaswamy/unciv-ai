"""Actual Monty execution privately binds a short read/check/write sequence."""

import asyncio
import hashlib

import pytest
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.execution_policy import OutputPolicy
from civ_agent.execution_windows import ExecutionWindowScheduler
from civ_agent.orchestration.contracts import GatewayJob, GatewayReceipt, RoleRequest
from civ_agent.orchestration.gateway_execution import operation_identity
from civ_agent.orchestration.store import JournalConflict
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.code_mode import code_mode_capabilities
from tests.domain_fixtures import run_async
from tests.test_code_mode_runtime import candidate
from tests.test_local_agents import call, returns


@run_async
async def test_monty_focused_read_check_write_has_no_direct_native_alternative_and_releases_before_next_inference():
    request = candidate()
    scheduler = ExecutionWindowScheduler(game=request.scope.game, player=request.scope.player)
    selected = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
    other = selected.model_copy(update={"job": "sibling", "purpose": "economy"})
    read_started = asyncio.Event()
    order, runs = [], []
    code = "r = player_turn()\nif r['revision'] == 'r1':\n    execute_commands(value=1)\nr['revision']"

    async def read(ctx: RunContext[RoleRequest]) -> dict:
        assert ctx.tool_call_id is not None
        runs.append(ctx.run_id)

        async def inspect():
            order.append("read")
            read_started.set()
            await asyncio.sleep(0)
            return {"revision": "r1"}

        return await scheduler.run(selected, ctx.tool_call_id, inspect)

    async def write(ctx: RunContext[RoleRequest], value: int) -> dict:
        assert ctx.tool_call_id is not None

        async def command():
            assert value == 1 and order == ["read"]
            order.append("write")
            return {"decisionId": "command", "receipts": [], "unexecuted": []}

        return await scheduler.run(selected, ctx.tool_call_id, command, write=True, run_id=ctx.run_id)

    async def sibling():
        await read_started.wait()

        async def update():
            order.append("sibling")

        await scheduler.run(other, "sibling-call", update, write=True)

    async def model(messages, info):
        assert [tool.name for tool in info.function_tools] == ["run_code"]
        if not returns(messages):
            return ModelResponse(parts=[ToolCallPart("run_code", {"code": code, "focused": True}, "program1")])
        assert scheduler.owner is None, "No window is retained across a model request"
        return ModelResponse(parts=[TextPart("done")])

    pending = asyncio.create_task(sibling())
    try:
        policy = request.execution_policy.model_copy(update={"output": OutputPolicy()})
        agent = Agent(
            FunctionModel(model),
            deps_type=RoleRequest,
            tools=[
                Tool(read, name="player_turn", sequential=True),
                Tool(write, name="execute_commands", sequential=True),
            ],
            capabilities=code_mode_capabilities(
                policy, required={"player_turn", "execute_commands"}, store=None, execution_windows=scheduler
            ),
        )
        result = await agent.run("Inspect, check and apply the chosen test command.", deps=request)
        await pending
        assert result.output == "done" and order == ["read", "write", "sibling"]
        started = next(row for row in scheduler.events if row["kind"] == "started")
        assert started["run_id"] == runs[0] and started["parent_call_id"] == "program1"
        assert started["program_sha256"] == hashlib.sha256(code.encode()).hexdigest()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await scheduler.close()


@pytest.mark.parametrize("fail_child", [False, True])
@run_async
async def test_focused_deferred_child_gets_own_binding_and_parent_cannot_resume_ended_window(tmp_path, fail_child):
    parent = candidate()
    child = parent.model_copy(update={"job": "economy-child", "purpose": "economy"})
    scheduler = ExecutionWindowScheduler(game=parent.scope.game, player=parent.scope.player)
    steps = SqliteStepStore(database=tmp_path / "steps")
    dispatched = []

    async def invoke(deps, identifier, step, decision):
        identity = GatewayJob(scope=deps.scope, job=deps.job, purpose=deps.purpose)

        async def native():
            dispatched.append((deps.job, decision["kind"]))
            if fail_child and deps.job == child.job:
                raise JournalConflict("CHILD_NATIVE_STORE_UNCERTAIN")
            if decision["kind"] == "finish":
                return GatewayReceipt(result={"roleFinished": True})
            tool = "economy_wait" if decision["kind"] == "wait" else "player_turn"
            data = {"economyWait": {"state": "waiting"}} if tool == "economy_wait" else {"turn": 1}
            return GatewayReceipt(
                result={
                    "decisionId": identifier,
                    "receipts": [
                        {"operationId": identifier + ":0", "tool": tool, "status": "confirmed", "result": data}
                    ],
                    "unexecuted": [],
                }
            )

        return await scheduler.run(
            identity,
            operation_identity(identity.job, identity.scope.epoch, step, identifier),
            native,
            release=decision["kind"] in {"wait", "finish"},
        )

    def child_model(messages, info):
        if not returns(messages):
            return call(info, "run_code", {"code": "player_turn()", "focused": True}, "child-program")
        return call(info, "finish_role", {"summary": "Child used its own fresh focus."}, "child-finish")

    async def wait(_deps, requests):
        assert scheduler.owner is None
        await LocalAgentRunner(
            LocalAgentServices(
                invoke=invoke, step_store=steps, execution_windows=scheduler, overflow_root=tmp_path / "overflow"
            )
        ).run_role(
            child,
            startup={"user": "Inspect the child's permitted view.", "instructions": "Use permitted tools."},
            catalogue=[{"name": name, "inputSchema": {}} for name in ("finish_role", "player_turn")],
            model=FunctionModel(child_model),
        )
        return DeferredToolResults(calls={item.tool_call_id: {"waitComplete": True} for item in requests.calls})

    def parent_model(messages, info):
        history = returns(messages)
        if not history:
            code = (
                "player_turn()\ntry:\n    economy_wait()\nexcept Exception:\n    pass\nplayer_turn()"
                if fail_child
                else "player_turn()\neconomy_wait()\nplayer_turn()"
            )
            return call(
                info,
                "run_code",
                {"code": code, "focused": True},
                "parent-program",
            )
        if len(history) == 1:
            assert history[-1].outcome == "retried" and "FOCUSED_WINDOW_ENDED" in str(history[-1].content)
            return call(info, "run_code", {"code": "player_turn()", "focused": True}, "parent-reconsidered")
        return call(info, "finish_role", {"summary": "Reconsidered after the deferred child."}, "parent-finish")

    try:
        run = LocalAgentRunner(
            LocalAgentServices(
                invoke=invoke,
                step_store=steps,
                execution_windows=scheduler,
                overflow_root=tmp_path / "overflow",
                on_wait=wait,
            )
        ).run_role(
            parent,
            startup={"user": "Wait for the economy role.", "instructions": "Use permitted tools."},
            catalogue=[{"name": name, "inputSchema": {}} for name in ("finish_role", "player_turn", "economy_wait")],
            model=FunctionModel(parent_model),
        )
        if fail_child:
            with pytest.raises(JournalConflict, match="CHILD_NATIVE_STORE_UNCERTAIN"):
                await run
            assert dispatched == [(parent.job, "inspect"), (parent.job, "wait"), (child.job, "inspect")]
            assert scheduler.owner is None and not scheduler.queue
            return
        await run
        assert dispatched == [
            (parent.job, "inspect"),
            (parent.job, "wait"),
            (child.job, "inspect"),
            (child.job, "finish"),
            (parent.job, "inspect"),
            (parent.job, "finish"),
        ]
        assert [row["job"] for row in scheduler.events if row["kind"] == "started"] == [
            parent.job,
            child.job,
            parent.job,
        ]
        assert scheduler.owner is None and not scheduler.queue
        child_run = next(row for row in await steps.list_runs() if row.metadata.get("job") == child.job)
        nested = [
            row.metadata["parent_tool_call_id"]
            for row in await steps.list_events(run_id=child_run.run_id)
            if row.metadata.get("native_nested_version") == "1"
        ]
        assert nested and set(nested) == {"child-program"}, "A deferred child must not inherit Main's SDK parent/fence"
        parent_run = next(row for row in await steps.list_runs() if row.metadata.get("job") == parent.job)
        parent_waits = [
            row.metadata["parent_tool_call_id"]
            for row in await steps.list_events(run_id=parent_run.run_id)
            if row.metadata.get("native_nested_version") == "1" and row.tool_name == "economy_wait"
        ]
        assert parent_waits and set(parent_waits) == {"parent-program"}, (
            "Original deferred settlement retains its parent"
        )
    finally:
        await scheduler.close()
