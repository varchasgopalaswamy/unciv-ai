"""Synthetic provider failures recover without native effects or model failover."""

import asyncio
import logging
import warnings
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    ModelAuthenticationError,
    ModelConnectionError,
    ModelContextWindowExceededError,
    ModelOverloadedError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelServerError,
)
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.orchestration.contracts import GatewayReceipt
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices, configure_cache_warning_logging
from civ_agent.runtime.model_policy import (
    LocalModelPolicy,
    ModelPolicySettings,
    context_compaction,
)
from tests.domain_fixtures import run_async
from tests.test_local_agents import request


@run_async
async def test_late_stream_failure_retries_before_executing_role_completion(tmp_path):
    sent, effects = [], []

    async def stream(messages, info):
        sent.append(list(messages))
        yield {0: DeltaToolCall(name="finish_role", json_args='{"summary":"Done"}', tool_call_id="finish")}
        if len(sent) == 1:
            raise ModelConnectionError("synthetic", "Connection dropped after output", phase="read", in_stream=True)

    async def invoke(_request, _identifier, _step, decision):
        effects.append(decision)
        return GatewayReceipt(result={"roleFinished": True})

    runner = LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke,
            step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
            model_policy=ModelPolicySettings(initial_delay=0, maximum_delay=0),
            on_context_usage=AsyncMock(),
        )
    )
    result = await runner.run_role(
        request(),
        startup={"user": "Complete this role", "instructions": "Finish the current work."},
        catalogue=[{"name": "finish_role", "inputSchema": {}}],
        model=FunctionModel(stream_function=stream),
    )
    assert result.report.summary == "Done" and len(sent) == 2 and sent[0] == sent[1]
    assert len(effects) == 1 and effects[0]["kind"] == "finish"


@pytest.mark.parametrize("kind", [ModelOverloadedError, ModelRateLimitError, ModelQuotaExceededError, ModelServerError])
@run_async
async def test_server_backoff_retries_same_request_without_repeating_tools(kind):
    sent, sleeps, statuses, attempts = [], [], [], []

    async def sleep(delay):
        sleeps.append(delay)

    async def status(state, delay):
        statuses.append((state, delay))

    async def attempt(_deps, run_id, step, number):
        attempts.append((run_id, step, number))

    def model(messages, info):
        sent.append(list(messages))
        if len(sent) < 3:
            raise kind("synthetic", "Capacity unavailable", retry_after=7)
        return ModelResponse(parts=[TextPart("Ready.")])

    agent = Agent(
        FunctionModel(model),
        capabilities=[LocalModelPolicy(on_status=status, on_attempt=attempt, sleep=sleep)],
    )
    result = await agent.run("Do ordinary permitted work.")
    assert result.output == "Ready."
    assert sleeps == [7, 7]
    assert sent[0] == sent[1] == sent[2]
    assert [item[2] for item in attempts] == [1, 2, 3]
    assert [item[0] for item in statuses if item[0] != "inference"] == ["waiting_for_model"] * 2
    assert result.usage.requests == 1


@run_async
async def test_refusal_waits_on_same_model_without_changing_prompt_or_dispatching_tools():
    seen, sleeps = [], []

    async def sleep(delay):
        sleeps.append(delay)

    def model(messages, info):
        seen.append(list(messages))
        if len(seen) == 1:
            return ModelResponse(parts=[], finish_reason="content_filter")
        return ModelResponse(parts=[TextPart("Ready.")])

    agent = Agent(FunctionModel(model), capabilities=[LocalModelPolicy(sleep=sleep)])
    assert (await agent.run("Review this ordinary game position.")).output == "Ready."
    assert seen[0] == seen[1] and sleeps == [2]


@pytest.mark.parametrize("failures", [60, 250])
@run_async
async def test_repeated_capacity_wait_retries_past_the_sdk_attempt_guard(failures):
    attempts = 0

    async def sleep(_delay):
        pass

    def model(_messages, _info):
        nonlocal attempts
        attempts += 1
        if attempts <= failures:
            raise ModelOverloadedError("synthetic", "PRIVATE_PROVIDER_RESPONSE")
        return ModelResponse(parts=[TextPart("Ready.")])

    policy = LocalModelPolicy(sleep=sleep)
    result = await Agent(FunctionModel(model), capabilities=[policy]).run("Review.")
    assert result.output == "Ready." and attempts == failures + 1
    assert len(result.response.failed_attempts or []) == failures
    assert b"PRIVATE_PROVIDER_RESPONSE" not in ModelMessagesTypeAdapter.dump_json(result.all_messages())


