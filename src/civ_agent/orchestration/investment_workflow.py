"""Opt-in choice-to-task workflow over existing authored strategy and Planning."""

from dataclasses import replace

from pydantic import ValidationError
from pydantic_ai import FunctionToolset, RunContext, Tool
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai_harness.planning import PlanItem, TaskStatus

from civ_agent.memory_records import Source, Strategy, Text
from civ_agent.orchestration.authored_plan_service import (
    AuthoredDocumentReply,
    ChooseInvestmentDocument,
    ReadInvestmentDocument,
)
from civ_agent.orchestration.authored_plan_tools import AuthoredPlanTools
from civ_agent.orchestration.contracts import GatewayDependencies
from civ_agent.orchestration.investment_records import InvestmentCheckpoint, InvestmentChoice
from civ_agent.orchestration.player_plans import GatewayPlanStore, ReadRolePlan
from civ_agent.protocol import JsonObject
from civ_agent.schemas import argument_feedback
from civ_agent.tool_recovery import RecoverableToolError, recovery_response

WORKFLOW_TOOLS = frozenset({"review_investments", "choose_investment", "track_investment"})
WORKFLOW_GUIDANCE = """Use the investment workflow as the normal shared plan for consequential choices.
Main: review_investments, then choose_investment to create or revise a stable choice, explicitly wait, or abandon it. Choose an owner, next step, observable outcome and reassessment checkpoint. Compare reasonable alternatives including waiting. For consequential production/purchase comparisons, inspect the relevant native details, then copy source, version and facts from review_investments().progress.quoteSources into each observed quote. Detail evidence labels such as e12 and construction queue revisions are different identifiers. If no relevant quote is available, explicitly record unavailable/unknown facts. Broad strategic notes may use ordinary notes; choose_investment maintains the accepted strategy instead of a generic save_strategy.
Economy: review_investments to see Main's choices and your existing role tasks. After acting or choosing to wait, track_investment with the actual next step, authored disposition and receiptRefs copied from its historical progress. It maintains one task per choice in your existing Planning store. Read sibling role plans for coordination; only the owner writes its role tasks. Main may use the same workflow for its own work. Do not recreate these tasks in a second checklist.
Use the returned strategy.md and stable decision ID in native command intent when that choice explains an action. Compare receipt-grounded progress and observable results with the checkpoint before claiming success; task completion, quote claims, and prose are authored assessments, never native effects or fresh inspection credit. A started worker job is not a completed improvement, and a produced settler is not a founded city. If facts or acknowledgements are missing, preserve the uncertainty, refresh relevant native facts and reconsider; never replay a committed action to improve its annotations.
Waiting, accepted risk, changing priorities, wonders and abandoning an investment remain legal. No fixed build order, compulsory movement, aggression or escort is imposed. A plan/storage refusal leaves native decisions available: finish required native work and report the planning issue without inventing completion. Keep a sound choice unchanged; use track_investment for progress rather than resetting its baseline by rewriting it every turn."""


def workflow_instructions(text: str, *, enabled: bool) -> str:
    if not enabled:
        return text
    from civ_agent.orchestration.role_instructions import NATIVE_STRATEGY

    if NATIVE_STRATEGY in text:
        return text.replace(NATIVE_STRATEGY, WORKFLOW_GUIDANCE, 1)
    return text + "\n\n" + WORKFLOW_GUIDANCE


def workflow_enabled(ctx) -> bool:
    return ctx.deps.purpose in {"gameplay", "economy", "military", "communication"} and bool(
        getattr(getattr(ctx.deps, "execution_policy", None), "follow_through_workflow", False)
    )


def task_id(decision_id: str) -> str:
    return "investment-" + decision_id


