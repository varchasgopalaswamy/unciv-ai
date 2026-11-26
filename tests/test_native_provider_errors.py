"""Native provider categories and privacy survive ordinary local agent execution."""

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    ModelAuthenticationError,
    ModelContextWindowExceededError,
    ModelHTTPError,
    ModelNotFoundError,
    ModelOverloadedError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelRequestRejectedError,
    ModelServerError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openai_codex import OpenAICodexProvider
from pydantic_ai.usage import RequestUsage

from civ_agent.orchestration.provider_errors import sanitize_model_error
from civ_agent.runtime.capabilities import ContextUsageTelemetry
from civ_agent.runtime.model_policy import LocalModelPolicy, SanitizeProviderErrors

SECRET = "provider-secret-must-not-cross-native-failure-boundary"
RESET = 2000000000


@pytest.mark.parametrize("subscription", [False, True])
def test_real_provider_adapter_recovers_a_transport_failure_after_streamed_text(subscription):
    async def run():
        calls = []
        closed = []

        class ResponseStream(httpx2.AsyncByteStream):
            def __init__(self, number):
                self.number = number

            async def __aiter__(self):
                response: dict[str, object] = {
                    "id": "response-" + str(self.number),
                    "object": "response",
                    "created_at": 1,
                    "model": "synthetic-model",
                    "status": "in_progress",
                    "output": [],
                }
                events = [
                    {"type": "response.created", "sequence_number": 0, "response": response},
                    {
                        "type": "response.output_text.delta",
                        "sequence_number": 1,
                        "content_index": 0,
                        "output_index": 0,
                        "item_id": "message",
                        "delta": "Partial" if self.number == 1 else "Recovered",
                        "logprobs": [],
                    },
                ]
                for event in events:
                    yield ("data: " + json.dumps(event) + "\n\n").encode()
                if self.number == 1:
                    raise httpx2.ReadError(SECRET)
                response.update(
                    status="completed",
                    usage={
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "total_tokens": 12,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                )
                event = {"type": "response.completed", "sequence_number": 2, "response": response}
                yield ("data: " + json.dumps(event) + "\n\n").encode()

            async def aclose(self):
                closed.append(self.number)

        async def respond(request):
            calls.append(json.loads(request.content))
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=ResponseStream(len(calls))
            )

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False) as http:
            async with AsyncOpenAI(api_key="synthetic", http_client=http, max_retries=0) as sdk:
                sdk.platform_headers()
                provider = OpenAICodexProvider(openai_client=sdk) if subscription else OpenAIProvider(openai_client=sdk)
                model = (
                    OpenAICodexModel("synthetic-model", provider=provider)
                    if subscription
                    else OpenAIResponsesModel("synthetic-model", provider=provider)
                )
                sleep = AsyncMock()
                agent = Agent(
                    model,
                    capabilities=[SanitizeProviderErrors(), LocalModelPolicy(sleep=sleep), ContextUsageTelemetry()],
                )
                result = await agent.run("Synthetic transport recovery test.")
                assert result.output == "Recovered" and len(calls) == 2 and calls[0] == calls[1]
                assert SECRET not in str(result.all_messages()) and set(closed) == {1, 2}
                sleep.assert_awaited_once_with(2)

    asyncio.run(run())


@pytest.mark.parametrize(
    "subscription,status,code,error_type,category",
    [
        (True, 429, None, "usage_limit_reached", ModelQuotaExceededError),
        (True, 429, "usage_limit_reached", None, ModelQuotaExceededError),
        (False, 429, None, "usage_limit_reached", ModelRateLimitError),
        (True, 429, "rate_limit_exceeded", "requests", ModelRateLimitError),
        (False, 429, "insufficient_quota", "insufficient_quota", ModelQuotaExceededError),
        (True, 429, None, SECRET, ModelRateLimitError),
        (True, 403, None, "usage_limit_reached", ModelAuthenticationError),
    ],
)
def test_synthetic_http_preserves_exact_native_categories_and_sanitizes_diagnostics(
    subscription,
    status,
    code,
    error_type,
    category,
):
    async def run():
        seen = []

        async def respond(request):
            seen.append(request.url.path)
            return httpx2.Response(
                status,
                json={
                    "error": {
                        "message": SECRET + " quota exhausted",
                        "type": error_type,
                        "code": code,
                        "resets_at": RESET,
                        "resets_in_seconds": 300,
                        "plan_type": SECRET,
                    }
                },
                headers={"retry-after": "7", "x-should-retry": "false", "x-private": SECRET},
            )

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False) as http:
            async with AsyncOpenAI(
                api_key="synthetic",
                base_url="https://chatgpt.com/backend-api/codex" if subscription else "https://api.openai.com/v1",
                http_client=http,
                max_retries=0,
            ) as sdk:
                sdk.platform_headers()
                provider = OpenAICodexProvider(openai_client=sdk) if subscription else OpenAIProvider(openai_client=sdk)
                native = (
                    OpenAICodexModel("synthetic-model", provider=provider)
                    if subscription
                    else OpenAIResponsesModel("synthetic-model", provider=provider)
                )
                agent = Agent(native, capabilities=[SanitizeProviderErrors()], retries=0)
                with pytest.raises(category) as caught:
                    async with asyncio.timeout(10):
                        await agent.run("Synthetic test only.")
                error = caught.value
                assert isinstance(error, ModelHTTPError)
                assert error.status_code == status and not error.in_stream
                assert error.retry_after == 7 and error.provider_retry_hint is False
                assert error.body == {"resets_at": RESET, "resets_in_seconds": 300.0}
                assert error.headers == {"x-should-retry": "false"}
                assert error.__cause__ is None and error.__suppress_context__
                assert SECRET not in str(error) and SECRET not in repr(error.__getstate__())
                assert error.provider_error_type == (None if error_type == SECRET else error_type)
        assert seen == ["/backend-api/codex/responses" if subscription else "/v1/responses"]

    asyncio.run(run())