@run_async
async def test_long_capacity_wait_is_cancellable_and_keeps_attempt_identity():
    reached, attempts = asyncio.Event(), []

    async def sleep(_delay):
        if len(attempts) > 110:
            reached.set()
            await asyncio.Event().wait()

    async def attempted(_deps, run_id, step, number):
        attempts.append((run_id, step, number))

    def model(_messages, _info):
        raise ModelRateLimitError("synthetic", "private response")

    agent = Agent(FunctionModel(model), capabilities=[LocalModelPolicy(sleep=sleep, on_attempt=attempted)])
    task = asyncio.create_task(agent.run("Wait for provider availability."))
    await asyncio.wait_for(reached.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len({(run, step) for run, step, _ in attempts}) == 1
    assert [number for _, _, number in attempts] == list(range(1, len(attempts) + 1))


@run_async
async def test_each_provider_attempt_retains_its_request_deadline():
    calls = []

    async def model(_messages, _info):
        calls.append(True)
        if len(calls) < 3:
            await asyncio.sleep(0.1)
        return ModelResponse(parts=[TextPart("Ready.")])

    agent = Agent(FunctionModel(model), capabilities=[LocalModelPolicy(sleep=AsyncMock())])
    result = await agent.run("Review.", model_settings={"request_timeout": 0.02})
    assert result.output == "Ready." and len(calls) == 3


@run_async
async def test_authentication_and_unknown_program_errors_are_not_retried():
    for error in [ModelAuthenticationError("synthetic", "Invalid credentials"), AssertionError("bug")]:
        sent = []

        def model(_messages, _info, *, sent=sent, error=error):
            sent.append(True)
            raise error

        with pytest.raises(type(error)):
            await Agent(FunctionModel(model), capabilities=[LocalModelPolicy()]).run("Review.")
        assert len(sent) == 1


@run_async
async def test_actual_context_overflow_compacts_same_conversation_preserving_tool_pairs():
    calls, effects = [], []

    def model(messages, info):
        calls.append(list(messages))
        completed = [
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if len(calls) <= 4:
            return ModelResponse(parts=[ToolCallPart("read", {"target": str(len(calls))}, "read" + str(len(calls)))])
        if len(calls) == 5:
            raise ModelContextWindowExceededError("synthetic", "Full")
        assert len(messages) < len(calls[-2])
        assert "Critical role instructions" in str(messages)
        assert "Current required native choice" in str(messages)
        assert any(part.tool_call_id == "read4" for part in completed)
        return ModelResponse(parts=[TextPart("Completed after compaction.")])

    agent = Agent(FunctionModel(model), capabilities=[LocalModelPolicy()])

    @agent.tool_plain
    async def read(target: str):
        effects.append(target)
        return "Already delivered facts " + target + ": " + "x" * 8000

    result = await agent.run("Current required native choice", instructions="Critical role instructions")
    assert result.output == "Completed after compaction."
    assert effects == ["1", "2", "3", "4"] and len(calls) == 6
    assert result.usage.requests == 5
    assert result.conversation_id is not None


@run_async
async def test_proactive_compaction_limits_old_history_with_no_summary_model_requests():
    sizes = []
    settings = ModelPolicySettings(context_target_tokens=500)

    def model(messages, info):
        sizes.append(len(str(messages)))
        if len(sizes) <= 6:
            return ModelResponse(parts=[ToolCallPart("read", {}, "read" + str(len(sizes)))])
        return ModelResponse(parts=[TextPart("Done.")])

    agent = Agent(FunctionModel(model), capabilities=[context_compaction(settings), LocalModelPolicy()])

    @agent.tool_plain
    async def read():
        return "x" * 4000

    result = await agent.run("Keep the native mission.")
    assert result.output == "Done." and result.usage.requests == 7
    assert sizes[-1] < 24000


@run_async
async def test_unshrinkable_context_stays_waiting_and_is_cancellable():
    waiting = asyncio.Event()

    async def status(state, _delay):
        if state == "waiting_for_context":
            waiting.set()

    def model(_messages, _info):
        raise ModelContextWindowExceededError("synthetic", "Full")

    agent = Agent(FunctionModel(model), capabilities=[LocalModelPolicy(on_status=status)])
    task = asyncio.create_task(agent.run("Required context cannot be dropped."))
    await asyncio.wait_for(waiting.wait(), 2)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@run_async
async def test_maintained_cache_detector_logs_warning_without_prompt_contents(tmp_path, caplog):
    calls = []
    secret = "PRIVATE_PROMPT_MUST_NOT_APPEAR_IN_WARNING"

    def model(_messages, _info):
        index = len(calls)
        calls.append(True)
        usage = RequestUsage(input_tokens=12000, cache_read_tokens=10000 if index < 2 else 1000)
        return ModelResponse(
            parts=[ToolCallPart("player_turn", {}, "call" + str(index))]
            if index < 2
            else [ToolCallPart("finish_role", {"summary": "Done."}, "finish")],
            usage=usage,
            model_name="synthetic-cache-model",
            provider_name="synthetic",
            provider_url="https://synthetic.invalid",
        )

    async def invoke(_request, _identifier, _step, decision):
        return GatewayReceipt(result={"roleFinished": True} if decision["kind"] == "finish" else {"receipts": []})

    logging.captureWarnings(False)
    configure_cache_warning_logging()
    caplog.set_level(logging.WARNING, logger="py.warnings")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            runner = LocalAgentRunner(
                LocalAgentServices(invoke=invoke, step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"))
            )
            result = await runner.run_role(
                request(),
                startup={"user": secret, "instructions": "Review."},
                catalogue=[{"name": "finish_role", "inputSchema": {}}, {"name": "player_turn", "inputSchema": {}}],
                model=FunctionModel(model),
            )
        assert result.report.summary == "Done."
        records = [record for record in caplog.records if record.name == "py.warnings"]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "Cache hit collapsed" in records[0].message
        assert "10000" in records[0].message and "1000" in records[0].message
        assert secret not in records[0].message
    finally:
        logging.captureWarnings(False)


def test_repeated_cache_busts_each_emit_a_warning(caplog):
    from pydantic_ai_harness.warn_on_cache_busts import CacheBustWarning

    caplog.set_level(logging.WARNING, logger="py.warnings")
    try:
        with warnings.catch_warnings():
            configure_cache_warning_logging()
            for _ in range(2):
                warnings.warn(
                    CacheBustWarning(
                        "Cache hit collapsed",
                        reason="unknown",
                        established_tokens=10000,
                        cache_read_tokens=1000,
                        missed_tokens=9000,
                    ),
                    stacklevel=2,
                )
        assert len([record for record in caplog.records if record.name == "py.warnings"]) == 2
    finally:
        logging.captureWarnings(False)
