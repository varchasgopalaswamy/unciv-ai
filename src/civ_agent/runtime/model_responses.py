"""Collect complete model responses before exposing executable tool calls."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai._model_request_attempts import AttemptStart, carried_attempts, carry_attempts
from pydantic_ai.messages import ModelMessage, ModelRequestAttempt, ModelResponse
from pydantic_ai.models import CompletedStreamedResponse, ModelRequestParameters
from pydantic_ai.models._request_timeout import (
    carry_deadline,
    enforce_request_deadline,
    resume_deadline,
    start_request_deadline,
    stream_under_deadline,
)
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

provider_attempt_ordinal: ContextVar[int] = ContextVar("provider_attempt_ordinal", default=1)


def retain_partial_attempt(error, stream, model, started):
    """Describe billed partial output once, before its admission is settled."""
    if stream is None or carried_attempts(error):
        return
    usage = stream.get().usage
    if usage.has_values() or usage.unmeasured_requests:
        carry_attempts(
            error,
            [
                ModelRequestAttempt(
                    model_name=model.model_name,
                    provider_name=model.system,
                    outcome="error",
                    error=type(error).__name__,
                    timestamp=started.timestamp,
                    duration=started.elapsed(),
                    usage=usage,
                )
            ],
        )


class CompleteResponseModel(WrapperModel):
    def __init__(
        self,
        wrapped,
        *,
        retry: Callable[[Exception], Awaitable[bool]] | None = None,
        before_retry: Callable[[], Awaitable[None]] | None = None,
    ):
        super().__init__(wrapped)
        self.retry, self.before_retry = retry, before_retry

    def _start_request_deadline(self, model_settings, *, stream):
        # Deadlines belong to each live provider attempt, before backoff/replay.
        return None

    async def _request(self, send):
        """Wait below the SDK's finite hook-attempt guard, without changing history."""
        failures, ordinal = [], 1
        try:
            while True:
                token = provider_attempt_ordinal.set(ordinal)
                started = AttemptStart()
                try:
                    if ordinal > 1 and self.before_retry is not None:
                        await self.before_retry()
                    response = await send()
                except Exception as error:
                    if self.retry is None:
                        raise
                    failures.extend(
                        [replace(attempt, error=type(error).__name__) for attempt in carried_attempts(error)]
                        or [
                            ModelRequestAttempt(
                                model_name=self.model_name,
                                provider_name=self.system,
                                outcome="error",
                                error=type(error).__name__,
                                timestamp=started.timestamp,
                                duration=started.elapsed(),
                            )
                        ]
                    )
                    if not await self.retry(error):
                        raise
                else:
                    response.failed_attempts = [*failures, *(response.failed_attempts or ())] or None
                    return response
                finally:
                    provider_attempt_ordinal.reset(token)
                ordinal += 1
        except BaseException as error:
            carry_attempts(error, failures)
            raise

    async def request(self, messages, model_settings, model_request_parameters):
        async def send():
            deadline = start_request_deadline(self.wrapped, model_settings, stream=False)
            with enforce_request_deadline(deadline):
                return await self.wrapped.request(messages, model_settings, model_request_parameters)

        return await self._request(send)

    async def _stream_response(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> ModelResponse:
        deadline = start_request_deadline(self.wrapped, model_settings, stream=True)
        if deadline is not None and messages and isinstance(last := messages[-1], ModelResponse):
            if last.state == "suspended":
                deadline = resume_deadline(deadline, last)
        started = AttemptStart()
        stream = None
        try:
            async with stream_under_deadline(
                deadline,
                self.wrapped.request_stream(messages, model_settings, model_request_parameters, run_context),
            ) as stream:
                async for _event in stream:
                    pass
                response = stream.get()
            if response.state == "suspended":
                carry_deadline(response, deadline)
        except BaseException as error:
            retain_partial_attempt(error, stream, self, started)
            raise
        return response

    @asynccontextmanager
    async def request_stream(
        self, messages, model_settings, model_request_parameters, run_context=None
    ) -> AsyncIterator[CompletedStreamedResponse]:
        response = await self._request(
            lambda: self._stream_response(messages, model_settings, model_request_parameters, run_context)
        )
        yield CompletedStreamedResponse(response, model_request_parameters=model_request_parameters, replay_events=True)
