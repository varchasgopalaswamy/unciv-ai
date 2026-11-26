"""Freeze optional effort advice before admitting a native role to model workers."""

from typing import Literal

import aiohttp
from pydantic import Field
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openai_codex import OpenAICodexProvider

from civ_agent.decisions_tasks import TASK_VERSION, Evidence
from civ_agent.effort_decisions import EFFORT_LEVELS, VERSION, Effort, brief, choose
from civ_agent.memory_records import public_record
from civ_agent.orchestration.contracts import Digest, GatewayJob, ModelSelection, NativeAuxiliaryTask, Reference, digest
from civ_agent.orchestration.store import JournalConflict
from civ_agent.orchestration.task_identity import TurnRoleTask, task_identity_sha256
from civ_agent.proposal_gateway import public_result
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel


class EffortInput(BoundaryModel):
    version: Literal["reasoning-effort-v2"] = VERSION
    identity: GatewayJob
    selection: ModelSelection
    policy: Digest
    revision: str | None
    levels: list[Effort]
    evidence: Reference | None = None
    fallback: str | None = None
    task: NativeAuxiliaryTask | None = Field(default=None, exclude_if=lambda value: value is None)


class EffortOutcome(BoundaryModel):
    selection: ModelSelection
    reason: str
    diagnostics: JsonObject


def supported_levels(selection: ModelSelection) -> list[Effort]:
    """Use the pinned SDK's effort dialect; unknown model knobs stay unknown."""
    if selection.provider != "openai":
        return []
    provider = OpenAICodexProvider if selection.authentication == "subscription" else OpenAIProvider
    profile = provider.model_profile(selection.model)
    if profile is None or not profile.get("supports_thinking", False):
        return []
    accepted = profile.get("openai_reasoning_efforts")
    if accepted is not None and not isinstance(accepted, frozenset):
        return []
    # An unset effort set uses the SDK's one-to-one unified thinking mapping.
    # It does not disable reasoning: Terra and Luna use this profile dialect.
    # Unified thinking may map max to xhigh on a narrower endpoint. The
    # classifier must compare distinct supported settings, not duplicate aliases.
    return [
        level
        for level in EFFORT_LEVELS
        if (accepted is None or level in accepted)
        and (level != "max" or profile.get("openai_responses_supports_max_reasoning_effort", False))
    ]


def check(root, binding, job):
    root.check_owner(binding.scope)
    if (job.id, job.epoch, job.purpose) != (binding.job, binding.scope.epoch, binding.purpose):
        raise JournalConflict("EFFORT_JOB_CONFLICT")
    root.gateway.check(job)
    with root.memory.transaction() as db:
        if root.memory._meta(db, "epoch") != binding.scope.epoch:
            raise JournalConflict("EFFORT_EPOCH_CONFLICT")


async def select_effort(
    root, binding: GatewayJob, job, prepared, selection: ModelSelection, *, task: TurnRoleTask | None = None
) -> ModelSelection:
    """Reuse broker work and its original accounting identity after interruption."""
    runtime = root.gateway.advisories
    if runtime is None or not runtime.settings.decisions_effort_enabled or job.purpose not in {"gameplay", "economy"}:
        return selection
    check(root, binding, job)
    logical = None
    if task is not None:
        if (task.owner.game, task.owner.player, task.owner.civilization, task.purpose, task.turn) != (
            binding.scope.game,
            binding.scope.player,
            root.civilization,
            binding.purpose,
            job.memory.get("gameTurn"),
        ):
            raise JournalConflict("EFFORT_TASK_CONFLICT")
        logical = NativeAuxiliaryTask(kind="effort_selection", key=task_identity_sha256(task))
    policy = runtime.settings
    key = "nativeEffort:" + binding.scope.identity("job", binding.job)
    saved = await root.get(key)
    if saved is not None:
        frozen = EffortInput.model_validate(saved)
        if (frozen.identity, frozen.selection, frozen.policy, frozen.task) != (
            binding,
            selection,
            digest(policy.model_dump()),
            logical,
        ):
            raise JournalConflict("EFFORT_BINDING_CONFLICT")
    else:
        revision = (job.observation or {}).get("revision")
        levels = [level for level in policy.decisions_effort_levels if level in supported_levels(selection)]
        frozen = EffortInput(
            identity=binding,
            selection=selection,
            policy=digest(policy.model_dump()),
            revision=revision,
            levels=levels,
            task=logical,
        )
        if selection.provider != "openai" or selection.reasoning_effort not in EFFORT_LEVELS:
            frozen.fallback = "unsupported_settings"
        elif prepared is None or revision is None:
            frozen.fallback = "input_unavailable"
        elif len(levels) < 2:
            frozen.fallback = "insufficient_supported_levels"
        else:
            try:
                plans = [
                    public_result(public_record(identifier, job.memory["records"][identifier]))
                    for identifier in job.memory.get("contextIds", [])
                    if job.memory["records"][identifier]["category"] in {"strategy", "city_plan", "handoff"}
                ]
                evidence = Evidence.model_validate(
                    {
                        "version": TASK_VERSION,
                        "player": root.civilization,
                        "effortSelection": {
                            "version": VERSION,
                            "role": binding.purpose,
                            "model": selection.model,
                            "levels": levels,
                            "facts": brief(prepared, plans, binding.purpose),
                        },
                    }
                ).model_dump(exclude_none=True)
                frozen.evidence = root.store.put(binding.scope, evidence)
            except JournalConflict:
                raise
            except (ValueError, KeyError, TypeError):
                frozen.fallback = "input_unavailable"
        # Freeze before any broker call. A cancelled preparation resumes this
        # exact evidence even if refreshed startup reads have since changed.
        await root.put(key, frozen.model_dump(mode="json"))
    body = frozen.model_dump(mode="json")
    _, recorded = root.store.reserve(binding.scope, "effort-selection", body)
    if recorded is not None:
        outcome = EffortOutcome.model_validate(root.store.get(binding.scope, recorded))
    else:
        decision = {"reason": frozen.fallback or "selection_unavailable"}
        if frozen.evidence is not None:
            try:
                result = await runtime.evaluate(root.store.get(binding.scope, frozen.evidence), task=frozen.task)
                decision = {
                    **choose(
                        result,
                        "main_effort" if binding.purpose == "gameplay" else "economy_effort",
                        frozen.levels,
                        policy,
                    ),
                    "requestId": result.get("requestId"),
                }
            except JournalConflict:
                raise
            except (TimeoutError, OSError, ValueError, KeyError, TypeError, aiohttp.ClientError):
                pass
        check(root, binding, job)
        if job.role_refresh_required or (job.observation or {}).get("revision") != frozen.revision:
            decision["reason"] = "state_changed"
        chosen = selection
        if decision["reason"] == "selected":
            chosen = ModelSelection.model_validate(
                {**selection.model_dump(), "reasoning_effort": decision["candidate"]}
            )
        outcome = EffortOutcome(selection=chosen, reason=decision["reason"], diagnostics=decision)
        root.store.complete(binding.scope, "effort-selection", body, outcome.model_dump(mode="json"))
    check(root, binding, job)
    if job.role_refresh_required or (job.observation or {}).get("revision") != frozen.revision:
        return selection
    return outcome.selection
