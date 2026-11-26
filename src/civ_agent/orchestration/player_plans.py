"""Authorized, role-scoped access to the maintained native Planning store."""

import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart, post_compaction_window
from pydantic_ai.tools import ToolDefinition
from pydantic_ai_harness import Planning
from pydantic_ai_harness.planning import PlanItem, SqlitePlanStore, TaskStatus
from pydantic_ai_harness.planning._toolset import PLAN_ORIGIN, framed_plan

from civ_agent.orchestration.contracts import GatewayJob, PlayerRole, RoleRequest, digest
from civ_agent.orchestration.gateway_registry import job_key
from civ_agent.orchestration.store import JournalConflict
from civ_agent.schemas import BoundaryModel
from civ_agent.tls import private_directory, regular_file

PLAN_TOOLS = (
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
)


class ReadPlan(BoundaryModel):
    kind: Literal["get_items"] = "get_items"


class ReadRolePlan(BoundaryModel):
    kind: Literal["read_role_plan"] = "read_role_plan"
    role: PlayerRole


class WritePlan(BoundaryModel):
    kind: Literal["set_items"] = "set_items"
    items: list[PlanItem]


class ReadPlanItem(BoundaryModel):
    kind: Literal["get_item"] = "get_item"
    item_id: str


class AddPlanItem(BoundaryModel):
    kind: Literal["add_item"] = "add_item"
    item: PlanItem


class RemovePlanItem(BoundaryModel):
    kind: Literal["remove_item"] = "remove_item"
    item_id: str


class UpdatePlanItem(BoundaryModel):
    kind: Literal["update_item"] = "update_item"
    item_id: str
    content: str | None = None
    status: TaskStatus | None = Field(default=None, strict=False)
    active_form: str | None = None
    parent_id: str | None = None
    depends_on: list[str] | None = None


class PlayerPlanRequest(BoundaryModel):
    version: Literal[1] = 1
    identity: GatewayJob
    operation: Annotated[
        ReadPlan | ReadRolePlan | WritePlan | ReadPlanItem | AddPlanItem | RemovePlanItem | UpdatePlanItem,
        Field(discriminator="kind"),
    ]


class PlayerPlanReply(BoundaryModel):
    items: list[PlanItem] | None = None
    item: PlanItem | None = None
    removed: bool | None = None


def plan_session(identity: GatewayJob) -> str:
    scope = identity.scope
    return digest([scope.game, scope.player, scope.epoch, identity.purpose])


class PlayerPlanService:
    """Authorize live role access; SQLite alone owns the latest committed plan.

    The controller admits one writer for each role. Plans are private intent,
    so an interrupted update is read afresh instead of replayed as a world effect.
    """

    def __init__(self, registry, database: Path):
        self.registry, self.database = registry, database
        private_directory(database.parent)
        try:
            descriptor = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        regular_file(database, private=True)
        self.stores: dict[str, SqlitePlanStore] = {}

    def store(self, identity):
        session = plan_session(identity)
        if session not in self.stores:
            self.stores[session] = SqlitePlanStore(str(self.database), session=session)
        return self.stores[session]

    def load(self, identity, *, mutation):
        with self.registry.gateway.memory.transaction() as db:
            record = self.registry._load(db, identity)
            if record.retired:
                raise JournalConflict("GATEWAY_JOB_RETIRED")
            if record.pending is not None:
                raise JournalConflict("GATEWAY_RECONCILIATION_REQUIRED")
            self.registry._resume(db, record)
        if mutation:
            gateway = self.registry.gateway
            if gateway.checkpoint_requested:
                raise JournalConflict("PLANNING_CHECKPOINT_IN_PROGRESS")
            roles = gateway.role_coordinator
            # The communication loop serializes its own jobs and retires them through the registry.
            if roles is not None and identity.purpose != "communication":
                owner = roles.jobs.get(identity.purpose)
                if owner is None or owner.id != identity.job:
                    raise JournalConflict("PLANNING_ROLE_WRITER_CHANGED")
        return record

    async def invoke(self, request: PlayerPlanRequest) -> PlayerPlanReply:
        operation = request.operation
        mutation = isinstance(operation, WritePlan | AddPlanItem | RemovePlanItem | UpdatePlanItem)
        # The native coordinator dispatch gate also drains accepted I/O on stop.
        async with self.registry.dispatch:
            record = await self.registry.gateway.io.call(self.load, request.identity, mutation=mutation)
            if mutation:
                key = job_key(request.identity)
                job = self.registry.jobs.get(key)
                if job is None:
                    job = await self.registry.resolve(record)
                    self.registry.jobs[key] = job
                self.registry.gateway.check(job)
            identity = request.identity
            if isinstance(operation, ReadRolePlan):
                # The caller chooses a role, never another player, epoch or DB.
                identity = identity.model_copy(update={"purpose": operation.role})
            store = self.store(identity)
            if isinstance(operation, ReadPlan | ReadRolePlan):
                return PlayerPlanReply(items=await store.get_items())
            if isinstance(operation, ReadPlanItem):
                return PlayerPlanReply(item=await store.get_item(operation.item_id))
            if isinstance(operation, WritePlan):
                await store.set_items(operation.items)
                return PlayerPlanReply()
            if isinstance(operation, AddPlanItem):
                return PlayerPlanReply(item=await store.add_item(operation.item))
            if isinstance(operation, RemovePlanItem):
                return PlayerPlanReply(removed=await store.remove_item(operation.item_id))
            return PlayerPlanReply(
                item=await store.update_item(
                    operation.item_id, **operation.model_dump(exclude={"kind", "item_id"}, exclude_none=True)
                )
            )