def test_successful_local_response_preserves_all_native_usage():
    async def run():
        seen = []

        def respond(messages, info):
            seen.append(messages)
            return ModelResponse(
                [TextPart("done")],
                usage=RequestUsage(
                    input_tokens=10,
                    cache_read_tokens=3,
                    output_tokens=7,
                    details={"reasoning_tokens": 2},
                ),
            )

        result = await Agent(FunctionModel(respond), capabilities=[SanitizeProviderErrors()]).run("Synthetic only.")
        assert result.output == "done" and len(seen) == 1
        observed = result.usage
        assert observed.input_tokens == 10 and observed.cache_read_tokens == 3
        assert observed.output_tokens == 7 and observed.details["reasoning_tokens"] == 2

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["pool", "connect", "write", "read", None])
def test_local_transport_phase_and_privacy_are_preserved_without_implicit_retry(phase):
    async def run():
        seen = []

        def respond(messages, info):
            seen.append(messages)
            raise ModelTimeoutError("synthetic-model", SECRET, phase=phase, body={"message": SECRET}) from RuntimeError(
                SECRET
            )

        agent = Agent(
            FunctionModel(respond, model_name="synthetic-model"), capabilities=[SanitizeProviderErrors()], retries=0
        )
        with pytest.raises(ModelTimeoutError) as caught:
            await agent.run("Synthetic only.")
        assert caught.value.phase == phase and caught.value.body is None
        assert SECRET not in str(caught.value) and caught.value.__cause__ is None
        assert len(seen) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("category", "status"),
    [
        (ModelRateLimitError, 429),
        (ModelQuotaExceededError, 402),
        (ModelUnavailableError, 503),
        (ModelOverloadedError, 529),
        (ModelServerError, 500),
        (ModelContextWindowExceededError, 400),
        (ModelAuthenticationError, 401),
        (ModelNotFoundError, 404),
        (ModelRequestRejectedError, 422),
    ],
)
def test_native_http_categories_and_hints_preserve_native_serialization(category, status):
    original = ModelHTTPError.for_category(
        category,
        status_code=status,
        model_name="synthetic-model",
        body={"error": {"resets_at": RESET, "message": SECRET}},
        headers={"x-private": SECRET, "x-should-retry": " TRUE "},
        hint=SECRET,
        suggested_model_id=SECRET,
        retry_after=3.5,
        in_stream=True,
    )
    sanitized = sanitize_model_error(original, system="openai", model_name="synthetic-model")
    recovered = copy.deepcopy(sanitized)
    assert type(original) is type(sanitized) is type(recovered)
    assert isinstance(recovered, category) and isinstance(recovered, ModelHTTPError)
    assert recovered.status_code == status and recovered.in_stream
    assert recovered.retry_after == 3.5 and recovered.provider_retry_hint is True
    assert recovered.body == {"resets_at": RESET}
    assert SECRET not in str(recovered) and SECRET not in repr(recovered.__getstate__())


@pytest.mark.parametrize("invalid", [True, -1, "300", float("nan"), float("inf")])
def test_retry_and_reset_hints_require_numeric_finite_nonnegative_values(invalid):
    error = ModelHTTPError.for_category(
        ModelRateLimitError,
        status_code=429,
        model_name="synthetic-model",
        body={"resets_at": invalid, "resets_in_seconds": invalid, "message": SECRET},
        headers={"x-private": SECRET},
    )
    error.retry_after = invalid
    sanitized = sanitize_model_error(error, system="openai", model_name="synthetic-model")
    assert isinstance(sanitized, ModelRateLimitError) and isinstance(sanitized, ModelHTTPError)
    assert sanitized.body is None and sanitized.retry_after is None and sanitized.headers is None
