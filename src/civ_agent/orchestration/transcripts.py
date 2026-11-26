"""Scoped adapters around upstream agent snapshots and tool-effect records."""

import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic_ai import RunContext
from pydantic_ai.capabilities import durable_operation
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ToolReturn,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai_harness import StepPersistence
from pydantic_ai_harness.step_persistence import SqliteStepStore, StepEvent

from civ_agent.orchestration.contracts import (
    GatewayDependencies,
    OpaqueId,
    PeacePhase,
    Reference,
    RoleWait,
    Scope,
    Transcript,
    canonical,
    digest,
)
from civ_agent.orchestration.gateway_execution import native_call_id, operation_identity
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.schemas import BoundaryModel
from civ_agent.tls import regular_file


def scope_of(deps):
    return deps.request.scope if isinstance(deps, PeacePhase) else deps.scope


class PlayerPersistence(StepPersistence[Any]):
    async def for_run(self, ctx):
        scoped = cast(PlayerPersistence, await super().for_run(ctx))
        metadata = {**scoped.metadata, "scope": canonical(scope_of(ctx.deps).model_dump(mode="json"))}
        if isinstance(ctx.deps, GatewayDependencies):
            metadata["modelStepOffset"] = str(ctx.deps.model_step_offset)
        return replace(scoped, metadata=metadata)

    @durable_operation("native_receipt_link")
    async def link(self, run_id: str, call_id: str, identifier: str):
        effect = await self.store.get_tool_effect(run_id=run_id, tool_call_id=call_id)
        if effect is None or effect.idempotency_key not in {None, identifier}:
            raise JournalConflict("TRANSCRIPT_NATIVE_LINK_CONFLICT")
        await self.store.record_tool_effect(replace(effect, idempotency_key=identifier))

    async def before_tool_execute(self, ctx: RunContext, *, call, tool_def, args):
        arguments = await super().before_tool_execute(ctx, call=call, tool_def=tool_def, args=args)
        if (
            isinstance(ctx.deps, GatewayDependencies)
            and tool_def.kind == "function"
            and (tool_def.metadata or {}).get("code_mode_result_helper") is not True
        ):
            identifier = operation_identity(
                ctx.deps.job, ctx.deps.scope.epoch, ctx.deps.model_step_offset + ctx.run_step, call.tool_call_id
            )
            if ctx.run_id is None:
                raise JournalConflict("TRANSCRIPT_RUN_REQUIRED")
            await self.link(ctx.run_id, call.tool_call_id, identifier)
        await self.nested_evidence(ctx, call, phase="started")
        return arguments

    @durable_operation("native_nested_evidence")
    async def append_nested_evidence(
        self,
        *,
        run_id: str,
        conversation_id: str | None,
        step: int,
        parent_run_id: str | None,
        call_id: str,
        tool_name: str,
        phase: Literal["started", "completed"],
        metadata: dict[str, str],
    ):
        await self.store.append_event(
            StepEvent(
                run_id=run_id,
                conversation_id=conversation_id,
                parent_run_id=parent_run_id,
                agent_name=self.agent_name,
                kind="tool_call_started" if phase == "started" else "tool_call_completed",
                step_index=step,
                tool_call_id=call_id,
                tool_name=tool_name,
                metadata=metadata,
                idempotency_key=f"native-nested-v1:{step}:{phase}:{call_id}",
            )
        )

    async def nested_evidence(self, ctx, call, *, phase, result=None):
        from civ_agent.runtime.code_mode import _parent
        from civ_agent.runtime.code_mode_results import RESULT_HELPERS

        parent = _parent.get()
        if parent is None or call.tool_name == "run_code":
            return
        if ctx.run_id is None:
            raise JournalConflict("TRANSCRIPT_NESTED_OWNER_REQUIRED")
        metadata = {
            **self.metadata,
            "native_nested_version": "1",
            "parent_tool_call_id": parent,
            "scope": canonical(scope_of(ctx.deps).model_dump(mode="json")),
        }
        if isinstance(ctx.deps, GatewayDependencies) and call.tool_name not in RESULT_HELPERS:
            metadata.update(
                native_call_id=native_call_id(call.tool_call_id),
                native_operation_id=operation_identity(
                    ctx.deps.job, ctx.deps.scope.epoch, ctx.deps.model_step_offset + ctx.run_step, call.tool_call_id
                ),
            )
        if phase == "started":
            metadata["nested_call"] = ModelMessagesTypeAdapter.dump_json([ModelResponse(parts=[call])]).decode()
        else:
            value = result.return_value if isinstance(result, ToolReturn) else result
            returned = ToolReturnPart(
                call.tool_name,
                value,
                call.tool_call_id,
                metadata=result.metadata if isinstance(result, ToolReturn) else None,
            )
            parts: list[ModelRequestPart] = [returned]
            if isinstance(result, ToolReturn) and result.content is not None:
                parts.append(UserPromptPart(result.content))
            metadata["nested_result"] = ModelMessagesTypeAdapter.dump_json([ModelRequest(parts=parts)]).decode()
        await self.append_nested_evidence(
            run_id=ctx.run_id,
            conversation_id=ctx.conversation_id,
            step=ctx.run_step,
            parent_run_id=self.parent_run_id,
            call_id=call.tool_call_id,
            tool_name=call.tool_name,
            phase=phase,
            metadata=metadata,
        )

    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        # Record every full child result before the enclosing program can
        # raise. Harness's successful parent-return metadata is not produced
        # when a later Python statement fails or the snippet is cancelled.
        await self.nested_evidence(ctx, call, phase="completed", result=result)
        return await super().after_tool_execute(ctx, call=call, tool_def=tool_def, args=args, result=result)