class GatewayPlanStore:
    """Native PlanStore protocol; models cannot choose its writable session."""

    def __init__(self, client: Callable, identity: GatewayJob):
        self.client, self.identity = client, identity

    async def invoke(self, operation):
        request = PlayerPlanRequest(identity=self.identity, operation=operation)
        return PlayerPlanReply.model_validate(await self.client().post("plans", request.model_dump(mode="json")))

    async def get_items(self):
        return (await self.invoke(ReadPlan())).items or []

    async def set_items(self, items):
        await self.invoke(WritePlan(items=items))

    async def get_item(self, item_id):
        return (await self.invoke(ReadPlanItem(item_id=item_id))).item

    async def update_item(self, item_id, **changes):
        return (await self.invoke(UpdatePlanItem(item_id=item_id, **changes))).item

    async def add_item(self, item):
        result = (await self.invoke(AddPlanItem(item=item))).item
        assert result is not None
        return result

    async def remove_item(self, item_id):
        return bool((await self.invoke(RemovePlanItem(item_id=item_id))).removed)


class RolePlanning(Planning[RoleRequest]):
    """Native planning plus an explicitly read-only view of sibling roles."""

    async def before_model_request(self, ctx, request_context):
        if self.inject and request_context.messages and isinstance(request_context.messages[-1], ModelRequest):
            for message in reversed(post_compaction_window(request_context.messages)):
                if not isinstance(message, ModelRequest):
                    continue
                for part in reversed(message.parts):
                    if isinstance(part, UserPromptPart) and not isinstance(part.content, str):
                        if any(
                            (getattr(item, "metadata", None) or {}).get("origin") == PLAN_ORIGIN
                            for item in part.content
                        ):
                            return await super().before_model_request(ctx, request_context)
                    if not isinstance(part, ToolReturnPart):
                        continue
                    metadata = part.metadata or {}
                    if metadata.get("origin") == PLAN_ORIGIN:
                        return await super().before_model_request(ctx, request_context)
                    if part.tool_name != "run_code":
                        continue
                    for child in reversed(list(metadata.get("tool_returns", {}).values())):
                        child_metadata = (
                            (child.get("metadata") or {}) if isinstance(child, dict) else child.metadata or {}
                        )
                        plan = child_metadata.get("plan")
                        if child_metadata.get("origin") != PLAN_ORIGIN or not isinstance(plan, str):
                            continue
                        if _visible_plan(part.content, plan):
                            if plan == framed_plan(await self._read_plan(ctx)):
                                return request_context
                            return await super().before_model_request(ctx, request_context)
        return await super().before_model_request(ctx, request_context)

    def get_toolset(self):
        toolset = super().get_toolset()
        assert isinstance(toolset, FunctionToolset)
        if "read_role_plan" not in toolset.tools:

            async def read_role_plan(ctx: RunContext[RoleRequest], role: PlayerRole) -> list[PlanItem]:
                """Read another role's current private plan for coordination; this cannot edit it."""
                assert self.store_resolver is not None
                store = self.store_resolver(ctx)
                assert isinstance(store, GatewayPlanStore)
                reply = await store.invoke(ReadRolePlan(role=role))
                return reply.items or []

            toolset.add_function(read_role_plan, takes_ctx=True)
        return toolset


def _visible_plan(value, plan):
    if isinstance(value, str):
        return plan in value
    if isinstance(value, dict):
        return any(_visible_plan(item, plan) for item in value.values())
    if isinstance(value, list):
        return any(_visible_plan(item, plan) for item in value)
    return False


def player_planning(client: Callable) -> Planning[RoleRequest]:
    def resolve(ctx: RunContext[RoleRequest]):
        return GatewayPlanStore(client, GatewayJob(scope=ctx.deps.scope, job=ctx.deps.job, purpose=ctx.deps.purpose))

    return RolePlanning(
        store_resolver=resolve,
        enable_subtasks=True,
        guidance=(
            "Use your private role plan for unfinished work and handoff between turns. Use read_role_plan for another "
            "role's plan as read-only coordination context; only that role may edit it. Task statuses are authored "
            "assessments. They do not authorize or prohibit native actions, change ownership, fulfill player obligations "
            "or complete the role. Use native tools and receipts for game state; finish_role remains completion authority."
        ),
    )


async def sequential_plan_tools(ctx: RunContext[RoleRequest], tools: list[ToolDefinition]) -> list[ToolDefinition]:
    return [replace(tool, sequential=True) if tool.name in PLAN_TOOLS else tool for tool in tools]
