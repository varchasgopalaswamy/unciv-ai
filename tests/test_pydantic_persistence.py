"""Evaluate the upstream persistence capability before replacing owned journals."""

import asyncio
import multiprocessing
from pathlib import Path

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness import StepPersistence
from pydantic_ai_harness.step_persistence import InMemoryStepStore, SqliteStepStore, annotate_tool_effect
from pydantic_ai_harness.step_persistence.recovery import inspect_recovery


def respond(messages, info):
    if any(
        isinstance(part, ToolReturnPart)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    ):
        return ModelResponse(parts=[TextPart("Observed the supplied field.")])
    return ModelResponse(parts=[ToolCallPart("inspect_field", {}, "read1")])


persistence = StepPersistence(store=InMemoryStepStore(), capture_frontier=True)


def _effect_worker(database, committed):
    async def run():
        store = SqliteStepStore(database=database)
        agent = Agent(FunctionModel(respond), capabilities=[StepPersistence(store=store, capture_frontier=True)])

        @agent.tool
        async def inspect_field(ctx: RunContext) -> dict:
            await annotate_tool_effect(
                store,
                ctx,
                idempotency_key="fixture:external-effect",
                effect_summary="A fixture effect may have happened",
            )
            committed.set()
            await asyncio.Event().wait()
            return {}

        await agent.run("Read the fixture.", conversation_id="crash-fixture")

    asyncio.run(run())


def test_upstream_effect_ledger_retains_uncertainty_after_real_process_kill(tmp_path):
    async def run():
        path = Path(tmp_path / "steps.sqlite3")
        context = multiprocessing.get_context("spawn")
        committed = context.Event()
        process = context.Process(target=_effect_worker, args=(path, committed))
        try:
            process.start()
            assert await asyncio.to_thread(committed.wait, 30)
            process.kill()
            await asyncio.to_thread(process.join, 10)
            assert process.exitcode == -9
            store = SqliteStepStore(database=path)
            runs = await store.list_runs(conversation_id="crash-fixture")
            assert len(runs) == 1
            recovery = await inspect_recovery(store=store, run_id=runs[0].run_id)
            assert len(recovery.unresolved) == 1
            assert recovery.unresolved[0].status == "started"
            assert recovery.unresolved[0].idempotency_key == "fixture:external-effect"
            assert recovery.latest is not None
        finally:
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 10)

    asyncio.run(run())
