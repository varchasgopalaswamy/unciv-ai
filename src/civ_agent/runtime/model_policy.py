"""Provider recovery for ordinary agent runs; native effects remain the host's job."""

import asyncio
import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from math import isfinite
from typing import Any

import httpx2
from pydantic_ai import RetryModelRequest, RunContext
from pydantic_ai._model_request_attempts import carried_attempts
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering
from pydantic_ai.exceptions import (
    ContentFilterError,
    ModelAPIError,
    ModelConnectionError,
    ModelContextWindowExceededError,
    ModelOverloadedError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelRequestTimeout,
    ModelServerError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness import (
    ClearToolResults,
    Planning,
    ReportContextUsage,
    SlidingWindowCompaction,
    TieredCompaction,
)
from pydantic_ai_harness.compaction import CompactionStrategy, compact_now

from civ_agent.execution_policy import CompactionPolicy
from civ_agent.orchestration.provider_errors import sanitize_model_error
from civ_agent.provider_attempts import AttemptAdmission
from civ_agent.runtime.compaction import (
    SUMMARY_INSTRUCTIONS,
    SUMMARY_PROMPT,
    EffectPreservingWindow,
    OwnerBoundSummary,
    SummaryServices,
    SupersededObservations,
)
from civ_agent.runtime.model_responses import CompleteResponseModel

type StatusCallback = Callable[[str, float], Awaitable[None]]
type AttemptCallback = Callable[[Any, str, int, int], Awaitable[None]]


class SanitizeProviderErrors(AbstractCapability[Any]):
    """Keep native error categories while excluding raw diagnostics from SDK persistence."""

    id = "sanitized-provider-errors"

    async def on_model_request_error(self, ctx, *, request_context, error):
        if isinstance(error, ModelAPIError):
            raise sanitize_model_error(
                error, system=request_context.model.system, model_name=request_context.model.model_name
            ) from None
        raise error


class FreshConversationRequired(Exception):
    """Ask the controller for fresh native context, without rewinding the game."""

    def __init__(self, reason: str, *, retry_after: float = 0):
        self.reason, self.retry_after = reason, retry_after
        super().__init__(reason)


@dataclass(frozen=True)
class ModelPolicySettings:
    initial_delay: float = 2
    maximum_delay: float = 60
    context_target_tokens: int | None = None

    def __post_init__(self):
        if (
            not isfinite(self.initial_delay)
            or not isfinite(self.maximum_delay)
            or self.initial_delay < 0
            or self.maximum_delay < self.initial_delay
            or self.context_target_tokens is not None
            and self.context_target_tokens < 1
        ):
            raise ValueError("INVALID_MODEL_RETRY_POLICY")


class LocalModelPolicy(AbstractCapability[Any]):
    """Wait on provider failures without abandoning the current model request."""

    id = "local-model-policy"

    def __init__(
        self,
        settings: ModelPolicySettings | None = None,
        *,
        on_status: StatusCallback | None = None,
        on_attempt: AttemptCallback | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        compaction: TieredCompaction[Any] | None = None,
    ):
        self.settings = settings or ModelPolicySettings()
        self.on_status, self.on_attempt, self.sleep = on_status, on_attempt, sleep
        self.compaction = compaction
        self._compacted_step: int | None = None
        self._compacted_messages = None
        self._run_context = None
        self._provider_step = None
        self._provider_attempt = 0
        self._waiting_usage = RunUsage()

    async def for_run(self, ctx):
        return type(self)(
            self.settings,
            on_status=self.on_status,
            on_attempt=self.on_attempt,
            sleep=self.sleep,
            compaction=self.compaction,
        )

    def get_wrapper_model(self, model):
        return CompleteResponseModel(model, retry=self._retry_provider_error, before_retry=self._provider_started)

    def get_ordering(self):
        # Every physical retry must pass through admission, including strict pilots.
        return CapabilityOrdering(wraps=[AttemptAdmission])

    async def _provider_started(self):
        ctx = self._run_context
        assert ctx is not None
        if self._provider_step != ctx.run_step:
            self._provider_step, self._provider_attempt = ctx.run_step, 0
        self._provider_attempt += 1
        if self.on_attempt is not None:
            if ctx.run_id is None:
                raise RuntimeError("MODEL_RUN_ID_REQUIRED")
            await self.on_attempt(ctx.deps, ctx.run_id, ctx.run_step, self._provider_attempt)
        if self.on_status is not None:
            await self.on_status("inference", 0)

    async def _retry_provider_error(self, error):
        carried = carried_attempts(error)
        if isinstance(error, httpx2.TransportError):
            kind = ModelTimeoutError if isinstance(error, httpx2.TimeoutException) else ModelConnectionError
            error = kind("provider", "MODEL_PROVIDER_TRANSPORT_ERROR")
        if not isinstance(
            error,
            (
                ModelRateLimitError,
                ModelQuotaExceededError,
                ModelOverloadedError,
                ModelUnavailableError,
                ModelServerError,
                ModelConnectionError,
                ModelRequestTimeout,
            ),
        ):
            return False
        for attempt in carried:
            if attempt.usage is not None:
                self._waiting_usage.incr(attempt.usage)
        ctx = self._run_context
        assert ctx is not None
        if ctx.usage_limits is not None:
            projected = ctx.usage + self._waiting_usage
            ctx.usage_limits.check_before_request(projected)
            ctx.usage_limits.check_tokens(projected)
            ctx.usage_limits.check_cost(projected, warn_if_cost_unavailable=False)
        await self._backoff(self._provider_attempt, error.retry_after)
        return True

    async def prepare_model_request(self, ctx: RunContext[Any], request_context: ModelRequestContext):
        self._run_context = ctx
        self._waiting_usage = RunUsage()
        if self._compacted_step == ctx.run_step and self._compacted_messages is not None:
            # The SDK restores the original request baseline before each
            # attempt. Apply the maintained compaction again in this public
            # per-attempt hook, rather than reaching into that baseline.
            request_context = replace(request_context, messages=list(self._compacted_messages))
            # The SDK runs before_model_request once per step, whereas this hook
            # runs per attempt. After an actual overflow, ask the maintained
            # Planning and context reporter to prepare the reduced request again.
            # A private queue here lets Planning's public enqueue hook build its
            # usual user-channel reminder without delaying it until the next step.
            refreshed = replace(ctx, messages=request_context.messages, pending_messages=[])
            for capability in ctx.capabilities.values():
                if isinstance(capability, Planning):
                    request_context = await capability.before_model_request(refreshed, request_context)
            for pending in refreshed.pending_messages or ():
                request_context.messages.extend(pending.messages)
            self._compacted_messages = list(request_context.messages)
            ctx.messages[:] = request_context.messages
            for capability in ctx.capabilities.values():
                if isinstance(capability, ReportContextUsage):
                    request_context = await capability.before_model_request(ctx, request_context)
        await self._provider_started()
        return request_context

    async def _backoff(self, attempt: int, retry_after: float | None = None):
        delay = min(self.settings.maximum_delay, self.settings.initial_delay * 2 ** min(attempt - 1, 20))
        if retry_after is not None and isfinite(retry_after) and retry_after >= 0:
            delay = max(delay, retry_after)
        if self.on_status is not None:
            await self.on_status("waiting_for_model", delay)
        await self.sleep(delay)

    async def _wait(self, attempt: int, retry_after: float | None = None):
        await self._backoff(attempt, retry_after)
        raise RetryModelRequest()

    async def on_model_request_error(self, ctx, *, request_context, error):
        if isinstance(error, httpx2.TransportError):
            kind = ModelTimeoutError if isinstance(error, httpx2.TimeoutException) else ModelConnectionError
            error = kind(request_context.model.model_name, "MODEL_PROVIDER_TRANSPORT_ERROR")
        if isinstance(error, ModelContextWindowExceededError):
            await self._compact_context(ctx, request_context)
        if isinstance(error, ContentFilterError):
            await self._wait(request_context.attempt)
        if isinstance(
            error,
            (
                ModelRateLimitError,
                ModelQuotaExceededError,
                ModelOverloadedError,
                ModelUnavailableError,
                ModelServerError,
                ModelConnectionError,
                ModelRequestTimeout,
            ),
        ):
            await self._wait(request_context.attempt, error.retry_after)
        raise error

    async def _compact_context(self, ctx, request_context):
        original = list(ctx.messages)
        if self.compaction is not None:
            compacted = copy.deepcopy(original)
            request_ctx = replace(ctx, model=request_context.model, messages=compacted)
            # An actual provider overflow is stronger evidence than a token
            # estimate. Force the configured tiers while keeping the owning
            # usage limits, identity and receipt capabilities in scope.
            for tier in self.compaction.tiers:
                candidate = await tier.compact(compacted, request_ctx)
                if len(ModelMessagesTypeAdapter.dump_json(candidate)) < len(
                    ModelMessagesTypeAdapter.dump_json(original)
                ):
                    compacted = candidate
                    break
        else:
            compacted = await compact_now(
                ClearToolResults(max_tokens=1, keep_pairs=1),
                copy.deepcopy(original),
                model=request_context.model,
                deps=ctx.deps,
                usage=ctx.usage,
                conversation_id=ctx.conversation_id,
            )
            compacted = await compact_now(
                SlidingWindowCompaction(max_messages=1, keep_messages=2, preserve_first_user_message=True),
                compacted,
                model=request_context.model,
                deps=ctx.deps,
                usage=ctx.usage,
                conversation_id=ctx.conversation_id,
            )
        if len(ModelMessagesTypeAdapter.dump_json(compacted)) >= len(ModelMessagesTypeAdapter.dump_json(original)):
            if self.on_status is not None:
                await self.on_status("waiting_for_context", self.settings.maximum_delay)
            # Instructions and the newest complete tool exchange are mandatory.
            # Repeating the same oversized request cannot help. Remain alive
            # and cancellable until the operator changes that input/config.
            while True:
                await self.sleep(max(1, self.settings.maximum_delay))
        ctx.messages[:] = compacted
        self._compacted_step, self._compacted_messages = ctx.run_step, compacted
        raise RetryModelRequest()

    async def after_model_request(self, ctx, *, request_context, response: ModelResponse):
        if response.finish_reason == "content_filter":
            # A refused response has not dispatched tools. Keep the same model
            # and prompt; do not rewrite the request to defeat its filter.
            await self._wait(request_context.attempt)
        return response


def context_compaction(
    settings: ModelPolicySettings,
    *,
    context_window: int | None = None,
    policy: CompactionPolicy | None = None,
    summary_services: SummaryServices | None = None,
):
    """Use recorded policy; omitted policy retains the legacy no-summary path."""
    tiers: list[CompactionStrategy[Any]]
    if policy is None:
        tiers = [
            ClearToolResults(max_tokens=1, keep_pairs=3),
            SlidingWindowCompaction(max_messages=1, keep_messages=12, preserve_first_user_message=True),
        ]
    else:
        tiers = [SupersededObservations(max_tokens=1, keep_pairs=policy.keep_tool_pairs)]
        if policy.summarize:
            if summary_services is None:
                raise ValueError("SUMMARY_OWNER_SERVICES_REQUIRED")
            tiers.append(
                OwnerBoundSummary(
                    max_messages=1,
                    keep_messages=policy.keep_recent_messages,
                    preserve_first_user_message=True,
                    incremental=True,
                    receipts=True,
                    # Full nested results stay private, but model-visible
                    # outcomes and unresolved-effect references reach summaries.
                    tool_return_max_chars=None,
                    instructions=SUMMARY_INSTRUCTIONS,
                    summary_prompt=SUMMARY_PROMPT,
                    services=summary_services,
                    policy=policy,
                )
            )
        tiers.append(
            EffectPreservingWindow(
                max_messages=1,
                keep_messages=policy.keep_recent_messages,
                preserve_first_user_message=True,
                receipts=True,
            )
        )
    if settings.context_target_tokens is not None:
        return TieredCompaction(tiers=tiers, target_tokens=settings.context_target_tokens)
    return TieredCompaction(
        tiers=tiers,
        target_fraction=policy.target_fraction if policy else 0.75,
        context_window=context_window,
        fallback_context_window=65536,
    )
