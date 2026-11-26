import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import httpx2
import pytest
from pydantic import BaseModel
from pydantic_ai import Agent, ConcurrencyLimiter, ModelRetry
from pydantic_ai.exceptions import (
    ModelConnectionError,
    ModelHTTPError,
    ModelOverloadedError,
    ModelRateLimitError,
    ModelServerError,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import UsageLimits
from sqlalchemy import select

from civ_agent.orchestration.providers import BrokerAuth
from civ_agent.provider_attempts import (
    AttemptAdmission,
    PilotAdmissionRetained,
    PilotOutcomeUncertain,
    ProviderAttemptAccount,
    attempts,
)
from civ_agent.runtime.capabilities import ContextUsageTelemetry
from civ_agent.runtime.model_policy import LocalModelPolicy
from tests.domain_fixtures import run_async


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("kind", [ModelOverloadedError, ModelRateLimitError, ModelServerError, ModelConnectionError])
@pytest.mark.parametrize("failure_phase", ["opening", "streaming"])
@pytest.mark.parametrize("failures", [2, 105])
@run_async
async def test_unlimited_tracking_retries_transient_errors_without_repeating_tools(
    tmp_path, streamed, kind, failure_phase, failures
):
    account = ProviderAttemptAccount(tmp_path / "provider-attempts.sqlite3", ceiling=None, pilot=False)
    sent, effects = [], []
    sleep = AsyncMock()

    def response(messages, info):
        sent.append(list(messages))
        if len(sent) == 1:
            return ModelResponse(parts=[ToolCallPart("act", {}, "act-once")])
        if len(sent) <= failures + 1:
            raise kind("test-model", "Transient failure", retry_after=3)
        return ModelResponse(parts=[TextPart("Done")])

    async def stream(messages, info):
        try:
            result = response(messages, info)
        except kind:
            if failure_phase == "streaming":
                yield {0: DeltaToolCall(name="act", json_args='{"unfinished":', tool_call_id="discarded")}
            raise
        part = result.parts[0]
        if isinstance(part, ToolCallPart):
            yield {0: DeltaToolCall(name=part.tool_name, json_args="{}", tool_call_id=part.tool_call_id)}
        else:
            yield "Done"

    agent = Agent(
        FunctionModel(stream_function=stream) if streamed else FunctionModel(response),
        capabilities=[
            AttemptAdmission(budget=account, namespace="gameplay"),
            LocalModelPolicy(sleep=sleep),
            *([ContextUsageTelemetry()] if streamed else []),
        ],
    )

    @agent.tool_plain
    def act():
        effects.append("committed")
        return "Action receipt"

    result = await agent.run("Act once and report the receipt.")
    assert result.output == "Done" and effects == ["committed"]
    assert len(sent) == failures + 2 and all(messages == sent[1] for messages in sent[2:])
    assert [call.args[0] for call in sleep.await_args_list] == [
        max(3, min(60, 2 * 2 ** min(i, 20))) for i in range(failures)
    ]
    assert any(
        isinstance(part, ToolReturnPart) and part.content == "Action receipt"
        for message in sent[-1]
        if isinstance(message, ModelRequest)
        for part in message.parts
    )
    report = account.report()
    assert report["ceiling"] is None and report["state"] == "OPEN" and report["attempts"] == failures + 2
    assert report["components"]["gameplay"]["completed"] == 2
    assert report["unknown_attempts"] == failures and report["unknown_usage"]
    if streamed and failure_phase == "streaming":
        for token in ("input_tokens", "output_tokens", "cache_read_tokens"):
            assert report["components"]["gameplay"][token] == getattr(result.usage, token)
    reopened = ProviderAttemptAccount(tmp_path / "provider-attempts.sqlite3", ceiling=None, pilot=False)
    assert reopened.report() == report


@pytest.mark.parametrize("raw_transport", [False, True])
@run_async
async def test_failed_stream_usage_stops_retries_before_exceeding_token_budget(tmp_path, raw_transport):
    account = ProviderAttemptAccount(tmp_path / "provider.sqlite3", ceiling=None, pilot=False)
    calls = []

    async def stream(_messages, _info):
        calls.append(True)
        yield "Partial output which must still count against the token allowance."
        if raw_transport:
            raise httpx2.ReadError("Disconnected")
        raise ModelConnectionError("test-model", "Disconnected", in_stream=True)

    agent = Agent(
        FunctionModel(stream_function=stream),
        capabilities=[
            AttemptAdmission(budget=account, namespace="play"),
            LocalModelPolicy(sleep=AsyncMock()),
            ContextUsageTelemetry(),
        ],
    )
    with pytest.raises(UsageLimitExceeded):
        await agent.run("Review this position.", usage_limits=UsageLimits(total_tokens_limit=1))
    assert len(calls) == 1 and account.report()["attempts"] == 1


@run_async
async def test_unlimited_tracking_preserves_http_503_for_the_retry_policy(tmp_path):
    account = ProviderAttemptAccount(tmp_path / "requests.sqlite3", ceiling=None, pilot=False)
    calls = []

    async def stream(messages, info):
        calls.append(True)
        if len(calls) == 1:
            raise ModelHTTPError.for_category(
                ModelOverloadedError, status_code=503, model_name="test-model", in_stream=True
            )
        yield "Recovered"

    agent = Agent(
        FunctionModel(stream_function=stream),
        capabilities=[
            AttemptAdmission(budget=account, namespace="gameplay"),
            LocalModelPolicy(sleep=AsyncMock()),
            ContextUsageTelemetry(),
        ],
    )
    assert (await agent.run("Continue")).output == "Recovered"
    assert len(calls) == account.report()["attempts"] == 2
    assert account.report()["state"] == "OPEN"


@run_async
async def test_shared_model_capacity_precedes_admission_and_canceled_waiters_are_not_sends(tmp_path):
    account = ProviderAttemptAccount(tmp_path / "capacity.sqlite3")
    limiter = ConcurrencyLimiter(1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def model(messages, info):
        entered.set()
        await release.wait()
        return ModelResponse(parts=[TextPart("done")])

    def agent(name):
        return Agent(
            FunctionModel(model), capabilities=[AttemptAdmission(budget=account, namespace=name, limiter=limiter)]
        )

    active = asyncio.create_task(agent("active").run("Assess."))
    await entered.wait()
    queued = asyncio.create_task(agent("queued").run("Assess."))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert account.report()["attempts"] == 1
    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    assert account.report()["state"] == "OPEN" and account.report()["attempts"] == 1
    release.set()
    await active
    assert account.report()["components"]["active"]["completed"] == 1


@run_async
async def test_shared_account_admits_at_most_ceiling_across_parallel_components(tmp_path):
    account = ProviderAttemptAccount(tmp_path / "budget.sqlite3", ceiling=7)
    sent = []

    async def model(messages, info):
        sent.append(1)
        await asyncio.sleep(0)
        return ModelResponse(parts=[TextPart("done")])

    async def run(index):
        agent = Agent(
            FunctionModel(model),
            capabilities=[AttemptAdmission(budget=account, namespace="adviser" if index % 2 else "executive")],
        )
        return await agent.run("Assess.")

    results = await asyncio.gather(*(run(index) for index in range(14)), return_exceptions=True)
    assert len(sent) == 7 and sum(isinstance(row, UsageLimitExceeded) for row in results) == 7
    report = account.report()
    assert report["attempts"] == 7 and report["state"] == "OPEN"
    assert sum(row["completed"] for row in report["components"].values()) == 7


@run_async
async def test_uncertain_provider_call_fences_every_component_and_restart(tmp_path):
    path = tmp_path / "budget.sqlite3"
    account = ProviderAttemptAccount(path)
    sent = []

    def model(messages, info):
        sent.append(1)
        raise ModelServerError("test-model", "uncertain transport")

    for namespace in ("executive", "adviser", "review", "summary", "classifier"):
        agent = Agent(FunctionModel(model), capabilities=[AttemptAdmission(budget=account, namespace=namespace)])
        with pytest.raises(PilotOutcomeUncertain):
            await agent.run("Assess.")
    assert len(sent) == 1 and account.report()["attempts"] == 1
    restored = ProviderAttemptAccount(path)
    with pytest.raises(PilotOutcomeUncertain):
        restored.admit("new", "other", {})
    with account.engine.connect() as db:
        diagnostic = json.loads(db.scalar(select(attempts.c.usage)))
    assert diagnostic["failure_chain"][0]["type"] == "pydantic_ai.exceptions.ModelServerError"
    assert "uncertain transport" not in json.dumps(diagnostic)


def test_retained_and_crashed_admissions_never_resend(tmp_path):
    path = tmp_path / "budget.sqlite3"
    account = ProviderAttemptAccount(path)
    account.admit("original", "executive", {})
    with pytest.raises(PilotAdmissionRetained):
        account.admit("original", "executive", {})
    restored = ProviderAttemptAccount(path)
    assert restored.report()["state"] == "UNCERTAIN"
    with pytest.raises(PilotOutcomeUncertain):
        restored.admit("replacement", "executive", {})


def test_extended_evaluation_requires_explicit_larger_ceiling_and_cannot_change_it(tmp_path):
    path = tmp_path / "extended.sqlite3"
    with pytest.raises(ValueError):
        ProviderAttemptAccount(path, ceiling=40000)
    account = ProviderAttemptAccount(path, ceiling=40000, pilot=False)
    account.admit("long-game", "executive", {})
    assert account.report()["ceiling"] == 40000
    with pytest.raises(ValueError, match="CEILING_CHANGED"):
        ProviderAttemptAccount(path, ceiling=50000, pilot=False)


def test_limited_views_share_one_ledger_and_leave_the_reserve_for_the_full_account(tmp_path):
    account = ProviderAttemptAccount(tmp_path / "reserved.sqlite3", ceiling=5)
    play = account.limited(3)
    tighter = play.limited(2)
    assert play.engine is tighter.engine is account.engine
    for index in range(2):
        tighter.admit(str(index), "play", {})
        tighter.complete(str(index), ModelResponse(parts=[TextPart("complete")]))
    with pytest.raises(UsageLimitExceeded):
        tighter.admit("denied-tight", "play", {})
    # A nested wider request cannot remove an already established limit.
    with pytest.raises(UsageLimitExceeded):
        tighter.limited(100).admit("still-denied", "play", {})
    play.admit("third", "play", {})
    play.complete("third", ModelResponse(parts=[TextPart("complete")]))
    with pytest.raises(UsageLimitExceeded):
        play.admit("denied-play", "play", {}, max_attempts=100)
    assert play.report() == account.report()
    assert play.report()["ceiling"] == 5 and play.report()["attempts"] == 3
    for identity in ("assessment-1", "assessment-2"):
        account.admit(identity, "assessment", {})
        account.complete(identity, ModelResponse(parts=[TextPart("complete")]))
    with pytest.raises(UsageLimitExceeded):
        account.limited(100).admit("global-overrun", "assessment", {})
    assert account.report()["attempts"] == 5
    assert account.report()["components"]["assessment"]["completed"] == 2


@pytest.mark.parametrize("value", [0, -1, True, "2", 2.0, None])
def test_limited_view_rejects_invalid_limits_without_admission(tmp_path, value):
    account = ProviderAttemptAccount(tmp_path / "invalid.sqlite3")
    with pytest.raises(ValueError, match="POSITIVE_INTEGER"):
        account.limited(value)
    if value is not None:  # None preserves the original unbounded-to-global call.
        with pytest.raises(ValueError, match="POSITIVE_INTEGER"):
            account.admit("invalid", "play", {}, max_attempts=value)
    assert account.report()["attempts"] == 0


def test_uncertain_admission_through_a_view_fences_every_view_and_reserved_capacity(tmp_path):
    account = ProviderAttemptAccount(tmp_path / "unknown.sqlite3", ceiling=5)
    play = account.limited(3)
    play.admit("unknown", "play", {})
    play.uncertain("unknown", RuntimeError("not included"), phase="stream")
    for budget in (account, play, account.limited(5)):
        with pytest.raises(PilotOutcomeUncertain):
            budget.admit("new", "assessment", {})
        assert budget.report()["state"] == "UNCERTAIN" and budget.report()["attempts"] == 1


def test_parallel_view_admission_cannot_consume_reserved_global_capacity(tmp_path):
    account = ProviderAttemptAccount(tmp_path / "parallel-reserve.sqlite3", ceiling=7)
    play = account.limited(4)

    def admit(index):
        try:
            play.admit(str(index), "play", {})
            return True
        except UsageLimitExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(admit, range(12)))
    assert sum(admitted) == account.report()["attempts"] == 4
    for index in range(3):
        account.admit("reserved-" + str(index), "assessment", {})
    assert account.report()["attempts"] == 7


@run_async
async def test_sdk_admission_view_stops_play_without_fencing_later_assessment(tmp_path):
    account = ProviderAttemptAccount(tmp_path / "sdk-reserve.sqlite3", ceiling=3)
    sent = []

    def model(messages, info):
        sent.append(1)
        return ModelResponse(parts=[TextPart("complete")])

    play = Agent(FunctionModel(model), capabilities=[AttemptAdmission(budget=account.limited(2), namespace="play")])
    for _ in range(2):
        await play.run("Play.")
    with pytest.raises(UsageLimitExceeded):
        await play.run("No third play request.")
    assert account.report()["state"] == "OPEN"
    judge = Agent(FunctionModel(model), capabilities=[AttemptAdmission(budget=account, namespace="assessment")])
    await judge.run("Assess.")
    assert len(sent) == account.report()["attempts"] == 3


@run_async
async def test_pilot_auth_does_not_add_an_unadmitted_send_after_401():
    broker = AsyncMock()
    broker.fetch.return_value = {"accessToken": "token", "chatgptAccountId": "account"}
    sent = []

    def respond(request):
        sent.append(request)
        return httpx2.Response(401)

    async with httpx2.AsyncClient(
        auth=BrokerAuth(broker, renew_after_unauthorized=False), transport=httpx2.MockTransport(respond)
    ) as client:
        result = await client.post("https://chatgpt.com/backend-api/codex/responses", json={})
    assert result.status_code == 401 and len(sent) == 1
    broker.fetch.assert_awaited_once_with(refresh=False, generation=None)


@pytest.mark.parametrize("business_validator", [False, True])
@run_async
async def test_streamed_validation_retry_retains_both_completed_admissions(tmp_path, business_validator):
    class Report(BaseModel):
        value: int

    account = ProviderAttemptAccount(tmp_path / "stream.sqlite3")
    sent, validated = [], []

    async def stream(messages, info):
        sent.append(1)
        value = "bad" if len(sent) == 1 and not business_validator else 7
        yield {
            0: DeltaToolCall(
                name=info.output_tools[0].name, json_args=json.dumps({"value": value}), tool_call_id=str(len(sent))
            )
        }

    agent = Agent(
        FunctionModel(stream_function=stream),
        output_type=Report,
        retries=2,
        capabilities=[AttemptAdmission(budget=account, namespace="stream-validation"), ContextUsageTelemetry()],
    )
    if business_validator:

        @agent.output_validator
        async def validate(ctx, report):
            validated.append(1)
            await asyncio.sleep(0)
            if len(validated) == 1:
                raise ModelRetry("Correct the evidence reference.")
            return report

    result = await agent.run("Return a report.")
    assert result.output.value == 7 and len(sent) == 2
    report = account.report()
    assert report["state"] == "OPEN" and report["attempts"] == 2
    assert report["components"]["stream-validation"]["completed"] == 2