class InvestmentWorkflow(AbstractCapability[GatewayDependencies]):
    id = "investment-workflow-v1"

    def __init__(self, client):
        self.client = client
        self.documents = AuthoredPlanTools(client)
        self._tools = None

    def plans(self, ctx):
        return GatewayPlanStore(
            (lambda: self.client) if hasattr(self.client, "post") else self.client, self.documents._identity(ctx)
        )

    async def prepare_tools(self, ctx, tool_defs):
        enabled = workflow_enabled(ctx)
        return [
            replace(tool, sequential=True) if tool.name in WORKFLOW_TOOLS else tool
            for tool in tool_defs
            if (enabled or tool.name not in WORKFLOW_TOOLS)
            and (tool.name != "choose_investment" or ctx.deps.purpose == "gameplay")
            and not (enabled and ctx.deps.purpose == "gameplay" and tool.name == "save_strategy")
        ]

    async def on_tool_validate_error(self, ctx, *, call, tool_def, args, error):
        if not workflow_enabled(ctx) or call.tool_name not in WORKFLOW_TOOLS:
            raise error
        if isinstance(error, ValidationError):
            details = argument_feedback(error, tool_def.parameters_json_schema)
            details["explanations"] = [
                item["msg"]
                for item in error.errors(include_input=False, include_context=False)
                if item["type"] == "value_error"
            ][:3]
        else:
            details = {"instruction": str(error)}
        raise RecoverableToolError(
            recovery_response(
                "INVALID_TOOL_ARGUMENTS",
                "The authored workflow request was not applied.",
                tool=call.tool_name,
                details=details,
            )
        ) from None

    async def read(self, ctx) -> AuthoredDocumentReply:
        reply = await self.documents._request(
            ctx,
            ReadInvestmentDocument(
                model_step=self.documents._step(ctx),
                program_delivered=self.documents._program_delivery(ctx),
            ),
        )
        return self.documents._known_rejection(reply) or AuthoredDocumentReply.model_validate(reply.result)

    async def workspace(self, ctx):
        reply = await self.read(ctx)
        result = self.documents._result(reply)
        strategy = reply.content if isinstance(reply.content, Strategy) else None
        grouped = {}
        for choice in strategy.decisions if strategy is not None else []:
            owner = choice.followThrough.ownerRole if choice.followThrough is not None else None
            if owner is None:
                continue
            if owner not in grouped:
                items = (await self.plans(ctx).invoke(ReadRolePlan(role=owner))).items or []
                grouped[owner] = {item.id: item for item in items}
        result["planning"] = (
            {
                choice.id: {
                    "role": choice.followThrough.ownerRole,
                    "task": item.model_dump(mode="json")
                    if (item := grouped.get(choice.followThrough.ownerRole, {}).get(task_id(choice.id))) is not None
                    else None,
                    "authority": "Existing authored role task, not native execution proof.",
                }
                for choice in strategy.decisions
                if choice.followThrough is not None
            }
            if strategy is not None
            else {}
        )
        return reply, result

    async def sync_task(self, ctx, choice, checkpoint: InvestmentCheckpoint):
        store = self.plans(ctx)
        identifier = task_id(choice.id)
        existing = await store.get_item(identifier)
        prefix = "[investment:" + choice.id + "] "
        if existing is not None and not existing.content.startswith(prefix):
            return {"applied": False, "error": "task_id_conflict", "taskId": identifier}
        status = {
            "active": TaskStatus.in_progress,
            "waiting": TaskStatus.blocked,
            "completed": TaskStatus.completed,
            "abandoned": TaskStatus.cancelled,
        }[checkpoint.status]
        content = (
            prefix
            + choice.objective
            + "\nNext: "
            + checkpoint.nextStep
            + "\nAssessment (authored): "
            + checkpoint.assessment
        )
        if checkpoint.receiptRefs:
            content += "\nHistorical receipts: " + ", ".join(checkpoint.receiptRefs)
        if existing is None:
            item = await store.add_item(PlanItem(id=identifier, content=content, status=status))
        elif existing.content != content or existing.status != status:
            item = await store.update_item(identifier, content=content, status=status)
        else:
            item = existing
        return {
            "applied": True,
            "role": ctx.deps.purpose,
            "task": item.model_dump(mode="json") if item else None,
            "authority": "Authored task status, never proof that a native objective completed.",
        }

    def get_toolset(self):
        if self._tools is not None:
            return self._tools

        async def review_investments(ctx: RunContext[GatewayDependencies]) -> JsonObject:
            """Read the shared strategy, existing owner-role task and historical native receipt progress. Grants no fresh native inspection."""
            return (await self.workspace(ctx))[1]

        async def choose_investment(
            ctx: RunContext[GatewayDependencies],
            choice: InvestmentChoice,
            assessment: Text | None = None,
            replaceDecision: Source | None = None,
        ) -> JsonObject:
            """Main: after review, merge one explicit investment, wait, revision or abandonment into the existing strategy. No native action occurs."""
            semantic = {
                "choice": choice.model_dump(mode="json"),
                "assessment": assessment,
                "replaceDecision": replaceDecision,
            }
            call = ChooseInvestmentDocument(
                choice=choice,
                assessment=assessment,
                replaceDecision=replaceDecision,
                operation=self.documents._operation(ctx, semantic),
                model_step=self.documents._step(ctx),
            )
            raw = await self.documents._request(ctx, call)
            reply = self.documents._known_rejection(raw) or AuthoredDocumentReply.model_validate(raw.result)
            result = self.documents._result(reply)
            if reply.replayed:
                result["planning"] = {
                    "notice": "The strategy edit was already committed. No task mutation was repeated; review the existing role task and track any missing follow-through explicitly."
                }
                return result
            if choice.ownerRole == ctx.deps.purpose:
                result["planning"] = await self.sync_task(
                    ctx,
                    choice,
                    InvestmentCheckpoint(
                        decisionId=choice.id,
                        status=choice.status,
                        nextStep=choice.nextStep,
                        assessment="Chosen investment; native outcome not yet established.",
                    ),
                )
            else:
                result["planning"] = {
                    "ownerRole": choice.ownerRole,
                    "nextStep": choice.nextStep,
                    "notice": "The owner creates/updates its own existing role task with track_investment.",
                }
            return result

        async def track_investment(
            ctx: RunContext[GatewayDependencies], checkpoint: InvestmentCheckpoint
        ) -> JsonObject:
            """Owner: checkpoint a chosen investment in your existing Planning task; copy historical receiptRefs from review_investments."""
            reply, workspace = await self.workspace(ctx)
            strategy = reply.content if isinstance(reply.content, Strategy) else None
            choice = (
                next((item for item in strategy.decisions if item.id == checkpoint.decisionId), None)
                if strategy
                else None
            )
            if choice is None or choice.followThrough is None or choice.followThrough.ownerRole != ctx.deps.purpose:
                raise RecoverableToolError(
                    recovery_response(
                        "INVESTMENT_OWNER_MISMATCH",
                        "This choice is missing or belongs to another role; no task changed.",
                        tool="track_investment",
                        details={
                            "reads": [{"tool": "review_investments", "arguments": {}}],
                            "instruction": "Review the choices and coordinate with the owning role.",
                        },
                    )
                )
            known = {item["receipt"]: item for item in (reply.progress or {}).get("receipts", [])}
            missing = [key for key in checkpoint.receiptRefs if key not in known]
            if missing:
                raise RecoverableToolError(
                    recovery_response(
                        "UNVERIFIED_RECEIPT_REFERENCE",
                        "Receipt references were not verified in scoped retained history; no task changed.",
                        tool="track_investment",
                        details={
                            "reads": [{"tool": "review_investments", "arguments": {}}],
                            "invalidFields": [{"fields": ["receiptRefs"], "code": "unverified"}],
                            "instruction": "Copy receipt references from the returned progress; do not invent or repeat actions to obtain them.",
                        },
                    )
                )
            task = await self.sync_task(ctx, choice, checkpoint)
            return {
                "strategy": workspace,
                "planning": task,
                "receiptEvidence": [known[key] for key in checkpoint.receiptRefs],
                "nativeObjectiveVerified": False,
                "notice": "Receipt evidence verifies its operation only; compare current native observations with the expected objective before claiming success.",
            }

        self._tools = FunctionToolset(
            [
                Tool(review_investments, takes_ctx=True, max_retries=0),
                Tool(choose_investment, takes_ctx=True, max_retries=0),
                Tool(track_investment, takes_ctx=True, max_retries=0),
            ],
            id="investment-workflow-tools-v1",
            max_retries=0,
        )
        return self._tools


def investment_workflow_capability(client) -> InvestmentWorkflow:
    return InvestmentWorkflow(client)