async def record_nested_result(ctx, call, result):
    """Retain an externally settled child result under its original identity."""
    for capability in ctx.capabilities.values():
        if isinstance(capability, PlayerPersistence):
            await capability.nested_evidence(ctx, call, phase="completed", result=result)
            return
    raise JournalConflict("TRANSCRIPT_NESTED_PERSISTENCE_REQUIRED")


persistence = PlayerPersistence(capture_frontier=True)


def backend(store: WorkflowStore, *, create=False):
    path = store.root / "agent-steps.sqlite3"
    if create:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
    regular_file(path, private=True)
    return SqliteStepStore(database=path)


def configure(store: WorkflowStore):
    persistence.store = backend(store, create=True)


def pointer(run_id: str, messages: list[ModelMessage]) -> Transcript:
    return Transcript(run_id=run_id, sha256=digest(ModelMessagesTypeAdapter.dump_python(messages, mode="json")))


class SnapshotTranscript(BoundaryModel):
    scope: Scope
    run_id: OpaqueId
    agent: Literal["player_role_v1", "strategic_review_v1", "peace_military_v1", "peace_main_v1"]
    conversation_id: OpaqueId
    allow_interrupted: bool = False


class NativeTranscripts:
    def __init__(self, store: WorkflowStore):
        self.store = store

    async def read(self, request: SnapshotTranscript) -> Transcript:
        steps = await bound_run(self.store, request.scope, request.run_id, agent=request.agent)
        run = await steps.get_run(run_id=request.run_id)
        if run is None or run.conversation_id != request.conversation_id or not run.registration_id:
            raise JournalConflict("TRANSCRIPT_RUN_CONFLICT")
        snapshot = await steps.latest_snapshot(run_id=request.run_id, include_interrupted=True)
        if (
            snapshot is None
            or snapshot.run_id != request.run_id
            or snapshot.agent_name != request.agent
            or snapshot.conversation_id != request.conversation_id
            or snapshot.state != "complete"
            and not request.allow_interrupted
        ):
            raise JournalConflict("TRANSCRIPT_SNAPSHOT_CONFLICT")
        with self.store.transaction(request.scope):
            pass
        return pointer(request.run_id, snapshot.messages)


async def bound_run(store: WorkflowStore, scope: Scope, run_id: str, *, agent: str):
    with store.transaction(scope):
        pass
    steps = backend(store)
    run = await steps.get_run(run_id=run_id)
    if run is None or run.agent_name != agent or run.metadata.get("scope") != canonical(scope.model_dump(mode="json")):
        raise JournalConflict("TRANSCRIPT_SCOPE_CONFLICT")
    return steps


async def wait_effect(store: WorkflowStore, request: RoleWait, identifier: str, *, completed=False):
    steps = await bound_run(store, request.scope, request.run_id, agent="player_role_v1")
    effect = await steps.get_tool_effect(run_id=request.run_id, tool_call_id=request.call_id)
    if (
        effect is None
        or effect.tool_name != "economy_wait"
        or effect.idempotency_key != identifier
        or effect.status not in {"started", "completed"}
    ):
        raise JournalConflict("TRANSCRIPT_WAIT_EFFECT_CONFLICT")
    if completed and effect.status != "completed":
        await steps.record_tool_effect(replace(effect, status="completed", ended_at=datetime.now(UTC)))


async def load(store: WorkflowStore, scope: Scope, transcript: Transcript, *, agent: str):
    steps = await bound_run(store, scope, transcript.run_id, agent=agent)
    snapshot = await steps.latest_snapshot(run_id=transcript.run_id, include_interrupted=True)
    if snapshot is None or pointer(transcript.run_id, snapshot.messages) != transcript:
        raise JournalConflict("TRANSCRIPT_SNAPSHOT_CONFLICT")
    with store.transaction(scope):
        pass
    return snapshot.messages


async def seal(store: WorkflowStore, scope: Scope, transcript: Transcript, *, agent: str) -> Reference:
    await load(store, scope, transcript, agent=agent)
    return store.put(scope, transcript.model_dump(mode="json"))


async def read(store: WorkflowStore, scope: Scope, reference: Reference, *, agent: str):
    return await load(store, scope, Transcript.model_validate(store.get(scope, reference)), agent=agent)
