"""Shared maintained capabilities and metadata for every local model run."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from opentelemetry.trace import get_current_span
from pydantic_ai.capabilities import AbstractCapability, Caching, PrepareTools, on_event
from pydantic_ai_harness import ReportContextUsage
from pydantic_ai_harness.compaction import ContextUsageEvent
from pydantic_ai_harness.step_persistence import StepStore
from pydantic_ai_harness.warn_on_cache_busts import WarnOnCacheBusts

from civ_agent.execution_policy import legacy_execution_policy
from civ_agent.orchestration.contracts import canonical
from civ_agent.orchestration.providers import model_settings
from civ_agent.orchestration.transcripts import PlayerPersistence, scope_of
from civ_agent.runtime.cache_policy import ProviderCaching
from civ_agent.runtime.compaction import SummaryServices
from civ_agent.runtime.model_policy import (
    AttemptCallback,
    LocalModelPolicy,
    ModelPolicySettings,
    SanitizeProviderErrors,
    StatusCallback,
    context_compaction,
)

type ContextUsageCallback = Callable[[Any, ContextUsageEvent], Awaitable[None]]

COMMON_TOOLS = (
    "read_memory",
    "search_memory",
    "write_memory",
    "delete_memory",
    "read_plan",
    "write_plan",
    "add_task",
    "update_task_status",
    "update_task_statuses",
    "remove_task",
    "add_subtask",
    "set_dependency",
    "get_available_tasks",
    "read_role_plan",
    "player_turn",
    "inspect_bundle",
    "command_help",
    "construction_details",
    "unit_details",
    "movement_preview",
)


async def stable_tool_order(_ctx, definitions):
    ranks = {name: index for index, name in enumerate(COMMON_TOOLS)}
    return sorted(definitions, key=lambda definition: (ranks.get(definition.name, len(ranks)), definition.name))


def run_metadata(deps: Any, *, agent_name: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    scope = scope_of(deps)
    purpose = getattr(
        deps, "purpose", getattr(deps, "phase", "review" if agent_name == "strategic_review_v1" else agent_name)
    )
    values = {"scope": canonical(scope.model_dump(mode="json")), "agent_name": agent_name, "purpose": purpose}
    for name in ("job", "source_turn", "target_turn", "review_cycle_id"):
        value = getattr(deps, name, None)
        if value is not None:
            values[name] = str(value)
    if hasattr(deps, "model_step_offset"):
        values["modelStepOffset"] = str(deps.model_step_offset)
    execution_policy = getattr(deps, "execution_policy", None)
    if execution_policy is not None and execution_policy.version:
        values["execution_policy_version"] = str(execution_policy.version)
        values["execution_policy_sha256"] = execution_policy.fingerprint
    gameplay_policy = getattr(deps, "gameplay_policy", None)
    if gameplay_policy is not None and gameplay_policy.version:
        values["gameplay_policy_sha256"] = gameplay_policy.fingerprint
        values["gameplay_architecture"] = gameplay_policy.architecture
    selection = getattr(deps, "selection", None)
    if selection is not None:
        values.update(model=selection.model, provider=selection.provider, reasoningEffort=selection.reasoning_effort)
    # Extra code-owned labels identify native admission, but cannot replace the
    # scope, role or selection that actually owns this model run.
    return {**(extra or {}), **values}


@dataclass
class ContextUsageTelemetry(AbstractCapability[Any]):
    callback: ContextUsageCallback | None = None
    id: str | None = "local-context-usage"

    @on_event(ContextUsageEvent)
    async def usage(self, ctx, event: ContextUsageEvent):
        span = get_current_span()
        span.set_attributes(
            {
                "civ.context.used_tokens": event.used_tokens,
                "civ.context.window_tokens": event.window_tokens,
                "civ.context.fraction": event.fraction,
                "civ.context.resolved": event.resolved,
            }
        )
        if self.callback is not None:
            await self.callback(ctx.deps, event)


def agent_capabilities(
    *,
    store: StepStore,
    metadata: dict[str, str],
    settings: ModelPolicySettings,
    cache_warnings: WarnOnCacheBusts,
    context_window: int | None,
    on_status: StatusCallback | None = None,
    on_attempt: AttemptCallback | None = None,
    remaining_model_attempts: Callable[[Any], int] | None = None,
    on_context_usage: ContextUsageCallback | None = None,
    role_capabilities: Sequence[AbstractCapability[Any]] = (),
    deps: Any = None,
    cache_account_scope: str | None = None,
    cache_endpoint: str | None = None,
    admission_factory: Callable[[str], AbstractCapability[Any]] | None = None,
) -> list[AbstractCapability[Any]]:
    """Compaction precedes Planning; the usage event describes the resulting request."""
    policy = getattr(deps, "execution_policy", legacy_execution_policy())
    account_scope = cache_account_scope

    def caching():
        if getattr(deps, "selection", None) is None:
            return Caching()
        return ProviderCaching(policy, deps.selection, account_scope=account_scope, endpoint=cache_endpoint)

    def summary_capabilities(owner):
        selected: list[AbstractCapability[Any]] = [caching()]
        if admission_factory is not None:
            selected.append(admission_factory("context_summary"))
        if on_context_usage is not None:

            async def summary_usage(_deps, event):
                await on_context_usage(owner.deps, event)

            selected.append(ContextUsageTelemetry(callback=summary_usage))
        return selected

    compaction = context_compaction(
        settings,
        context_window=context_window,
        policy=policy.compaction if policy.version else None,
        summary_services=SummaryServices(
            store=store,
            metadata=metadata,
            on_attempt=on_attempt,
            remaining_attempts=remaining_model_attempts,
            on_status=on_status,
            model_settings=lambda owner: model_settings(owner.selection),
            capabilities=summary_capabilities,
        ),
    )
    capabilities: list[AbstractCapability[Any]] = [
        SanitizeProviderErrors(),
        PlayerPersistence(store=store, metadata=metadata, capture_frontier=True),
        cache_warnings,
        LocalModelPolicy(
            settings, on_status=on_status, on_attempt=on_attempt, compaction=compaction if policy.version else None
        ),
        compaction,
        *role_capabilities,
        PrepareTools(stable_tool_order),
        caching(),
        ReportContextUsage(context_window=context_window, fallback_context_window=65536),
    ]
    # Listening to run events opts the SDK into streaming model responses. Keep
    # that opt-in explicit; the reporter itself works with request-only models.
    if on_context_usage is not None:
        capabilities.append(ContextUsageTelemetry(callback=on_context_usage))
    if admission_factory is not None:
        capabilities.insert(0, admission_factory(metadata["agent_name"] + "/" + metadata["purpose"]))
    return capabilities
