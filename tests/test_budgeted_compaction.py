"""Summaries use the owner's allowance and never clear mixed or effectful results."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelServerError, UsageLimitExceeded
from pydantic_ai.messages import (
    CompactionPart,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage, UsageLimits
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.execution_policy import CompactionPolicy
from civ_agent.orchestration.contracts import canonical
from civ_agent.orchestration.store import JournalConflict
from civ_agent.runtime.compaction import (
    EffectPreservingWindow,
    OwnerBoundSummary,
    SummaryServices,
    SupersededObservations,
)
from civ_agent.runtime.model_policy import LocalModelPolicy, ModelPolicySettings
from tests.domain_fixtures import run_async
from tests.test_local_agents import request


def history() -> list[ModelMessage]:
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("Resolve the required construction and research choices.")])
    ]
    for index in range(6):
        messages.extend(
            [
                ModelResponse(parts=[ToolCallPart("player_turn", {}, "turn" + str(index))]),
                ModelRequest(parts=[ToolReturnPart("player_turn", {"facts": "x" * 4000}, "turn" + str(index))]),
            ]
        )
    return messages


def context(model, *, requests=0, limit=10):
    return RunContext(
        deps=request(),
        model=model,
        usage=RunUsage(requests=requests),
        usage_limits=UsageLimits(request_limit=limit),
        run_id="parent-run",
        conversation_id="parent-conversation",
        run_step=7,
    )


def summarizer(tmp_path, *, callbacks=None, policy=None):
    store = SqliteStepStore(database=tmp_path / "steps.sqlite3")
    strategy = OwnerBoundSummary(
        max_messages=1,
        keep_messages=2,
        services=SummaryServices(store=store, metadata={"purpose": "gameplay"}, **(callbacks or {})),
        policy=policy or CompactionPolicy(summarize=True),
    )
    return strategy, store


@run_async
async def test_only_same_query_supersedes_observations_and_never_effects():
    model = FunctionModel(lambda _messages, _info: ModelResponse(parts=[TextPart("unused")]))
    messages = history()
    # Different query arguments must not supersede an older observation.
    messages[1] = ModelResponse(parts=[ToolCallPart("player_turn", {"city": "capital"}, "turn0")])
    # A read result carrying completion/error evidence is protected as well.
    messages[4] = ModelRequest(parts=[ToolReturnPart("player_turn", {"error": "NATIVE_CHOICE_REQUIRED"}, "turn1")])
    messages.extend(
        [
            ModelResponse(parts=[ToolCallPart("execute_commands", {}, "action")]),
            ModelRequest(parts=[ToolReturnPart("execute_commands", {"receipts": [{"id": "effect1"}]}, "action")]),
            ModelResponse(parts=[ToolCallPart("run_code", {"code": "print('mixed')"}, "program")]),
            ModelRequest(parts=[ToolReturnPart("run_code", "mixed action and observation", "program")]),
        ]
    )
    compacted = await SupersededObservations(max_tokens=1, keep_pairs=1).compact(messages, context(model))
    returns = {
        part.tool_call_id: part.content
        for message in compacted
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }
    assert returns["turn0"] == {"facts": "x" * 4000}
    assert returns["turn1"] == {"error": "NATIVE_CHOICE_REQUIRED"}
    assert returns["turn2"] == "[tool result cleared]"
    assert returns["turn5"] == {"facts": "x" * 4000}
    assert returns["action"] == {"receipts": [{"id": "effect1"}]}
    assert returns["program"] == "mixed action and observation"


@run_async
async def test_window_fallback_retains_complete_mixed_action_exchanges_and_latest_summary():
    model = FunctionModel(lambda _messages, _info: ModelResponse(parts=[TextPart("unused")]))
    messages = history()
    exchange = [
        ModelResponse(
            parts=[
                ToolCallPart("execute_commands", {}, "action"),
                ToolCallPart("player_turn", {}, "paired-read"),
            ]
        ),
        ModelRequest(parts=[ToolReturnPart("execute_commands", {"receipts": [{"id": "effect1"}]}, "action")]),
        ModelRequest(parts=[ToolReturnPart("player_turn", {"facts": "paired result"}, "paired-read")]),
        ModelResponse(parts=[ToolCallPart("run_code", {}, "program")]),
        ModelRequest(parts=[ToolReturnPart("run_code", "Unresolved effect; retrieve opaque-handle", "program")]),
    ]
    messages[3:3] = exchange
    messages.insert(1, ModelRequest(parts=[CompactionPart(content="Earlier decisions and exact receipt references")]))
    compacted = await EffectPreservingWindow(max_messages=1, keep_messages=2).compact(messages, context(model))
    assert len(compacted) < len(messages)
    assert "required construction" in str(compacted[0])
    assert all(message in compacted for message in exchange)
    assert "Earlier decisions and exact receipt references" in str(compacted)
    calls = {part.tool_call_id for message in compacted for part in message.parts if isinstance(part, ToolCallPart)}
    returns = {part.tool_call_id for message in compacted for part in message.parts if isinstance(part, ToolReturnPart)}
    assert calls == returns


@run_async
async def test_summary_charges_shared_usage_and_persists_owner_scope(tmp_path):
    attempts = []

    async def charged(owner, run_id, step, attempt):
        attempts.append((owner, run_id, step, attempt))

    def model(messages, info):
        assert info.model_settings["max_tokens"] == 2048
        assert "Resolve the required construction" in str(messages)
        return ModelResponse(
            parts=[
                TextPart("Required choices remain unresolved. No actions committed. Retrieve parent-run for details.")
            ],
            usage=RequestUsage(input_tokens=100, output_tokens=20),
        )

    owner = context(FunctionModel(model), requests=2)
    strategy, store = summarizer(tmp_path, callbacks={"on_attempt": charged})
    compacted = await strategy.compact(history(), owner)
    assert len(ModelMessagesTypeAdapter.dump_json(compacted)) < len(ModelMessagesTypeAdapter.dump_json(history()))
    assert owner.usage.requests == 3
    assert owner.usage.input_tokens == 100 and owner.usage.output_tokens == 20
    assert attempts == [(owner.deps, "parent-run", 7, 1)]
    assert "Resolve the required construction" in str(compacted[0])
    assert any(isinstance(part, CompactionPart) for message in compacted for part in message.parts)
    runs = await store.list_runs(parent_run_id="parent-run")
    assert len(runs) == 1 and runs[0].agent_name == "summarizing_compaction"
    assert runs[0].metadata["scope"] == canonical(owner.deps.scope.model_dump(mode="json"))
    assert runs[0].metadata["purpose"] == "context_summary"
    assert await store.latest_snapshot(run_id=runs[0].run_id) is not None


@run_async
async def test_summary_incrementally_updates_previous_assessment(tmp_path):
    prompts = []

    def model(messages, _info):
        prompts.append(str(messages))
        return ModelResponse(
            parts=[TextPart("Research selected; preserve exact receipt effect-1 and unresolved construction.")]
        )

    strategy, _store = summarizer(tmp_path)
    owner = context(FunctionModel(model))
    first = await strategy.compact(history(), owner)
    updated = [*first, *history()[1:]]
    owner.run_step += 1
    second = await strategy.compact(updated, owner)
    assert len(prompts) == 2 and "<previous-summary>" in prompts[1]
    assert "effect-1" in prompts[1]
    assert sum(isinstance(part, CompactionPart) for message in second for part in message.parts) == 1
    assert owner.usage.requests == 2


@run_async
async def test_latest_user_steering_survives_summary_and_window_beside_original_mission(tmp_path):
    def model(_messages, _info):
        return ModelResponse(parts=[TextPart("Earlier observations and decisions.")])

    strategy, _store = summarizer(tmp_path)
    messages = history()
    latest = ModelRequest(parts=[UserPromptPart("CURRENT TASK: extract exactly the three requested facts as JSON.")])
    messages.insert(-4, latest)
    owner = context(FunctionModel(model))
    summarized = await strategy.compact(messages, owner)
    windowed = await EffectPreservingWindow(max_messages=1, keep_messages=2).compact(messages, owner)
    for compacted in (summarized, windowed):
        assert "Resolve the required construction" in str(compacted)
        assert latest in compacted
        assert len(ModelMessagesTypeAdapter.dump_json(compacted)) < len(ModelMessagesTypeAdapter.dump_json(messages))


@run_async
async def test_summary_ignores_retained_parent_request_cap_and_charges_usage(tmp_path):
    calls = []

    def model(_messages, _info):
        calls.append(True)
        return ModelResponse(parts=[TextPart("summary")])

    strategy, _store = summarizer(tmp_path)
    original = history()
    owner = context(FunctionModel(model), requests=9, limit=10)
    compacted = await strategy.compact(original, owner)
    assert compacted != original
    assert calls == [True] and owner.usage.requests == 10


@run_async
async def test_summary_retries_transient_errors_past_legacy_attempt_allowances(tmp_path):
    from unittest.mock import AsyncMock, patch

    calls = 0

    def model(_messages, _info):
        nonlocal calls
        calls += 1
        if calls <= 25:
            raise ModelServerError("test", "Transient failure", retry_after=0)
        return ModelResponse(parts=[TextPart("A smaller summary of the history.")])

    strategy, _store = summarizer(tmp_path, callbacks={"remaining_attempts": lambda _owner: 1})
    with patch(
        "civ_agent.runtime.model_policy.LocalModelPolicy",
        lambda **kwargs: LocalModelPolicy(
            **kwargs, settings=ModelPolicySettings(initial_delay=0, maximum_delay=0), sleep=AsyncMock()
        ),
    ):
        owner = context(FunctionModel(model), requests=0, limit=10)
        original = history()
        assert await strategy.compact(original, owner) != original
    assert calls == 26 and owner.usage.requests == 1


@pytest.mark.parametrize(
    "failure", [JournalConflict("STALE_AUTHORITY"), UsageLimitExceeded("whole game stop"), asyncio.CancelledError()]
)
@run_async
async def test_summary_never_swallows_integrity_admission_or_cancellation(tmp_path, failure):
    async def stopped(*_args):
        raise failure

    def model(_messages, _info):
        raise AssertionError("The request must not be dispatched")

    strategy, _store = summarizer(tmp_path, callbacks={"on_attempt": stopped})
    with pytest.raises(type(failure)):
        await strategy.compact(history(), context(FunctionModel(model)))


@run_async
async def test_summary_preserves_foreground_deadline(tmp_path):
    async def waiting(*_args):
        await asyncio.sleep(1)

    def model(_messages, _info):
        raise AssertionError("The original foreground deadline must stop summary dispatch")

    strategy, _store = summarizer(tmp_path, callbacks={"on_attempt": waiting})
    owner = context(FunctionModel(model))
    owner.deadline = datetime.now(UTC) + timedelta(milliseconds=20)
    deadline = owner.deadline
    with pytest.raises(TimeoutError):
        await strategy.compact(history(), owner)
    assert owner.deadline == deadline


@run_async
async def test_expected_provider_failure_and_larger_summary_allow_deterministic_fallback(tmp_path):
    calls = []

    def model(_messages, _info):
        calls.append(True)
        if len(calls) == 1:
            raise ModelServerError("synthetic", "Unavailable")
        return ModelResponse(parts=[TextPart("x" * 40000)])

    strategy, _store = summarizer(tmp_path)
    original = history()
    owner = context(FunctionModel(model))
    assert await strategy.compact(original, owner) is original
    assert await strategy.compact(original, owner) is original
    assert len(calls) == 3
