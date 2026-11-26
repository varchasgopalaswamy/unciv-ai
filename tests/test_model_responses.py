"""Recovery spans provider streams while preserving deadlines and cancellation."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelConnectionError
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.wrapper import WrapperModel

from civ_agent.runtime.capabilities import ContextUsageTelemetry
from civ_agent.runtime.model_policy import LocalModelPolicy
from tests.domain_fixtures import run_async


@pytest.mark.parametrize(
    "failure",
    [
        ModelConnectionError("synthetic", "Connection failed during cleanup", phase="read"),
        httpx2.ReadError("Connection failed during cleanup"),
        httpx2.ReadTimeout("Cleanup timed out"),
    ],
)
@run_async
async def test_stream_close_failure_retries_the_same_request(failure):
    sent = []

    async def stream(messages, info):
        sent.append(list(messages))
        yield "Failed answer" if len(sent) == 1 else "Complete answer"

    class ClosingModel(WrapperModel):
        @asynccontextmanager
        async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as response:
                yield response
            if len(sent) == 1:
                raise failure

    sleep = AsyncMock()
    agent = Agent(
        ClosingModel(FunctionModel(stream_function=stream)),
        capabilities=[LocalModelPolicy(sleep=sleep), ContextUsageTelemetry()],
    )
    result = await agent.run("Answer the current request.")
    assert result.output == "Complete answer" and len(sent) == 2 and sent[0] == sent[1]
    sleep.assert_awaited_once_with(2)


@pytest.mark.parametrize("after_first_event", [False, True])
@run_async
async def test_stream_deadline_failures_retry_through_the_same_policy(after_first_event):
    sent = []

    async def stream(messages, info):
        sent.append(list(messages))
        if len(sent) == 1:
            if after_first_event:
                yield "Partial"
            await asyncio.sleep(1)
        yield "Complete"

    sleep = AsyncMock()
    agent = Agent(
        FunctionModel(stream_function=stream),
        capabilities=[LocalModelPolicy(sleep=sleep), ContextUsageTelemetry()],
    )
    result = await agent.run(
        "Answer the current request.", model_settings={"first_event_timeout": 0.02, "request_timeout": 0.05}
    )
    assert result.output == "Complete" and len(sent) == 2 and sent[0] == sent[1]
    sleep.assert_awaited_once_with(2)


@run_async
async def test_first_event_deadline_stops_applying_after_provider_output_begins():
    sent = []

    async def stream(messages, info):
        sent.append(True)
        yield "Started"
        await asyncio.sleep(0.05)
        yield " and completed"

    sleep = AsyncMock()
    agent = Agent(
        FunctionModel(stream_function=stream),
        capabilities=[LocalModelPolicy(sleep=sleep), ContextUsageTelemetry()],
    )
    result = await agent.run(
        "Answer the current request.", model_settings={"first_event_timeout": 0.02, "request_timeout": 1}
    )
    assert result.output == "Started and completed" and len(sent) == 1
    sleep.assert_not_awaited()


@run_async
async def test_cancelling_a_partial_response_closes_it_and_propagates_cancellation():
    entered, closed = asyncio.Event(), asyncio.Event()
    sent = []

    async def stream(messages, info):
        sent.append(True)
        try:
            yield "Partial"
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    sleep = AsyncMock()
    agent = Agent(
        FunctionModel(stream_function=stream),
        capabilities=[LocalModelPolicy(sleep=sleep), ContextUsageTelemetry()],
    )
    task = asyncio.create_task(agent.run("Answer the current request."))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(closed.wait(), 2)
    assert len(sent) == 1
    sleep.assert_not_awaited()
