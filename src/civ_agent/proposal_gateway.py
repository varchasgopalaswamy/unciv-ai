"""Job-scoped HTTPS tools. Only this trusted controller holds host credentials."""

import asyncio
import copy
import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import aiohttp
from aiohttp import web
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from civ_agent import experiment_metrics
from civ_agent.action_templates import add_decision_commands, research_change
from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.coordination_updates import (
    apply_coordination_update,
    payload_characters,
)
from civ_agent.coordination_updates import (
    content as coordination_content,
)
from civ_agent.coordination_updates import (
    response as coordination_response,
)
from civ_agent.coordination_updates import (
    snapshot as coordination_snapshot,
)
from civ_agent.decisions_ledger import fingerprint
from civ_agent.delivery_options import content_arguments, full_delivery
from civ_agent.diplomacy_tools import argument_error, commitment_problem, unavailable
from civ_agent.execution_windows import ExecutionWindowScheduler, native_effect_started, record_invalidation
from civ_agent.feedback_view import compact_feedback
from civ_agent.game_commands import add_combat_commands, command_arguments, command_error, command_kinds, command_schema
from civ_agent.host_effects import HOST_EFFECT_TOOLS, PreparedHostEffect, host_request_id
from civ_agent.inspection_bundle import run_bundle
from civ_agent.mcp_results import RESULT_FORMAT_HEADER, STRUCTURED_RESULT_FORMAT, canonical_tool_result
from civ_agent.memory_delivery import apply_memory_delivery, memory_response
from civ_agent.memory_records import (
    Intent,
    MemoryReadResult,
    MemorySearchResult,
    MemoryUpdate,
    MemoryUpdateResult,
    memory_argument_error,
    public_record,
)
from civ_agent.memory_store import RecordStore
from civ_agent.model_view import model_view, trade_inspection
from civ_agent.observations import (
    DETAIL_MODELS,
    EXTERNAL_DETAIL_TOOLS,
    DetailQuery,
    TurnOverview,
    detail_fingerprint,
    detail_scope,
    native_observation,
    overview,
    project_detail,
    required_detail,
)
from civ_agent.payload_updates import (
    apply_detail_response,
    apply_memory_receipt,
    compact_memory_receipt,
    detail_response,
)
from civ_agent.protocol import JsonObject
from civ_agent.response_compaction import compact_response, expand_response
from civ_agent.review_delivery import review_brief
from civ_agent.review_grounding import ReviewEvidence, source_catalogue
from civ_agent.role_view import label_commands, role_overview
from civ_agent.schemas import validate
from civ_agent.service_io import ServiceIO
from civ_agent.social_context import social_context
from civ_agent.state_updates import apply_overview_update, changed_details, overview_update, refresh_required, stamp
from civ_agent.strategic_review import frozen_read
from civ_agent.tactical_review import strategy_review, tactical_review
from civ_agent.telemetry import JobAccounting
from civ_agent.tool_schemas import TOOL_MODELS, TOOL_OUTPUT_MODELS, tool_model
from civ_agent.war_state import MILITARY_COMMANDS, ROLE_TOOLS, WarStore, declaration_targets, goal_versions

BOOKKEEPING = {"requestId", "epoch", "revision", "reviewId"}
NATIVE_TRADE_GAMEPLAY_ONLY = "Handle built-in AI trade and friendship decisions during your gameplay turn."
READ_TOOLS = {
    "player_capabilities",
    "player_turn",
    "player_result",
    "research_details",
    "movement_preview",
    "mission_preview",
    "player_inspect",
    "policy_details",
    "victory_progress",
}
COMMUNICATION_READ_TOOLS = {"player_turn", "player_result", "victory_progress"}


def public_result(value: Any) -> Any:
    return _without_bookkeeping(model_view(value))


def _without_bookkeeping(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_bookkeeping(
                {name: part for name, part in item.items() if name != "id"}
                if key == "turnReview" and isinstance(item, dict)
                else item
            )
            for key, item in value.items()
            if key not in BOOKKEEPING and key not in {"peaceVersion", "_evidence", "_assumptions", "_pinned"}
        }
    if isinstance(value, list):
        return [_without_bookkeeping(item) for item in value]
    return value


def tool_result(value: JsonObject) -> JsonObject:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, separators=(",", ":"))}],
        "structuredContent": value,
        "isError": "error" in value,
    }


class PlayerConnection(Protocol):
    async def close(self) -> None: ...
    async def list_tools(self) -> list[JsonObject]: ...
    async def tool(self, name: str, arguments: JsonObject, /) -> JsonObject: ...


@dataclass
class JobAccess:
    id: str
    purpose: str
    epoch: str
    connection: PlayerConnection
    memory: JsonObject
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    observation: JsonObject | None = None
    # Retain the last observed facts solely for exact invalidation comparisons.
    # observation=None still fences decisions until the native position is read.
    prior_role_observation: JsonObject | None = None
    delivered_overview: JsonObject | None = None
    compact_overviews: bool = False
    resume_delta_available: bool = False
    overview_view: str = "full"
    detail_seen: dict[str, JsonObject] = field(default_factory=dict)
    detail_pending: dict[str, JsonObject] = field(default_factory=dict)
    memory_seen: dict[str, JsonObject] = field(default_factory=dict)
    memory_pending: dict[str, JsonObject] = field(default_factory=dict)
    memory_read_seen: dict[str, JsonObject] = field(default_factory=dict)
    memory_read_pending: dict[str, JsonObject] = field(default_factory=dict)
    memory_versions: dict[str, int] = field(default_factory=dict)
    payload_sequence: int = 0
    social: JsonObject | None = None
    social_read: bool = False
    detail_revision: str | None = None
    details_read: list[DetailQuery] = field(default_factory=list)
    pending_invalidated: list[DetailQuery] = field(default_factory=list)
    role_refresh_required: bool = False
    city_scope: list[str] | None = None
    economy_seen: JsonObject | None = None
    coordination_seen: JsonObject | None = None
    coordination_scope: str = field(default_factory=lambda: secrets.token_hex(12))
    coordination_sequence: int = 0
    coordination_preparing: bool = False
    coordination_pending: JsonObject | None = None
    # Exact terms inspected by this job, independent of native board revisions.
    trades: dict[str, JsonObject] = field(default_factory=dict)
    closed: bool = False
    verified: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    bundle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    role_wait_pending: asyncio.Event = field(default_factory=asyncio.Event)
    role_wait_calls: dict[asyncio.Task, JsonObject] = field(default_factory=dict)
    wait_started: float | None = None
    completed_wait: float = 0
    owns_connection: bool = False
    catalogue: list[JsonObject] | None = None
    gameplay_fenced: bool = False
    handoff_at: float | None = None
    gameplay_ended: asyncio.Event = field(default_factory=asyncio.Event)
    model_stopped: asyncio.Event = field(default_factory=asyncio.Event)
    war_versions: dict[str, int] = field(default_factory=dict)
    review_inputs: JsonObject | None = None
    review_evidence: ReviewEvidence | None = None
    outgoing_start: int = 0
    seen_messages: set[str] = field(default_factory=set)
    # Structured batches pin this to model-delivered context, not host reads.
    delivered_messages: set[str] | None = None
    pending_calls: dict[asyncio.Task, JsonObject] = field(default_factory=dict)
    negotiation_calls: dict[asyncio.Task, JsonObject] = field(default_factory=dict)
    completed_actions: dict[asyncio.Task, JsonObject] = field(default_factory=dict)
    negotiation_pending: asyncio.Event = field(default_factory=asyncio.Event)
    suspend_for_negotiation: bool = False
    role_finished: asyncio.Event = field(default_factory=asyncio.Event)

    def waiting_seconds(self) -> float:
        return self.completed_wait + (time.monotonic() - self.wait_started if self.wait_started is not None else 0)


class ProposalGateway:
    def __init__(self, memory: CivilizationState, io: ServiceIO, workspace: Path):
        self.memory, self.io, self.workspace = memory, io, workspace
        self.jobs: dict[str, JobAccess] = {}
        self.paused = False
        self.reviewing = False
        self.checkpoint_requested = False
        self.checkpoint_released = asyncio.Event()
        self.checkpoint_released.set()
        self.calls_in_flight = 0
        self.dispatch = asyncio.Lock()
        self.role_dispatch = asyncio.Lock()
        self.execution_windows: ExecutionWindowScheduler | None = None
        self.wars = WarStore(memory)
        self.peace_reviews: Any = None
        self.consultation_hold = asyncio.Event()
        self.consultation_hold.set()
        self.role_coordinator: Any = None
        self.in_flight = 0
        self.deferred_completions = []
        self.accounting: JobAccounting | None = None
        self.history = None
        self.run_failure = memory.run_failure()
        self.routing = None
        self.native_advice_enabled = False
        self.advisories = None
        self.failed = asyncio.Event()
        if self.run_failure:
            self.failed.set()

    @asynccontextmanager
    async def admit(self, *, review=False, settlement_epoch=None):
        """Drain accepted calls before capture; later calls wait without model retries."""
        await self._wait_for_admission(review=review, settlement_epoch=settlement_epoch)
        self.calls_in_flight += 1
        try:
            yield
        finally:
            self.calls_in_flight -= 1

    async def _wait_for_admission(self, *, review=False, settlement_epoch=None):
        # MCP initialization and tool calls must use the same barrier rule.
        # Reviewers have frozen inputs and may connect during the quiet phase.
        if self.retiring_epoch():
            if settlement_epoch is not None and self.retiring_epoch(settlement_epoch):
                return
            raise StateConflict("SOURCE_EPOCH_RETIRED")
        while not self.checkpoint_released.is_set() and not (review and self.reviewing):
            await self.checkpoint_released.wait()
            if self.retiring_epoch():
                if settlement_epoch is not None and self.retiring_epoch(settlement_epoch):
                    return
                raise StateConflict("SOURCE_EPOCH_RETIRED")

    def retiring_epoch(self, epoch=None):
        with self.memory.transaction() as db:
            current = self.memory._meta(db, "epoch")
            retired = self.memory._meta(db, "nativeGatewayRetirement")
            return retired is not None and retired["epoch"] == (epoch if epoch is not None else current)

    def retire_source_epoch(self, epoch, owner_sha256):
        """Permanent source admission fence; accepted receipts may still settle."""
        with self.memory.transaction() as db:
            if self.memory._meta(db, "epoch") != epoch:
                raise StateConflict("DECISION_CONTEXT_EXPIRED")
            previous = self.memory._meta(db, "nativeGatewayRetirement")
            value = {"epoch": epoch, "ownerSha256": owner_sha256}
            if previous is not None and previous != value:
                raise StateConflict("SOURCE_RETIREMENT_CONFLICT")
            self.memory._set(db, "nativeGatewayRetirement", value)
        windows = getattr(self, "execution_windows", None)
        if windows is not None:
            windows.interrupt("source_epoch_retired")
        if self.advisories is not None:
            self.advisories.suspend()
        # Wake checkpoint waiters so the permanent rejection above can settle
        # their original calls instead of waiting for an inadmissible release.
        self.checkpoint_released.set()

    def request_checkpoint(self):
        windows = getattr(self, "execution_windows", None)
        if windows is not None:
            windows.suspend("checkpoint_requested")
        if self.routing is not None:
            self.routing.suspend()
        self.checkpoint_requested = True
        self.checkpoint_released.clear()
        if self.advisories is not None:
            self.advisories.suspend()

    def release_checkpoint(self):
        self.checkpoint_requested = False
        resume_advisories = getattr(self.advisories, "resume", None)
        if callable(resume_advisories):
            resume_advisories()
        self.checkpoint_released.set()
        windows = getattr(self, "execution_windows", None)
        if windows is not None:
            windows.resume()

    async def resume_run(self):
        if self.jobs:
            raise StateConflict("ACTIVE_JOBS")
        await self.io.call(self.memory.export_failure)
        await self.io.call(self.memory.resume_run)
        self.run_failure = None
        self.failed.clear()

    async def sync_failure(self):
        failure = await self.io.call(self.memory.run_failure)
        if failure:
            self.run_failure = failure
            try:
                if not self.failed.is_set():
                    await self.io.call(self.memory.export_failure)
            finally:
                self.failed.set()

    async def open(
        self,
        id: str,
        purpose: str,
        epoch: str,
        connection: PlayerConnection,
        settings: JsonObject,
        game_turn: int | None = None,
        *,
        owns_connection: bool = False,
        memory_snapshot: JsonObject | None = None,
    ) -> JobAccess:
        async with self.admit():
            return await self._open(
                id,
                purpose,
                epoch,
                connection,
                settings,
                game_turn,
                owns_connection=owns_connection,
                memory_snapshot=memory_snapshot,
            )

    async def _open(
        self, id, purpose, epoch, connection, settings, game_turn, *, owns_connection, memory_snapshot
    ) -> JobAccess:
        if self.run_failure:
            raise StateConflict(self.run_failure["error"])
        await self.io.call(self.memory.bind, epoch)
        try:
            snapshot = await self.io.call(self.memory.start_job, id, purpose, settings, game_turn, memory_snapshot)
        except StateConflict:
            await self.sync_failure()
            raise
        job = JobAccess(id, purpose, epoch, connection, snapshot, owns_connection=owns_connection)
        if purpose == "communication":
            job.overview_view = "communication"
        job.outgoing_start = await self.io.call(self.memory.outgoing_position)
        self.jobs[job.token] = job
        return job

    async def close(self, job: JobAccess, completed: bool):
        job.closed = True
        self.jobs.pop(job.token, None)
        pending = list(job.pending_calls)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        job.negotiation_calls.clear()
        job.role_wait_pending.clear()
        job.role_wait_calls.clear()
        job.completed_actions.clear()
        if job.review_inputs is not None:
            return
        if job.owns_connection:
            await job.connection.close()
        if self.paused or self.checkpoint_requested:
            self.deferred_completions.append((job.epoch, job.id, completed))
            return
        await self.io.call(self.memory.finish_job, job.id, completed)

    async def select_startup_memory(self, job: JobAccess, topics: set[str]):
        async with self.admit(), job.lock:
            self.check(job)
            selection = await self.io.call(
                self.memory.select_startup_context,
                job.id,
                topics,
                job.memory.get("advisoryRanking"),
                {
                    city["handle"]
                    for city in (job.observation or {}).get("cities", [])
                    if job.city_scope is None or city["handle"] in job.city_scope
                }
                if job.purpose == "economy"
                else None,
            )
            job.memory.update(selection)

    def check(self, job: JobAccess, *, ended_allowed=False):
        if self.run_failure:
            raise StateConflict(self.run_failure["error"])
        if (
            (self.paused and not (self.reviewing and job.review_inputs is not None))
            or job.closed
            or self.jobs.get(job.token) is not job
        ):
            raise StateConflict("JOB_PAUSED_OR_EXPIRED")
        if job.gameplay_fenced and not ended_allowed:
            raise StateConflict("TURN_ENDED: finish this conversation")

    async def verify_identity(self, job: JobAccess):
        if not job.verified:
            identity = await job.connection.tool("player_communication", {"op": "state", "epoch": job.epoch})
            if identity.get("civilization") != self.memory.civilization or identity.get("epoch") != job.epoch:
                raise StateConflict("PLAYER_CREDENTIAL_MISMATCH")
            job.verified = True

    async def tools(self, job: JobAccess):
        async with self.admit(review=job.review_inputs is not None):
            tools = await self._tools(job)
            if self.memory.authored_enabled:
                tools = [
                    tool for tool in tools if tool["name"] not in {"memory_read", "memory_search", "memory_update"}
                ]
                job.catalogue = copy.deepcopy(tools)
            return tools

    async def _tools(self, job: JobAccess):
        self.check(job, ended_allowed=True)
        if job.gameplay_fenced:
            return []
        if job.catalogue is not None:
            return source_catalogue(job.catalogue) if job.review_inputs is not None else copy.deepcopy(job.catalogue)
        upstream = await job.connection.list_tools()
        job.catalogue = self.tool_catalogue(
            upstream,
            job.purpose,
            routing_enabled=self.routing is not None,
            native_advice_enabled=self.native_advice_enabled,
        )
        catalogue = copy.deepcopy(job.catalogue)
        if self.role_coordinator is not None and job.purpose in {"gameplay", "economy", "military"}:
            from civ_agent.economy_roles import CITY_COMMANDS
            from civ_agent.unit_ownership import WORKER_COMMANDS

            for entry in catalogue:
                if entry["name"] == "player_command":
                    kinds = command_kinds(entry["inputSchema"])
                    allowed_kinds = {
                        kind
                        for kind in kinds
                        if kind not in {"END_TURN", "AUTOPLAY", "MOVE_AUTOMATED_UNITS"}
                        and (job.purpose != "economy" or kind in CITY_COMMANDS | WORKER_COMMANDS)
                        and (job.purpose != "military" or kind in MILITARY_COMMANDS)
                    }
                    entry["inputSchema"] = command_schema(allowed_kinds, military_review=job.purpose == "military")
                    entry["description"] += " City economic commands require economy ownership or Main's city_override."
                    if job.purpose == "military":
                        entry["description"] += (
                            " Add reviewAfter=true to receive a fresh militaryCompletion with the successful action's "
                            "state update, avoiding a separate military_review call. Failed actions do not run the review."
                        )
        if self.role_coordinator is not None and job.purpose in {"economy", "military"}:
            from civ_agent.economy_roles import ECONOMY_TOOLS

            allowed = (
                ECONOMY_TOOLS | {"war_goals", "role_message"}
                if job.purpose == "economy"
                else ECONOMY_TOOLS | ROLE_TOOLS | {"economy_request", "military_review"}
            )
            catalogue = [entry for entry in catalogue if entry["name"] in allowed]
        job.catalogue = catalogue
        return catalogue

    @staticmethod
    def tool_catalogue(upstream, purpose, *, routing_enabled=False, native_advice_enabled=False):
        """Project native tools without starting a gameplay or notebook job."""
        tools = []
        for tool in upstream:
            if tool["name"] in READ_TOOLS and (
                purpose in {"gameplay", "economy", "military"} or tool["name"] in COMMUNICATION_READ_TOOLS
            ):
                entry = copy.deepcopy(tool)
                if tool["name"] == "player_turn":
                    entry["inputSchema"] = TOOL_MODELS["player_turn"].model_json_schema()
                    entry["description"] = (
                        "Read the current position, decisions, alerts and review; details expose options/consequences, "
                        "and chat_read supplies social promises. Use returned stateUpdate before requesting another overview.\n"
                        "In victory.ballot, turnsUntilNextVote measures your own ballot. Read cityStateVoteTiming for "
                        "your last turn to influence the earliest pending votes of known city-states. "
                        "stateUpdate: full replaces the overview; delta requires matching baseStateId, applies set and "
                        "top-level removed, then collections (upsert replaces rows by key, removed deletes rows, order "
                        "sets order). Unmentioned sections/rows remain; fields absent from that replacement are removed. "
                        "unchanged keeps data. Adopt stateId; ignore an update already at that ID. Recover missing/mismatched "
                        "bases with a full player_turn inspection. Removal does not prove an entity ceased to exist. "
                        "unavailable requires a read while retaining the successful receipt; never repeat that action. "
                        "turn_ended ends the conversation.\n"
                        "Apply invalidatedDetails even with full/unchanged replies or communication results; reread only "
                        "needed invalidated inspections. Other inspections remain valid. Detail replies can carry sibling "
                        "stateUpdate: apply it first. Separate research/policy/victory/result/trade/agreement/chat reads "
                        "are not refreshed by an overview. applied=false argument errors need no refresh unless recovery.reads says so.\n"
                        + (
                            "Communication starts with a focused view. Use view=full or roleBrief's focused reads for "
                            "missing facts; omitting view keeps the selection. Gameplay handles native alerts and decisions. "
                            if purpose == "communication"
                            else "Check alerts/decisions: native.availableDemandsToSend lists outgoing demands; alerts.nativeDiplomacy "
                            "contains incoming messages. Copy RESPOND_CITY_STATE_ALERT for city-states or responseTool with "
                            "chat_send/nativeAction for majors. "
                        )
                        + "memoryUpdates replace records by ID only with newer versions. "
                        "Review notices suggest reconsideration without requiring edits or changed actions."
                    )
                    entry["outputSchema"] = TurnOverview.model_json_schema()
                    entry["outputSchema"]["properties"]["sharedValues"] = {"type": "object"}
                    definitions = entry["outputSchema"].pop("$defs", {})
                    entry["outputSchema"] = {
                        "anyOf": [entry["outputSchema"], {"type": "object", "required": ["stateUpdate"]}],
                        **({"$defs": definitions} if definitions else {}),
                    }
                elif tool["name"] == "player_capabilities":
                    entry["description"] = "List available observation tools and game actions."
                elif tool["name"] in {"research_details", "policy_details"}:
                    entry["description"] = entry["description"].replace(
                        "re-observe afterward", "read the returned stateUpdate"
                    )
                    entry["description"] += (
                        " Prefer names=[candidate names] from the overview for detailed relevant choices and copyable "
                        "commands; omit names for broader planning. Copy command; intent is optional."
                    )
                    if tool["name"] == "research_details":
                        entry["description"] += (
                            " SET_RESEARCH_QUEUE replaces current research and the entire queue. "
                            "command starts the offered path now; appendToQueue preserves all queued technologies. "
                            "Inspect researchQueue and the command label before choosing. Stored science is retained."
                        )
                elif tool["name"] == "mission_preview":
                    entry["description"] += (
                        " Use unit_details sections=[air] for air options; copy this preview's command with optional intent. "
                        "Refresh the preview after another action before using its command. "
                        "Interception is automatic; preserve readiness by leaving the unit unused or skipping it."
                    )
                elif tool["name"] == "movement_preview":
                    entry["description"] = (
                        "Preview an owned unit's destination x/y using native pathfinding. Paths group adjacent steps by "
                        "estimated turns; unexploredTiles counts uncharted tiles, tilesOutsideCurrentSight includes them. "
                        "Terrain/units discovered later can change arrival estimates. Each stop's afterMove estimates remaining "
                        "movement, attack/founding availability and nearby visible threats/own military. Proximity is not "
                        "enemy reach or guaranteed protection; unseenTiles marks incomplete visibility.\n"
                        "Civilian captureRisk identifies visible enemies able to capture the unguarded stop. First-stop risk "
                        "requires confirmAction=true; later turns recheck danger and stop for a new decision. Confirmation "
                        "covers only this move. civilianExposure adds protection, visibility gaps and references to known "
                        "camps/recent tacticalReview sightings. NO_VISIBLE_CAPTURE_THREAT does not mean safe. Same-tile "
                        "units travel together only after ESCORT pairing; escort names the partner, afterMove.escort its "
                        "movement at shared stops. Use unit orders ESCORT/STOP_ESCORT to pair/separate.\n"
                        "For settlers, settlement.siteStatus is ELIGIBLE/BLOCKED on explored destinations using native "
                        "restrictions, including undiscovered cities without revealing their identities; UNKNOWN means "
                        "unexplored. unitBlockers (e.g. NO_MOVEMENT) are separate; canFoundNow requires occupying the site. "
                        "Movement availability does not imply settlement eligibility. Check before choosing a site and "
                        "recheck unit_details sections=[settlement] on arrival. Waiting does not fix spacing/foreign ownership. "
                        "Reconsider repeatedly blocked sites.\n"
                        "Copy SET_UNIT_DESTINATION for this exact target/destination; refresh the preview after another "
                        "action. It moves now and retains the native order for later turns, without promising arrival. "
                        "Inspect unit.movementOrder or unit_details sections=[movement]; CANCEL_UNIT_MOVEMENT stops it. Intent is optional."
                    )
                tools.append(entry)
            elif tool["name"] == "player_command" and purpose in {"gameplay", "economy", "military"}:
                entry = copy.deepcopy(tool)
                entry["inputSchema"] = command_schema(military_review=purpose == "military")
                entry["description"] = (
                    "Take one game action using kind, such as FOUND_CITY. Copy offered commands when useful. "
                    "Intent and routine detail reads are optional; required previews still apply. SET_UNIT_DESTINATION "
                    "requires a matching movement_preview(target,destination). For founding consequences, read "
                    "unit_details sections=[settlement].\n"
                    "Add a confirmationField only after reading and accepting the consequences yourself. For civilian "
                    "captureRisk, confirmAction=true accepts this move's exposure only, not later turns of the route. "
                    "Invalid arguments return expectedArguments without acting. Check the outcome and apply stateUpdate.\n"
                    "Read alerts before answering or acknowledging them. Copy the overview's commands: ACKNOWLEDGE_ALERT "
                    "uses target; RESPOND_CITY_STATE_ALERT uses target and the offered selection; DISMISS_NOTIFICATIONS "
                    "uses targets=[read notice handles] and keeps their contents in receipts. CAST_DIPLOMATIC_VOTE uses "
                    "selection=candidate civilizationId from victory.ballot. ABSTAIN_DIPLOMATIC_VOTE and "
                    "ACKNOWLEDGE_DIPLOMATIC_RESULTS need only kind and optional intent; read victory.votingResults "
                    "before acknowledging.\n"
                    "Review pending decisions before finish_role. Idle units and tactical warnings are advisory. "
                    "Use finish_role when your work is complete; only the host advances the native turn."
                )
                tools.append(entry)

        def add(name, description):
            tools.append(
                {
                    "name": name,
                    "description": description,
                    "annotations": {
                        "readOnlyHint": name
                        in set(DETAIL_MODELS)
                        | {
                            "inspect_bundle",
                            "military_review",
                            "command_help",
                            "history_read",
                            "chat_read",
                            "chat_history",
                            "native_advice",
                            "memory_read",
                            "memory_search",
                            "trade_options",
                            "trade_prepare",
                            "trade_read",
                            "agreement_list",
                            "agreement_read",
                        },
                        "destructiveHint": False,
                        "openWorldHint": False,
                    },
                    "inputSchema": tool_model(name, purpose).model_json_schema(),
                }
            )
            if name == "chat_read" and not (routing_enabled and purpose == "gameplay"):
                tools[-1]["inputSchema"]["properties"].pop("handledMessages", None)
            if name in TOOL_OUTPUT_MODELS:
                tools[-1]["outputSchema"] = TOOL_OUTPUT_MODELS[name].model_json_schema()
                tools[-1]["outputSchema"]["properties"]["sharedValues"] = {"type": "object"}

        if purpose in {"gameplay", "economy", "military"}:
            add(
                "inspect_bundle",
                "Read up to 12 independent inspections in one call using queries=[{tool,arguments}]. Each read "
                "retains its own permissions, evidence and recovery. Apply results in order, including any "
                "stateUpdate, then decide. Stops at the first error. positionChanged reports a revision change; "
                "this is not an atomic snapshot. Actions, chat and nested bundles are not accepted.",
            )
            add(
                "command_help",
                "Read the complete argument schema and guidance for up to 16 player_command kinds allowed "
                "for this role. Common actions can copy commands already supplied without this read.",
            )
            add(
                "finish_role",
                "Finish your work for this role. The host waits for every active role and advances the native turn. Economy reports a disposition for each urgent request; deferred requests may set a future reviewTurn. Military supplies militaryReview with a current basis and reasoned holds for remaining completion-review targets. Stop issuing actions after success; another pass may be requested.",
            )
        if purpose == "military":
            add(
                "military_review",
                "Read a fresh, paginated completion check of exposed units and remaining native attack/capture/bombardment opportunities, plus observed progress and active-operation references. Resolve them with actions or reasoned holds in finish_role.militaryReview. All native-legal choices, including accepting risk, remain available. Follow nextCursor until all targets are reviewed.",
            )
        if purpose in {"gameplay", "military"}:
            add(
                "economy_request",
                "Send a new city production or economic priority to the economy manager. Routine requests wake it next own turn; urgent requests start or reopen it now. Do not repeat unchanged requests.",
            )
        if purpose == "gameplay":
            add(
                "economy_wait",
                "Suspend inference while awaiting economy, leaving your role unfinished. Supply requestIds to wait "
                "for recorded completed/deferred/rejected dispositions, or omit them to wait for the current pass. "
                "Returns immediately if already resolved or economy is idle; it never wakes economy or upgrades "
                "a routine request. New coordination or decisions can wake you early. The host resumes you with "
                "fresh facts, within 300 seconds and your activation deadline. Call this alone, then stop issuing "
                "tools until resumed. If you have no remaining decisions, use finish_role instead.",
            )
            add(
                "city_override",
                "Take economic control of an owned city for the rest of this turn. Supply a reason. This fences conflicting economy commands.",
            )

        for role_tool, description in {
            "war_goals": "Read private war episodes, goals, operations, escort assignments and messages. For initial, main must supply opponent, nonempty goals and a reason, including when already at war. For propose, supply the current version, replacement goals and a reason. The counterpart uses respond with proposal, version, agree and reason before either role finalizes. Main breaks disagreement with a reason. Priority 1 is highest. Use the current version and proposal IDs.",
            "escort_assignment": "Main assigns a military unit to escort duty with an owned civilian, or releases it after unpairing. Main controls both units for the duration of the assignment.",
            "military_operation": "Maintain an ongoing peacetime or wartime operation under a stable ID. Update it across turns; do not create per-turn journals. Set status to active, completed, abandoned or superseded; supersedes lists replaced IDs. action=set_status changes dispositions of existing IDs with a reason, including explicit reconciliation of legacy notes. Read older revisions with war_goals section=operation_history. Notes do not establish factual outcomes. Use role_message for attention or economy_request for supplies.",
            "role_message": "Send a durable private notice or request to another internal role; a finished recipient is reopened. Only the addressed recipient may acknowledge read messages with action=acknowledge and ids, or resolve requests with action=resolve, ids and a reason. Acknowledgement alone does not resolve a request. Economy request IDs belong in Economy's finish decision arguments.requests. History is available with war_goals section=messages, role and cursor. Record lasting instructions in current goals, active operations or economy requests.",
        }.items():
            if role_tool == "war_goals" or purpose in {"gameplay", "economy", "military"}:
                if role_tool == "war_goals" and purpose in {"economy", "communication"}:
                    description = (
                        "Read current accepted war goals; use section, IDs and cursor for operations, escort assignments, "
                        "role requests and older records. Main and Military own goal changes. "
                        + (
                            "Use role_message to request reconsideration."
                            if purpose == "economy"
                            else "Prepared goals already count as read; reread when missing or changed."
                        )
                    )
                add(role_tool, description)

        for name, description in {
            "unit_details": (
                "Inspect an owned unit using its player_turn handle. Choose sections: movement (paths, swaps and orders; "
                "use movement_preview for distant destinations), combat, orders (rename, guard, set up, escort, explore, "
                "automate, fortify, sleep, skip, wake and stopping orders), promotions (including saved city defaults), "
                "work (roads and improvements), settlement (founding requirements and promises), economy "
                "(upgrade/pillage/disband), abilities (great people/spaceship/gifts), or air (missions, bases, capacity "
                "and interception). Use mission_preview for special missions and movement_preview for prospective "
                "city sites. Multiple sections are allowed; omit sections or use [all] for everything. Only returned "
                "sections count as inspected. EXPLORE and AUTOMATE persist across turns; Worker automation chooses "
                "and builds improvements. Inspect orders to stop them. Work Boats are consumed when improving a tile. "
                "For movement captureRisk, confirmAction=true accepts exposure for this move; a same-tile escort "
                "permits normal movement. Read warnings and confirmations, then copy an offered command."
            ),
            "city_details": (
                "Inspect an owned city using its player_turn handle. Choose sections: status (capture choices), "
                "production (queue and options), citizens (growth, worked tiles, specialists and focus), economy "
                "(yields, buildings and sales), purchases, or combat (bombardment). Multiple sections are allowed; "
                "omit sections or use [all] for everything. Only returned sections count as inspected. For specific "
                "buildings or units, use construction_details(names=[candidates]). Read consequences and copy an "
                "offered command."
            ),
            "construction_details": (
                "Compare things an owned city could build or buy. Omit names for completion times, ongoing yield "
                "gains, maintenance and knownNames. Supply up to 16 names for detailed costs, requirements, "
                "before/after yields and their sources, conditional effects, specialist slots, queue progress and "
                "commands. unknownNames includes suggestions; known but blocked choices show their requirements.\n"
                "Times assume starting now and exclude earlier queue entries or missing research. Locked victory "
                "projects and spaceship parts still show costs and technology/resource requirements. queueOptions "
                "distinguishes continuing, reordering and adding an item.\n"
                "Comparisons hold population and citizen assignments fixed. Food is net surplus; Gold includes "
                "maintenance. oneTime lists completion grants separately. whileBuilding compares the immediate "
                "change from current construction. Empire happiness and food effects assume completion now. "
                "Conversion yields include their modifiers.\n"
                "horizonTurns (1–100) requires a nonempty names list and compares building or buying against "
                "continuing current output. following "
                "compares converting now with building or buying and then selecting that conversion. endOfHorizon "
                "shows completed assets or unfinished work. Read the named options and copy a command; add purchase "
                "confirmation only after accepting the consequences."
            ),
            "map_region": (
                "Read explored tiles and frontier within radius 0–4 of an observed coordinate, including world wrap. "
                "Unexplored tiles have no data. Outside current sight, check visibility and observation dates for "
                "remembered information. With format=compact, tile fields override tileDefaults, even when null, "
                "false or empty; visible facts are retained. Use format=full for expanded tiles, or "
                "player_details(map_index) to locate distant regions."
            ),
            "player_details": (
                "Read your economy, development opportunities, diplomacy and war consequences, settlement sites, "
                "research, policies, current/previous turn receipts, or map index. Site scores use current and "
                "remembered information; preview a route before travel. The evidence ID identifies this read. "
                "For CITY_STATE_ACTION, read section=diplomacy and copy target=civilizationId and "
                "selection=available actions.name. Add confirmAction=true only after accepting the consequences. "
                "DECLARE_WAR also accepts city-states. receipt.cityState reports influence and alliance changes."
            ),
        }.items():
            if name in {"unit_details", "city_details"}:
                description += (
                    " Combat HP and remaining-HP estimates may change with death, withdrawal or interception. "
                    "destructionPossible warns of risk; attackerConsumedOnAttack means consumption regardless of "
                    "damage. Nearby forces do not establish enemy reach or protection. afterMove estimates movement "
                    "and actions after completing the route; recheck on arrival."
                )
            add(name, description)

        add(
            "trade_options",
            "Inspect trade terms, city names/populations, eligibility, unavailable reasons, research funding, friendship and active agreements with a met player. WarDeclaration names a third civilization both players know; give commits you to war and receive asks the partner to declare. Your own native declaration warnings and possibly relevant social promises are included. Weigh socialAdvisories when choosing; they never restrict legal actions. give/receive are from your perspective. City names in terms are short trade IDs. Available during either player's turn.",
        )
        add(
            "trade_prepare",
            "Prepare a detached trade draft from give/receive items. Pairs treaty terms and optionally adds immediate gold to fund a research agreement. Returns explicit final terms and validity; no offer or game effects. Review the result and pass its exact give/receive to trade_offer to publish.",
        )
        add(
            "trade_read",
            "Read a binding trade before accepting, declining, withdrawing or counteroffering. Copy its inspection command from chat_read. Every clause has an explicit from/to direction, identical for both players. Interpret socialAdvisories yourself; they never restrict legal actions. Re-read changed terms; copy an available response; your private intent is optional.",
        )
        add(
            "agreement_list",
            "List current and historical agreement summaries, including ended agreements. Current summaries are already in chat_read; use this tool to find older agreement IDs. Read agreement_read for exact terms and ending history. The returned evidence ID can support private memory.",
        )
        add(
            "agreement_read",
            "Read one of your enacted agreements by its agreement ID: first-observed and current give/receive terms, remaining turns, ending reasons and effects. give/receive are from your perspective. Friendship and pact amounts count relationships, not signing prices. Fixed-duration agreements cannot be cancelled directly.",
        )
        add(
            "trade_offer",
            "Propose binding terms from trade_options when canInitiate is true. Copy the offered name, amount and duration for Embassy, Agreement, City, Treaty and WarDeclaration; choose resource and gold amounts within availability. WarDeclaration in give makes YOU declare; in receive makes the PARTNER declare. Both sides can declare on the same target for joint war. Inspect your declaration warnings. Social promises never restrict legal game actions. Use trade_prepare to pair treaties and prepare research funding; review and publish its explicit terms. Research Agreement amount is each player's signing cost after immediate gold transfers. give/receive are from YOUR perspective. Set counterofferTo to counter a trade inspected with trade_read when counterofferAvailable is true. No effect until acceptance. Avoid duplicate proposals; changed circumstances or terms may justify a new proposal.",
        )
        add(
            "friendship_offer",
            "Propose a Declaration of Friendship with a met player when trade_options permits it, using its fixed terms. An accepted declaration unlocks eligible research agreements and defensive pacts. A verbal promise alone does not establish this relationship.",
        )
        add(
            "trade_decide",
            'Accept or decline the exact trade or friendship proposal you inspected with trade_read; an author can withdraw it. Copy a returned response; your private intent is optional, for example {"proposalId":"trade1","decision":"withdraw","intent":{"reason":"Preserve funds"}}. Put the reason inside intent, not at the top level. Acceptance applies all agreed terms together if still legal. Only ENACTED with an outcome confirms execution. Changed, expired or unavailable terms require a new decision.',
        )
        add(
            "history_read",
            "Inspect confirmed actions and detailed receipts for a game turn. includeReview=true retrieves the exact completed strategic review for that turn, including its dated assumptions; a null review means none completed at that turn.",
        )
        add(
            "chat_read",
            "Read new messages, current promises, pending trade IDs and active agreements. conversationContext "
            "includes recent messages with the same participants and your confirmed sends, with IDs and turns. "
            "Use chat_history for older messages. A proposed revision does not replace an older promise still in "
            "effect; check their versions. includeContext=true adds rules, economy and known diplomacy when you "
            "lack an overview. includeHistory=true adds older records; use trade_read or agreement_read for exact "
            "terms. Read a binding trade before deciding. Social promises express intentions; they do not change "
            "the game rules, and players interpret ambiguous terms."
            + (
                " Use handledMessages with messages[].body.message IDs, never event id numbers, for messages "
                "you have fully considered and responded to or acted on as needed. "
                "After successful gameplay, those messages need no separate response session. Reading alone "
                "does not mark them handled."
                if routing_enabled and purpose == "gameplay"
                else ""
            ),
        )
        add(
            "chat_history",
            "Find earlier permitted messages and commitments. Returns the most recent 20 matches; narrow query for older evidence.",
        )
        add(
            "chat_send",
            "Send a message to chatRecipients from chat_read. Use to for one player or recipients for a private "
            "group, excluding yourself; omit both for public chat. To reply to everyone, copy the participants "
            "except yourself. Each participant group has its own history; order does not matter. Private messages "
            "notify everyone receiving them. Public messages notify only mentioned players; mentions must belong "
            "to the audience. Never combine recipients with to or nativeAction. Text alone has no game effects; "
            "use trade tools for binding trades or peace. "
            + "delivery=auto returns sent, queued or omitted with a deliveryId. Only a sent receipt proves delivery. "
            "Use delivery=immediate to send now, or chat_delivery to send or cancel the original draft. "
            + (
                "During your active turn, nativeAction may DENOUNCE or ISSUE_DEMAND against a civilization "
                "(selection=type from native.availableDemandsToSend), or RESPOND_DIPLOMACY using your alert handle "
                "and an offered selection. Read player_turn first. Available demands are ones you may send. "
                "Before replying, read nativeDiplomacy's sender, event, options, declaresWar and consequences. "
                "Built-in AI players understand these game requests, not ordinary chat."
                if purpose == "gameplay"
                else ""
            ),
        )
        if routing_enabled:
            add(
                "chat_delivery",
                "Check your deliveryId, send its original message immediately, or cancel it before sending. "
                "Repeating immediate does not send a second copy. Delivery requires disposition=sent and a receipt.",
            )
        if native_advice_enabled and purpose == "gameplay":
            add(
                "native_advice",
                "Read suggested next actions. Optional cities/units select your owned handles. Check costs, "
                "requirements and limitations. Suggestions sharing a unit, city, destination or budget may be "
                "alternatives; reconsider after each action. Reading advice takes no actions and predicts "
                "neither discoveries nor combat results.",
            )
        add(
            "commitment_offer",
            "Propose exact social terms. This is not an engine trade or peace treaty. Everyone must explicitly consent. Social promises, including kind=peace, record stated intentions and never restrict legal game actions or impose penalties. Terms and conditions may be ambiguous; players can negotiate or disagree about their meaning. conditions describes when the promise applies; use 'Unconditional' if applicable.",
        )
        add(
            "commitment_decide",
            "Accept terms you read with chat_read, withdraw consent, or voluntarily record that you reconsidered a promise. No decision here is required before a legal game action. Changing terms requires a new offer and consent. No engine action is implicit.",
        )
        add(
            "memory_read",
            "Read private records by ID; use includeRetired for archived entries. Observations record earlier "
            "facts; statements and notes contain assessments. After your turn ends, information is limited to "
            "that completed turn.",
        )
        add(
            "memory_search",
            "Search private records by type, subject, state or text, 20 per page. Results are active by default; "
            "history=true includes earlier versions as historical evidence. Use nextOffset for another page.",
        )
        add(
            "memory_update",
            (
                "Create one city_plan per owned city with its exact city handle. Read an existing plan and use "
                "amend to change its content; do not create another plan for the same city. Keep inherited choices "
                "and unknown reasons explicit within focus, growth, citizens, specialists, construction or purchases. "
                "You may edit cities owned by economy; respect main's city overrides. "
                if purpose == "economy"
                else "Use create to leave private notes for your next turn. Existing notebook entries are read-only "
                "during this exchange. "
                if purpose != "gameplay"
                else "Create, replace or retire at most one active strategy and one handoff. Read before replacing either; supply the "
                "whole entry, since omitted fields are not kept. Handoffs appear next turn only; strategy dates "
                "from when you wrote it. Use amend for changed note or belief fields: omitted fields stay and "
                "lists replace earlier lists. Contributions from conversations cannot be edited; use them to "
                "update your notebook, then retire them when handled. "
            )
            + "Save useful knowledge, reasoning and uncertainty; receipts already keep routine facts. Citations "
            "are optional, up to 20 per list. Unavailable sources produce warnings and remain unverified. "
            "needsReview/reviewSources are reminders, not restrictions on actions or required edits. Retiring an "
            "entry preserves its history and does not cancel a promise.",
        )
        return tools

    async def read_social(
        self, job: JobAccess, delivered: bool = True, *, include_context: bool = False, include_history: bool = False
    ):
        result = await job.connection.tool("player_communication", {"op": "state", "epoch": job.epoch})
        if "error" in result:
            raise StateConflict("COMMUNICATION_UNAVAILABLE")
        self.check(job)
        result = social_context(
            result,
            include_context=include_context,
            include_history=include_history,
            include_native_trades=job.purpose == "gameplay",
        )
        version = await self.io.call(self.memory.observe, "communication", result, job.id if delivered else None)
        job.memory["records"]["observation:communication"] = {
            "version": version,
            "category": "observation",
            "body": native_observation(result),
        }
        job.social_read = delivered or (job.social_read and result == job.social)
        job.social = result
        return result

    @staticmethod
    def register_external_read(job: JobAccess, name: str, arguments: JsonObject, value: JsonObject) -> JsonObject:
        query = DetailQuery.model_validate({"tool": name, "arguments": arguments})
        if query in job.pending_invalidated:
            job.pending_invalidated.remove(query)
        if query not in job.details_read:
            job.details_read.append(query)
        return {**value, "query": query.model_dump()}

    async def conversation_update(self, job, arguments):
        unseen = await self.io.call(self.memory.outgoing_messages, job.outgoing_start)
        fresh = [
            row["message"]
            for row in unseen
            if row["job"] != job.id
            and row["message"]["message"] not in job.seen_messages
            and self.same_conversation(arguments, row["message"])
        ]
        if not fresh:
            return None
        job.seen_messages.update(message["message"] for message in fresh)
        return {
            "error": "CONVERSATION_UPDATED",
            "applied": False,
            "recoverable": True,
            "messages": fresh,
            "instruction": "You already sent these messages from another conversation view. Reconsider whether this new message is still needed. No additional read is required.",
        }

    async def invoke(self, job: JobAccess, name: str, arguments: JsonObject, call_id: str) -> JsonObject:
        """Accepted MCP calls belong to the job, even if its model disconnects.

        Interruption can cancel an entire parallel code-mode batch. Retain every
        accepted call so the controller can deliver its result when it resumes.
        """
        self.check(job, ended_allowed=True)
        if job.negotiation_pending.is_set() or job.role_wait_pending.is_set():
            return {"error": "GAMEPLAY_SUSPENDED", "applied": False}
        task = asyncio.create_task(self.call(job, name, arguments, call_id))
        job.pending_calls[task] = {"tool": name, "arguments": copy.deepcopy(arguments)}

        def finished(task):
            job.pending_calls.pop(task, None)
            # The HTTP caller may have gone away; the controller still observes
            # failures through negotiation_calls or the gateway's durable fence.
            if not task.cancelled():
                task.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def call(
        self, job: JobAccess, name: str, arguments: JsonObject, call_id: str, *, source: str = "tool"
    ) -> JsonObject:
        async with self.admit(review=job.review_inputs is not None):
            return await self._admitted_call(job, name, arguments, call_id, source=source)

    async def fresh_turn(self, job: JobAccess, arguments: JsonObject, reason: str) -> JsonObject:
        """Give each new host observation its own journal identity.

        Usage counters and handoff passes can remain unchanged across several
        refreshes. Reusing their IDs either conflicts with new arguments or
        silently replays an old position. This helper is restricted to a read;
        actions must retain their existing durable identities for reconciliation.
        """
        return await self.call(
            job, "player_turn", arguments, "host:" + reason + ":" + secrets.token_hex(16), source="host"
        )

    @staticmethod
    def reset_coordination(job: JobAccess) -> None:
        job.coordination_scope = secrets.token_hex(12)
        job.coordination_seen = None
        job.coordination_sequence = 0
        job.coordination_pending = None
        job.detail_seen.clear()
        job.detail_pending.clear()
        job.memory_seen.clear()
        job.memory_pending.clear()
        job.memory_read_seen.clear()
        job.memory_read_pending.clear()
        job.payload_sequence = 0
        job.compact_overviews = False
        job.resume_delta_available = False

    @staticmethod
    def accept_payloads(job: JobAccess, name: str, arguments: JsonObject, result: JsonObject, *, replayed=False):
        result = expand_response(result)
        detail = result.get("detailUpdate")
        if detail is not None and detail["scopeId"] == job.coordination_scope:
            key = detail_scope(DetailQuery.model_validate(result["query"]))
            cache = job.detail_pending if job.coordination_preparing else job.detail_seen
            try:
                cache[key] = apply_detail_response(cache, result)
                job.payload_sequence = max(job.payload_sequence, detail["sequence"])
            except ValueError:
                cache.pop(key, None)
        cache = job.memory_pending if job.coordination_preparing else job.memory_seen
        read_cache = job.memory_read_pending if job.coordination_preparing else job.memory_read_seen
        delivery = result.get("memoryDelivery")
        if name in {"memory_read", "memory_search"} and delivery is not None:
            if delivery["scopeId"] == job.coordination_scope:
                try:
                    _, updated = apply_memory_delivery(
                        read_cache, result, scope=job.coordination_scope, minimum_versions=job.memory_versions
                    )
                except ValueError:
                    for item in delivery.get("items", []):
                        read_cache.pop(item["id"], None)
                else:
                    read_cache.clear()
                    read_cache.update(updated)
                    for item in delivery["items"]:
                        key = item["id"]
                        if key in updated:
                            cache[key] = copy.deepcopy(updated[key]["value"])
                            job.memory_versions[key] = max(job.memory_versions.get(key, 0), updated[key]["version"])
                    job.payload_sequence = max(job.payload_sequence, delivery["sequence"])
        elif name in {"memory_read", "memory_search"} and not arguments.get("history"):
            for record in result.get("records", []):
                if not record.get("historical") and (not replayed or record["id"] not in cache):
                    cache[record["id"]] = copy.deepcopy(record)
        if name == "memory_update" and result.get("recordScope") == job.coordination_scope:
            for request, change in zip(arguments.get("changes", []), result.get("changes", []), strict=True):
                if not change.get("accepted") or change.get("version", 0) <= job.memory_versions.get(change["id"], 0):
                    continue
                read_cache.pop(change["id"], None)
                try:
                    expanded = apply_memory_receipt([request], {"changes": [change]}, cache)["changes"][0]
                except ValueError:
                    cache.pop(change["id"], None)
                    continue
                if isinstance(expanded.get("record"), dict):
                    cache[change["id"]] = expanded["record"]
                    job.memory_versions[change["id"]] = change["version"]
        for change in result.get("memoryUpdates", []):
            record = change["record"]
            if change["version"] > job.memory_versions.get(record["id"], 0):
                read_cache.pop(record["id"], None)
                cache[record["id"]] = copy.deepcopy(record)
                job.memory_versions[record["id"]] = change["version"]

    def coordination_delivery(self, job: JobAccess, *, full: bool = False) -> JsonObject:
        if self.role_coordinator is None or job.coordination_preparing:
            return {}
        from civ_agent.coordination_context import working_context

        current = coordination_snapshot(
            working_context(self.role_coordinator.context(), job.purpose),
            job.id,
            job.epoch,
            job.coordination_scope,
            job.coordination_sequence + 1,
        )
        return coordination_response(job.coordination_seen, current, full=full)

    @staticmethod
    def accept_coordination(job: JobAccess, result: JsonObject) -> None:
        delivery = result.get("economyCoordination", result.get("coordinationUpdate"))
        if delivery is None or delivery.get("scopeId") != job.coordination_scope:
            return
        try:
            current = apply_coordination_update(job.coordination_seen, result)
        except ValueError:
            job.coordination_seen = None
            return  # A subsequent call sends a full snapshot; receipts remain replayable.
        if current is not None:
            job.coordination_seen = current
            job.coordination_sequence = current["sequence"]
            job.economy_seen = coordination_content(current)
            if "warState" in current:
                job.war_versions.update(goal_versions(current["warState"]))

    def commit_startup_coordination(self, job: JobAccess) -> None:
        job.compact_overviews = True
        job.resume_delta_available = job.delivered_overview is not None
        if job.coordination_pending is not None:
            self.accept_coordination(job, {"economyCoordination": job.coordination_pending})
            job.coordination_pending = None
        job.detail_seen.update(job.detail_pending)
        job.detail_pending.clear()
        job.memory_seen.update(job.memory_pending)
        job.memory_pending.clear()
        job.memory_read_seen.update(job.memory_read_pending)
        job.memory_read_pending.clear()

    def startup_memory_context(self, job: JobAccess) -> str:
        """Seed exactly the full records in the eventual startup message."""
        selected = {key: job.memory["records"][key] for key in job.memory["contextIds"]}
        if not selected:
            return self.memory.context(job.memory)
        response = memory_response(
            {"records": [public_record(key, record) for key, record in selected.items()]},
            {key: record["version"] for key, record in selected.items()},
            {},
            job.coordination_scope,
            job.payload_sequence + 1,
            full=True,
        )
        _, pending = apply_memory_delivery({}, response, scope=job.coordination_scope)
        job.memory_read_pending = pending
        job.memory_pending = {key: copy.deepcopy(item["value"]) for key, item in pending.items()}
        for key, item in pending.items():
            job.memory_versions[key] = max(job.memory_versions.get(key, 0), item["version"])
        job.payload_sequence = response["memoryDelivery"]["sequence"]
        return self.memory.context(job.memory, response["memoryDelivery"])

    async def _admitted_call(
        self, job: JobAccess, name: str, arguments: JsonObject, call_id: str, *, source: str
    ) -> JsonObject:
        accounting = self.accounting
        counted = (
            await self.io.call(
                accounting.begin_call, job.id, name, arguments, source="internal" if source == "host" else source
            )
            if accounting
            else None
        )
        outcome, replayed = "incomplete", False
        sizes = None
        ended = False
        try:
            result, replayed = await self._call_once(
                job, name, arguments, call_id, startup=source == "startup", host_handoff=source == "host"
            )
            if source == "host":
                # Controller reads are not model messages and can advance delivery
                # cursors. A subsequent resume must re-establish a full baseline.
                job.resume_delta_available = False
            elif source == "tool" and not replayed and name == "player_turn" and full_delivery(arguments):
                job.resume_delta_available = "stateId" in result
            outcome = "error" if "error" in result else "success"
            if result.get("error") == "STATE_UPDATE_FAILED":
                await self.sync_failure()
            if (
                self.history is not None
                and not replayed
                and name == "player_command"
                and outcome == "success"
                and result.get("kind") == arguments.get("kind")
                and isinstance(result.get("kind"), str)
            ):
                kind = result["kind"]
                description = kind.replace("_", " ").capitalize() + " confirmed."
                actions = (job.observation or {}).get("turnReview", {}).get("actions", [])
                if kind != "END_TURN" and actions and actions[-1].get("kind") == kind:
                    description = actions[-1].get("description") or description
                await self.io.call(self.history.action, job.id, call_id, kind, description)
            ended = result.get("stateUpdate", {}).get("kind") == "turn_ended"
            if ended:
                job.gameplay_fenced = True
            visible = result if name == "native_advice" else public_result(result)
            if name == "history_read" and "review" in result:
                visible["review"] = copy.deepcopy(result["review"])
            if source == "tool":
                sizes = {**payload_characters(visible), "replayedResponses": int(replayed)}
            if name == "native_advice":
                # Advice explicitly names its observation epoch/revision. These
                # are evidence, not authority to bypass command validation.
                return result
            return visible
        except asyncio.CancelledError:
            self.forget_observation(job)
            await self.sync_failure()
            raise
        except (OSError, ValueError, KeyError, TypeError, SQLAlchemyError):
            outcome = "error"
            # A memory batch may have durably failed before its export/response
            # failed too. Its terminal stop must still reach the lifecycle owner.
            await self.sync_failure()
            raise
        finally:
            if accounting and counted:
                await self.io.call(accounting.end_call, counted, outcome, replayed, sizes)
            if ended:
                # The native receipt, tool response and accounting are durable.
                # Fence further calls before notifying the lifecycle controller.
                if job.handoff_at is None:
                    job.handoff_at = time.monotonic()
                job.gameplay_ended.set()

    async def _call_once(
        self,
        job: JobAccess,
        name: str,
        arguments: JsonObject,
        call_id: str,
        *,
        startup: bool = False,
        host_handoff: bool = False,
    ) -> tuple[JsonObject, bool]:
        self.check(job, ended_allowed=True)
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise ValueError("INVALID_ARGUMENT")
        if name == "inspect_bundle":
            if job.review_inputs is None and job.purpose not in {"gameplay", "economy", "military"}:
                return {"error": "TOOL_NOT_PERMITTED", "applied": False}, False
            return await run_bundle(self, job, arguments, call_id)
        if job.review_inputs is not None:
            if job.review_evidence is None:
                raise ValueError("REVIEW_EVIDENCE_REQUIRED")
            value = public_result(frozen_read(job.review_inputs, name, arguments))
            if "error" in value:
                return value, False
            return job.review_evidence.deliver(name, value, arguments), False
        role_lock = (
            self.role_dispatch
            if self.role_coordinator is not None
            and name
            in {
                "player_command",
                "finish_role",
                "military_review",
                "economy_wait",
                "city_override",
                "economy_request",
                "memory_update",
                "movement_preview",
                "mission_preview",
                "player_inspect",
            }
            | ROLE_TOOLS
            | DETAIL_MODELS.keys()
            else nullcontext()
        )
        async with job.lock:
            self.check(job, ended_allowed=True)
            previous = await self.io.call(self.memory.call, job.id, call_id, name, arguments)
            if previous is not None:
                self.accept_coordination(job, previous)
                self.accept_payloads(job, name, arguments, previous, replayed=True)
                if self.role_coordinator is not None and name == "economy_wait":
                    self.role_coordinator.arm_economy_wait(job, previous, replayed=True)
                return previous, True
            # The action receipt may be durable even if a barrier interrupted
            # saving the MCP response. Replay it without needing a new observation.
            receipt = await self.io.call(self.memory.call_receipt, job.id, call_id)
            self.check(
                job,
                ended_allowed=(
                    receipt is not None and name == "player_command" and arguments.get("kind") == "END_TURN"
                ),
            )
            advice_revision = (job.observation or {}).get("revision")
            if receipt is not None:
                result = copy.deepcopy(receipt)
            else:
                finishing = name == "finish_role" or (
                    name == "player_command" and arguments.get("kind") == "END_TURN" and not host_handoff
                )
                waited = self.role_coordinator is not None and finishing and await self.wait_for_gameplay(job)
                if waited:
                    self.forget_observation(job)
                    result = self.reobserve_after_negotiation()
                else:
                    async with role_lock:
                        refresh = {}
                        result = await self._call(
                            job, name, arguments, call_id, startup=startup, host_handoff=host_handoff
                        )
                        if job.role_refresh_required and result.get("error") == "OBSERVATION_REQUIRED":
                            refresh = await self.refresh_role_observation(job, call_id)
                            if refresh:
                                if name in DETAIL_MODELS.keys() | {
                                    "movement_preview",
                                    "mission_preview",
                                    "player_inspect",
                                }:
                                    result = await self._call(job, name, arguments, call_id, startup=startup)
                                else:
                                    result["recovery"] = {
                                        "instruction": "Another role changed the position. Use the returned stateUpdate, "
                                        "reconsider your choice, then submit a new call. No additional read is required."
                                    }
                        result = {**result, **refresh}
            result = await self.settle_gameplay(job, name, result)
            if self.native_advice_enabled:
                if name == "native_advice":
                    await self.io.call(experiment_metrics.exposure, self.memory, job, call_id, result, startup)
                elif name == "player_command":
                    await self.io.call(
                        experiment_metrics.action_match, self.memory, job, call_id, arguments, result, advice_revision
                    )
            async with self.role_dispatch if self.role_coordinator is not None else nullcontext():
                if (
                    self.role_coordinator is not None
                    and name == "player_turn"
                    and job.purpose in {"gameplay", "economy", "military"}
                    and "error" not in result
                    and job.observation is not None
                ):
                    # Another player's peace can change diplomacy without one
                    # of this role's actions. Reduce the fresh accepted view
                    # before a later finish checks the cached war episodes.
                    await self.role_coordinator.synchronize_war(job.observation)
                if (
                    self.role_coordinator is not None
                    and self.role_coordinator.economy_schedule is not None
                    and job.purpose == "economy"
                    and name == "player_command"
                    and "error" not in result
                ):
                    from civ_agent.economy_roles import CITY_COMMANDS

                    if arguments.get("kind") in CITY_COMMANDS and arguments.get("target"):
                        self.role_coordinator.extend_economy_scope([arguments["target"]])
                effect_revision = result.get("revision")
                result = await self.update_after_action(job, name, arguments, call_id, result)
                if (
                    self.role_coordinator is not None
                    and (self.changes_game(name, result) or name in {"city_override", "escort_assignment"})
                    and "error" not in result
                ):
                    if self.changes_game(name, result):
                        await self.role_coordinator.after_action(job, arguments, result)
                    for sibling in self.role_coordinator.jobs.values():
                        if sibling is not job:
                            self.invalidate_role_observation(
                                sibling,
                                provenance={
                                    "epoch": job.epoch,
                                    "role": job.purpose,
                                    "tool": name,
                                    "operationId": host_request_id(job.id, call_id),
                                    "fromRevision": advice_revision,
                                    "toRevision": effect_revision,
                                }
                                if name == "player_command"
                                else None,
                            )
                            if name in {"city_override", "escort_assignment"}:
                                # Ownership labels are outside native detail projections.
                                sibling.pending_invalidated.extend(
                                    query for query in sibling.details_read if query not in sibling.pending_invalidated
                                )
                if (
                    self.role_coordinator is not None
                    and job.purpose in {"gameplay", "economy", "military"}
                    and name not in {"player_capabilities", "native_advice"}
                ):
                    # Native receipts/overviews already committed by deliver_turn
                    # carry their own delivery. Other replies attach only changes.
                    if "economyCoordination" not in result and "coordinationUpdate" not in result:
                        result = {**result, **self.coordination_delivery(job)}
                if self.role_coordinator is not None and name in DETAIL_MODELS.keys() | READ_TOOLS:
                    # Overview labels were committed with their state ID already.
                    if name != "player_turn":
                        result = label_commands(result, self.role_coordinator, job.purpose, job.observation)
                if name in DETAIL_MODELS and isinstance(result.get("data"), dict) and "query" in result:
                    key = detail_scope(DetailQuery.model_validate(result["query"]))
                    result = detail_response(
                        job.detail_seen.get(key),
                        result,
                        job.coordination_scope,
                        job.payload_sequence + 1,
                        key,
                        full=startup or full_delivery(arguments),
                        baselines=job.detail_seen,
                    )
                result = compact_response(result, full=startup or full_delivery(arguments), share=job.compact_overviews)
                result = await self.io.call(self.memory.call, job.id, call_id, name, arguments, result)
                assert result is not None
                self.accept_coordination(job, result)
                self.accept_payloads(job, name, arguments, result)
                if self.role_coordinator is not None and name == "economy_wait":
                    self.role_coordinator.arm_economy_wait(job, result)
                return result, receipt is not None

    @staticmethod
    def forget_observation(job: JobAccess) -> None:
        job.pending_invalidated.extend(query for query in job.details_read if query not in job.pending_invalidated)
        job.observation = None
        job.prior_role_observation = None
        job.delivered_overview = None
        job.detail_revision = None
        job.details_read.clear()
        job.role_refresh_required = False
        job.resume_delta_available = False

    async def apply_host_effect(
        self,
        job: JobAccess,
        prepared: PreparedHostEffect,
        call_id: str,
        raw_receipt: JsonObject,
        *,
        replayed: bool = False,
    ) -> JsonObject:
        """Apply a confirmed host result without another effect or response journal.

        The native durable operation retains the exact host receipt. Domain
        reducers may run again after a lost acknowledgement; their effect key
        comes from the frozen request, never from a fresh dispatch.
        """
        self.check(job, ended_allowed=True)
        name, arguments = prepared.name, copy.deepcopy(prepared.arguments)
        result = copy.deepcopy(raw_receipt)
        if prepared.result is not None or prepared.payload is None:
            raise ValueError("HOST_EFFECT_NOT_PREPARED")
        if self.native_advice_enabled and name == "player_command":
            await self.io.call(
                experiment_metrics.action_match,
                self.memory,
                job,
                call_id,
                arguments,
                result,
                prepared.payload.get("revision"),
            )
        if (
            name in {"trade_offer", "trade_decide", "friendship_offer"}
            and "error" not in result
            and result.get("trade")
        ):
            for proposal in (
                arguments.get("proposalId"),
                arguments.get("counterofferTo"),
                result["trade"].get("proposal"),
            ):
                if proposal:
                    job.trades.pop(proposal, None)
        if (
            (
                prepared.transport == "player_command"
                or name == "trade_decide"
                and result.get("outcome")
                or result.get("nativeOutcome")
            )
            and "error" not in result
            and job.purpose != "gameplay"
        ):
            self.invalidate_role_observation(job)
        coordinator = self.role_coordinator
        if (
            coordinator is not None
            and coordinator.economy_schedule is not None
            and job.purpose == "economy"
            and name == "player_command"
            and "error" not in result
        ):
            from civ_agent.economy_roles import CITY_COMMANDS

            if arguments.get("kind") in CITY_COMMANDS and arguments.get("target"):
                coordinator.extend_economy_scope([arguments["target"]])
        result = await self.update_after_action(job, name, arguments, call_id, result, record_call=False)
        if coordinator is not None and self.changes_game(name, result) and "error" not in result:
            await coordinator.after_action(job, arguments, result, effect_id=prepared.payload["requestId"])
            for sibling in coordinator.jobs.values():
                if sibling is not job:
                    self.invalidate_role_observation(
                        sibling,
                        provenance={
                            "epoch": job.epoch,
                            "role": job.purpose,
                            "tool": name,
                            "operationId": prepared.payload["requestId"],
                            "fromRevision": prepared.payload.get("revision"),
                            "toRevision": raw_receipt.get("revision"),
                        }
                        if prepared.transport == "player_command"
                        else None,
                    )
        if coordinator is not None and job.purpose in {"gameplay", "economy", "military"}:
            if "economyCoordination" not in result and "coordinationUpdate" not in result:
                result = {**result, **self.coordination_delivery(job)}
            await coordinator.save()
        result = compact_response(result, full=full_delivery(arguments), share=job.compact_overviews)
        self.accept_coordination(job, result)
        self.accept_payloads(job, name, arguments, result, replayed=replayed)
        return result

    @staticmethod
    def invalidate_role_observation(job: JobAccess, *, provenance=None) -> None:
        """Fence decisions, retaining delivered facts for selective revalidation."""
        record_invalidation(job, provenance)
        if job.observation is not None:
            job.prior_role_observation = job.observation
        job.role_refresh_required = job.prior_role_observation is not None
        job.observation = None
        job.detail_revision = None

    @staticmethod
    def resume_delivery(job: JobAccess) -> str:
        return (
            "auto"
            if job.compact_overviews and job.resume_delta_available and job.delivered_overview is not None
            else "full"
        )

    async def refresh_role_observation(self, job: JobAccess, call_id: str) -> JsonObject:
        """Deliver a sibling's changes with the next read, without retrying a choice."""
        value = await job.connection.tool("player_turn", {})
        self.check(job)
        if (
            "error" in value
            or value.get("phase") != "ACTIVE"
            or value.get("epoch") != job.epoch
            or value.get("turn") != job.memory.get("gameTurn")
            or (value.get("negotiation") or {}).get("active")
        ):
            return {}
        return await self.deliver_turn(job, value, "player_turn", {}, "role-refresh:" + call_id, {})

    @staticmethod
    def changes_game(name, result):
        return not result.get("roleFinished") and (
            name == "player_command"
            or bool(result.get("nativeOutcome"))
            or (name in {"trade_offer", "friendship_offer", "trade_decide"} and bool(result.get("trade")))
        )

    async def deliver_turn(
        self,
        job: JobAccess,
        value: JsonObject,
        name: str,
        arguments: JsonObject,
        call_id: str,
        receipt: JsonObject | None = None,
        *,
        force_full: bool = False,
        record_call: bool = True,
    ) -> JsonObject:
        """Register only the reconstructed overview; detail contents stay opt-in."""
        self.check(job)
        revision = value.get("revision")
        if not isinstance(revision, str) or value.get("epoch") != job.epoch:
            raise StateConflict("INVALID_HOST_REVISION")
        previous, baseline = job.observation or job.prior_role_observation, job.delivered_overview
        value = {**value, "communication": await self.read_social(job, delivered=False)}
        add_combat_commands(value)
        if "map" in value and "combatReports" in value:
            value = {"tacticalReview": tactical_review(value).model_dump(exclude_none=True), **value}
        compact = public_result(overview(value))
        compact = role_overview(
            compact, job.overview_view, roles=self.role_coordinator, purpose=job.purpose, social=job.social
        )
        advice = job.memory.get("strategicAdvice")
        if brief := review_brief(
            {**advice, "_contextFacts": job.memory.get("reviewFacts")} if advice else None, value, job.purpose
        ):
            compact["strategicReview"] = brief
        if self.role_coordinator is not None:
            compact = label_commands(compact, self.role_coordinator, job.purpose, value)
            if job.purpose == "economy":
                from civ_agent.economy_completion import worker_completion

                compact["roleCompletion"] = {
                    "tool": "finish_role",
                    **worker_completion(value, self.role_coordinator.war_state),
                }
            if job.purpose == "military":
                from civ_agent.military_completion import completion_review

                check = completion_review(value, self.role_coordinator.war_state)
                compact["militaryCompletion"] = {
                    "remainingTargets": len(check["items"]),
                    "read": {"tool": "military_review", "arguments": {}},
                }
        if job.city_scope is not None:
            compact["cityScope"] = {
                "included": list(job.city_scope),
                "ownedCities": [{"handle": city["handle"], "name": city["name"]} for city in compact.get("cities", [])],
            }
            compact["cities"] = [city for city in compact.get("cities", []) if city["handle"] in job.city_scope]
        compact.pop("strategyReview", None)
        compact.pop("stateId", None)
        compact_read = (
            receipt is None
            and job.compact_overviews
            and not job.coordination_preparing
            and not force_full
            and not full_delivery(arguments)
        )
        delivery = self.coordination_delivery(job, full=receipt is None and not compact_read)
        invalidated = list(job.pending_invalidated)
        invalidated.extend(
            query for query in changed_details(previous, value, job.details_read) if query not in invalidated
        )

        def respond(records, changed):
            presented = copy.deepcopy(compact)
            if isinstance(presented.get("decisionFeedback"), dict):
                presented["decisionFeedback"] = compact_feedback(presented["decisionFeedback"])
            current = stamp(
                {**presented, "strategyReview": strategy_review(records).model_dump()}, job.id, job.epoch, revision
            )
            result = (
                {**current, **({"invalidatedDetails": [q.model_dump() for q in invalidated]} if invalidated else {})}
                if receipt is None and not compact_read
                else {
                    **(receipt or {}),
                    "stateUpdate": overview_update(
                        baseline, current, invalidated, compact_nested=True, allow_unchanged=compact_read
                    ),
                }
            )
            if compact_read:
                complete = {
                    **current,
                    **({"invalidatedDetails": [q.model_dump() for q in invalidated]} if invalidated else {}),
                }
                if len(json.dumps(result, separators=(",", ":"))) >= len(json.dumps(complete, separators=(",", ":"))):
                    result = complete
            result.update(copy.deepcopy(delivery))
            if changed:
                result["memoryUpdates"] = [
                    {"version": record["version"], "record": public_record(key, record)}
                    for key, record in sorted(changed.items())
                ]
            return compact_response(
                result,
                full=full_delivery(arguments) or call_id.removeprefix("review:").startswith("startup:"),
                share=job.compact_overviews,
            )

        try:
            observed, result = await self.io.call(
                self.memory.deliver_player,
                value,
                job.id,
                compact,
                call_id,
                name,
                arguments,
                respond,
                not call_id.removeprefix("review:").startswith("startup:"),
                record_call=record_call,
            )
        except BaseException:
            # A cancelled I/O await may still have committed the response. Replay
            # it from SQLite; never reuse a possibly older in-memory observation.
            self.forget_observation(job)
            raise
        job.memory["records"].update(observed)
        job.observation = value
        job.prior_role_observation = None
        job.role_refresh_required = False
        if self.role_coordinator is not None and job.purpose == "economy":
            self.role_coordinator.economy_observation = {
                **value,
                "cities": [
                    city
                    for city in value.get("cities", [])
                    if job.city_scope is None or city["handle"] in job.city_scope
                ],
            }
        if self.role_coordinator is not None and job.purpose == "military":
            self.role_coordinator.military_observation = copy.deepcopy(value)
            self.role_coordinator.military_observation_state = fingerprint(self.role_coordinator.war_state)
        self.accept_coordination(job, result)
        expanded = expand_response(result)
        job.delivered_overview = (
            {
                key: part
                for key, part in expanded.items()
                if key not in {"memoryUpdates", "invalidatedDetails", "economyCoordination", "coordinationUpdate"}
            }
            if "stateUpdate" not in expanded
            else apply_overview_update(baseline, expanded["stateUpdate"])
        )
        job.details_read = [query for query in job.details_read if query not in invalidated]
        job.pending_invalidated.clear()
        # Availability can change with the board, but the terms this job read
        # remain its selected version. The engine checks that version and current
        # legality when a decision arrives; another role's move cannot erase it.
        job.detail_revision = revision
        return result

    async def refresh_economy_request(self, job, arguments, call_id):
        """Let a sender reconsider stale production requests before publishing them."""
        from civ_agent.economy_schedule import request_city

        coordinator = self.role_coordinator
        if (
            coordinator is None
            or arguments.get("priority", "routine") != "urgent"
            or coordinator.jobs.get(job.purpose) is not job
            or job.purpose not in {"gameplay", "military"}
            or coordinator.ready.get(job.purpose)
        ):
            return None
        if any(
            request["status"] == "pending"
            and request.get("city") == arguments.get("city")
            and request["text"] == arguments["text"]
            and request.get("priority", "routine") == arguments.get("priority", "routine")
            for request in coordinator.requests
        ):
            return None
        value = await job.connection.tool("player_turn", {})
        self.check(job)
        if (
            "error" in value
            or value.get("phase") != "ACTIVE"
            or value.get("epoch") != job.epoch
            or value.get("turn") != job.memory.get("gameTurn")
            or (value.get("negotiation") or {}).get("active")
        ):
            return self.reobserve_after_negotiation()
        current = public_result(overview(value))
        target = request_city(value, arguments.get("city"))

        def queues(view):
            return {
                city["handle"]: city.get("constructionQueue", [])
                for city in view.get("cities", [])
                if target is None or city["handle"] == target
            }

        previous = job.delivered_overview or {}
        seen = {r["id"]: r for r in (job.economy_seen or {}).get("requests", [])}
        changed_dispositions = any(
            request["status"] != "pending"
            and request != seen.get(request["id"])
            and (target is None or request.get("city") is None or request_city(value, request.get("city")) == target)
            for request in coordinator.requests
        )
        if job.observation is not None and queues(previous) == queues(current) and not changed_dispositions:
            return None
        return await self.deliver_turn(
            job,
            value,
            "economy_request",
            arguments,
            call_id,
            {
                "error": "ECONOMY_CONTEXT_CHANGED",
                "applied": False,
                "recoverable": True,
                "recovery": {
                    "instruction": "Economy changed since your last delivered view. Apply stateUpdate and read the current request dispositions in economyCoordination. Reconsider whether this request is still needed; submit a new call if it is. No additional read is required."
                },
            },
        )

    async def update_after_action(
        self,
        job: JobAccess,
        name: str,
        arguments: JsonObject,
        call_id: str,
        result: JsonObject,
        *,
        record_call: bool = True,
    ) -> JsonObject:
        if "error" in result or result.get("roleFinished"):
            return result
        social_change = name in {"commitment_offer", "commitment_decide"}
        changes_game = self.changes_game(name, result)
        ownership_change = name in {"city_override", "escort_assignment"}
        if not changes_game and not social_change and not ownership_change:
            return result
        if ownership_change:
            job.pending_invalidated.extend(query for query in job.details_read if query not in job.pending_invalidated)
        if job.purpose not in {"gameplay", "economy", "military"}:
            self.forget_observation(job)
            invalidated = [query.model_dump() for query in job.pending_invalidated]
            job.pending_invalidated.clear()
            job.social_read = False
            return {**result, "invalidatedDetails": invalidated}
        if name == "player_command" and (arguments.get("kind") == "END_TURN" or result.get("isYourTurn") is False):
            self.forget_observation(job)
            return {
                **result,
                "stateUpdate": {
                    "kind": "turn_ended",
                    "instruction": "Your turn ended. Keep this receipt and stop issuing game commands.",
                },
            }
        try:
            value = await job.connection.tool("player_turn", {})
        except (OSError, TimeoutError, aiohttp.ClientError):
            self.forget_observation(job)
            self.check(job)
            return {**result, "stateUpdate": refresh_required()}
        except StateConflict as error:
            return await self.failed_update(job, call_id, result, error, record_call=record_call)
        except (ValueError, KeyError, TypeError) as error:
            return await self.failed_update(job, call_id, result, error, record_call=record_call)
        try:
            self.check(job)
            # Do not spend a confirmed command's response waiting for a new
            # negotiation or attach another turn's state to this action.
            if "error" in value or value.get("negotiation", {}).get("active"):
                self.forget_observation(job)
                return {**result, "stateUpdate": refresh_required()}
            if value.get("phase") == "WAITING":
                self.forget_observation(job)
                return {**result, "stateUpdate": {"kind": "turn_ended"}}
            if name == "player_command" and arguments.get("kind") == "SET_RESEARCH_QUEUE":
                result = {**result, "researchChange": research_change(job.observation, value)}
            if name == "player_command" and arguments.get("reviewAfter") and job.purpose == "military":
                from civ_agent.military_completion import completion_review, review_page

                coordinator = self.role_coordinator
                if coordinator is not None:
                    await coordinator.synchronize_war(value)
                    saved = (
                        await self.io.call(coordinator.military_schedule.saved)
                        if coordinator.military_schedule is not None
                        else {}
                    )
                    result = {
                        **result,
                        "militaryCompletion": review_page(completion_review(value, coordinator.war_state, saved)),
                    }
            if (
                name == "player_command"
                and arguments.get("kind") in {"MOVE_UNIT", "SET_UNIT_DESTINATION", "ATTACK", "REBASE_UNIT", "PARADROP"}
                and job.observation is not None
                and "tiles" in job.observation.get("map", {})
                and "tiles" in value.get("map", {})
            ):
                # Both maps came through the same player's visibility boundary.
                # Include zero: moving successfully need not reveal any terrain.
                before_tiles = {(t["position"]["x"], t["position"]["y"]) for t in job.observation["map"]["tiles"]}
                after_tiles = {(t["position"]["x"], t["position"]["y"]) for t in value["map"]["tiles"]}
                result = {
                    **result,
                    "exploration": {
                        "exploredTilesBefore": len(before_tiles),
                        "exploredTilesAfter": len(after_tiles),
                        "newlyExploredTiles": len(after_tiles - before_tiles),
                    },
                }
            return await self.deliver_turn(job, value, name, arguments, call_id, result, record_call=record_call)
        except (OSError, ValueError, KeyError, TypeError, SQLAlchemyError) as error:
            return await self.failed_update(job, call_id, result, error, record_call=record_call)

    async def failed_update(self, job, call_id, result, error, *, record_call=True):
        self.forget_observation(job)
        self.check(job)  # Paused/expired jobs must stop, retaining the raw receipt.
        if not record_call or isinstance(error, StateConflict) and str(error) == "HOST_UNAVAILABLE":
            # Both the player overview and its communication context require
            # host reads. A dropped read must preserve the confirmed action.
            return {**result, "stateUpdate": refresh_required()}
        failed = await self.io.call(
            self.memory.fail_run,
            job.id,
            call_id,
            "STATE_UPDATE_FAILED",
            {"exceptionType": type(error).__name__, "message": str(error)},
            {**result, "applied": True, "stateUpdate": refresh_required()},
        )
        await self.sync_failure()
        return failed

    @staticmethod
    def suspend_negotiation(job: JobAccess) -> None:
        # Capture accepted calls before waking the model controller. A queued
        # call still needs its settled result even if it has not reached the
        # native negotiation check when suspension starts.
        job.delivered_overview = None
        job.negotiation_calls.update(job.pending_calls)
        job.negotiation_pending.set()

    async def wait_for_gameplay(self, job: JobAccess) -> bool:
        """Long-poll in code, outside dispatch and the memory actor. Chat jobs remain runnable."""
        waited = job.negotiation_pending.is_set()
        # Consume only the notification already observed, before any await.
        # A later suspension must remain pending for the next refresh boundary.
        job.negotiation_pending.clear()
        job.wait_started = time.monotonic()
        try:
            while True:
                self.check(job)
                if not self.consultation_hold.is_set():
                    waited = True
                    await self.consultation_hold.wait()
                # The first read establishes whether old gameplay assumptions need refreshing.
                state = await job.connection.tool(
                    "player_communication",
                    {"op": "negotiation", "epoch": job.epoch, "waitSeconds": 25 if waited else 0},
                )
                self.check(job)
                active = state.get("negotiation", {}).get("active")
                if "error" in state or type(active) is not bool:
                    raise StateConflict("NEGOTIATION_STATUS_UNAVAILABLE")
                if not active:
                    return waited
                if self.role_coordinator is not None:
                    for sibling in self.role_coordinator.jobs.values():
                        if sibling is not job and not self.role_coordinator.ready[sibling.purpose]:
                            self.suspend_negotiation(sibling)
                if job.suspend_for_negotiation and job.pending_calls:
                    # An earlier action's response may have reached the code
                    # cell but never been printed before it was interrupted.
                    # Rebase the settled response instead of referring to an
                    # overview delta the model might not have received.
                    self.suspend_negotiation(job)
                waited = True
        except BaseException:
            # No refresh was delivered when polling failed or was cancelled.
            # Retain that obligation without clearing a newer notification.
            if waited:
                job.negotiation_pending.set()
            raise
        finally:
            job.completed_wait += time.monotonic() - job.wait_started
            job.wait_started = None

    async def settle_gameplay(self, job: JobAccess, name: str, result: JsonObject) -> JsonObject:
        if job.purpose not in {"gameplay", "economy", "military"}:
            return result
        if name in {"trade_offer", "friendship_offer"} and "error" not in result and result.get("trade"):
            if result["trade"].get("mode") == "NATIVE_AI":
                return result
            await self.wait_for_gameplay(job)
            # Return the settled canonical terms, even when this is recovery of a lost offer response.
            final = await self._call(job, "trade_read", {"proposalId": result["trade"]["proposal"]}, "settlement")
            return {
                **final,
                "outcome": final["trade"].get("outcome"),
            }
        error = result.get("error")
        if name == "player_command" and isinstance(error, dict) and error.get("code") == "NEGOTIATION_ACTIVE":
            await self.wait_for_gameplay(job)
            job.observation = None
            return self.reobserve_after_negotiation()
        return result

    @staticmethod
    def reobserve_after_negotiation(
        instruction: str = "The game state changed. Read player_turn and make a fresh decision.",
    ) -> JsonObject:
        return {
            "error": "OBSERVATION_REQUIRED",
            "applied": False,
            "recoverable": True,
            "recovery": {
                "reads": [{"tool": "player_turn", "arguments": {}}],
                "instruction": instruction,
            },
        }

    async def read_trade(self, job: JobAccess, proposal: str) -> JsonObject:
        value: JsonObject = await job.connection.tool(
            "player_communication", {"op": "trade_read", "epoch": job.epoch, "proposal": proposal}
        )
        self.check(job)
        if job.purpose == "communication":
            if "pendingTrades" in value:
                value = {**value, "pendingTrades": [t for t in value["pendingTrades"] if t["mode"] == "SHARED"]}
            if "error" not in value and value["trade"]["mode"] == "NATIVE_AI":
                value = {
                    **value,
                    "trade": {
                        **value["trade"],
                        "acceptAvailable": False,
                        "declineAvailable": False,
                        "withdrawAvailable": False,
                        "counterofferAvailable": False,
                        "unavailableReason": NATIVE_TRADE_GAMEPLAY_ONLY,
                    },
                }
        return value

    def same_conversation(self, arguments: JsonObject, message: JsonObject) -> bool:
        recipients = arguments.get("recipients")
        if recipients is None and arguments.get("to"):
            recipients = [arguments["to"]]
        if recipients is None:
            return message["public"]
        return not message["public"] and set(message["participants"]) == {self.memory.civilization, *recipients}

    async def chat_read(self, job: JobAccess, arguments: JsonObject) -> JsonObject:
        # Mark only messages actually returned; concurrent sends stay unseen.
        if arguments.get("handledMessages"):
            try:
                if (
                    job.delivered_messages is not None
                    and not set(arguments["handledMessages"]) <= job.delivered_messages
                ):
                    raise StateConflict("MESSAGES_NOT_DELIVERED_TO_JOB")
                await self.io.call(self.memory.acknowledge_messages, job.id, arguments["handledMessages"])
            except StateConflict as error:
                # These checks precede the atomic acknowledgment. They are
                # definite rejections, not uncertain transport failures.
                code = str(error)
                if code not in {"MESSAGES_NOT_DELIVERED_TO_JOB", "MESSAGE_ACKNOWLEDGEMENT_GAMEPLAY_ONLY"}:
                    raise
                return {
                    "error": code,
                    "applied": False,
                    "recoverable": True,
                    "instruction": (
                        "No messages were acknowledged. Use messages[].body.message IDs from a delivered "
                        "chat_read, not event id numbers. Read chat_read with handledMessages=[] if needed, "
                        "then acknowledge only messages you have handled."
                        if code == "MESSAGES_NOT_DELIVERED_TO_JOB"
                        else "Message acknowledgment is available only during routed gameplay. "
                        "Read chat_read without handledMessages."
                    ),
                }
        social = await self.read_social(
            job,
            include_context=arguments.get("includeContext", False),
            include_history=arguments.get("includeHistory", False),
        )
        recent = await self.io.call(
            self.memory.snapshot,
            job.purpose,
            event_ids=job.memory.get("communicationEventIds") if job.purpose == "communication" else None,
        )
        events = recent["events"]
        await self.io.call(
            self.memory.refresh_memory,
            job.id,
            ["event:" + str(event["id"]) for event in events],
            recent["cursor"],
            routing_ids=recent.get("routingEventIds", []) if job.purpose == "communication" else [],
        )
        delivered = {event["body"]["message"] for event in events if event["kind"] == "message"}
        outgoing = await self.io.call(self.memory.outgoing_messages, limit=20, echo_cursor=recent["cursor"])
        messages = [
            row["message"] for row in outgoing if row["message"]["message"] not in delivered | job.seen_messages
        ]
        job.seen_messages.update(delivered)
        job.seen_messages.update(message["message"] for message in messages)
        context = await self.io.call(
            self.memory.conversation_context, [e["body"] for e in events if e["kind"] == "message"] + messages
        )
        await self.io.call(self.memory.refresh_memory, job.id, context.pop("_evidenceKeys"))
        job.seen_messages.update(m["message"] for thread in context["threads"] for m in thread["messages"])
        return public_result(
            self.register_external_read(
                job,
                "chat_read",
                arguments,
                {
                    **social,
                    "messages": events,
                    "conversationContext": context,
                    **({"deliveries": await self.routing.statuses()} if self.routing is not None else {}),
                    **({"recentSentMessages": messages} if messages else {}),
                },
            )
        )

    async def _call(
        self,
        job: JobAccess,
        name: str,
        arguments: JsonObject,
        call_id: str,
        *,
        startup: bool = False,
        host_handoff: bool = False,
    ) -> JsonObject:
        result = await self._call_or_prepare(job, name, arguments, call_id, startup=startup, host_handoff=host_handoff)
        assert isinstance(result, dict)
        return result

    async def prepare_host_effect(
        self,
        job: JobAccess,
        name: str,
        arguments: JsonObject,
        call_id: str,
        *,
        host_handoff: bool = False,
    ) -> PreparedHostEffect:
        """Validate/freeze one native envelope without a send or local receipt journal."""
        if name not in HOST_EFFECT_TOOLS:
            return PreparedHostEffect(
                name=name,
                arguments=copy.deepcopy(arguments),
                result={"error": "HOST_EFFECT_NOT_SUPPORTED", "applied": False},
            )
        result = await self._call_or_prepare(
            job, name, arguments, call_id, host_handoff=host_handoff, prepare_only=True
        )
        if isinstance(result, PreparedHostEffect):
            return result
        return PreparedHostEffect(name=name, arguments=copy.deepcopy(arguments), result=copy.deepcopy(result))

    async def _call_or_prepare(
        self,
        job: JobAccess,
        name: str,
        arguments: JsonObject,
        call_id: str,
        *,
        startup: bool = False,
        host_handoff: bool = False,
        prepare_only: bool = False,
    ) -> JsonObject | PreparedHostEffect:
        self.check(job)
        if self.memory.authored_enabled and name in {"memory_read", "memory_search", "memory_update"}:
            return {"error": "TOOL_NOT_PERMITTED", "applied": False}
        arguments = copy.deepcopy(arguments)
        await self.verify_identity(job)
        if BOOKKEEPING & arguments.keys():
            return {
                "error": "INVALID_TOOL_ARGUMENTS",
                "applied": False,
                "recoverable": True,
                "invalidFields": [{"fields": [key]} for key in sorted(BOOKKEEPING & arguments.keys())],
                "instruction": "Supply gameplay choices only; the service manages request bookkeeping.",
            }
        if name == "memory_update":
            model = tool_model(name, job.purpose)
            try:
                # Keep independent per-edit permission errors and their receipts.
                # The advertised role schema describes the edits that can succeed.
                arguments = MemoryUpdate.model_validate(arguments).model_dump(exclude_unset=True)
            except ValidationError as error:
                return memory_argument_error(arguments, error, model)
        elif name in TOOL_MODELS:
            model = tool_model(name, job.purpose)
            try:
                arguments = model.model_validate(arguments).model_dump(by_alias=True, exclude_unset=True)
            except ValidationError as error:
                return argument_error(model, arguments, error)
        if name == "player_command":
            try:
                arguments = command_arguments(arguments)
            except ValidationError as error:
                return command_error(arguments, error)
        canonical_arguments = copy.deepcopy(arguments)
        coordinator = self.role_coordinator
        if name == "player_command" and arguments.pop("reviewAfter", False):
            if coordinator is None or job.purpose != "military":
                return {"error": "ACTIVE_MILITARY_ROLE_REQUIRED", "applied": False}
        if name == "economy_wait":
            if coordinator is None:
                return {"error": "ACTIVE_ROLE_REQUIRED", "applied": False}
            return await coordinator.economy_wait(job, arguments, call_id)
        if coordinator is not None and job.purpose in {"economy", "military"}:
            from civ_agent.economy_roles import ECONOMY_TOOLS

            allowed = (
                ECONOMY_TOOLS | {"war_goals", "role_message"}
                if job.purpose == "economy"
                else ECONOMY_TOOLS | ROLE_TOOLS | {"economy_request", "military_review"}
            )
            if name not in allowed:
                return {"error": "ROLE_ACTION_NOT_PERMITTED", "applied": False}
        if name == "military_review":
            from civ_agent.military_completion import completion_review, review_page

            if coordinator is None or job.purpose != "military":
                return {"error": "ACTIVE_MILITARY_ROLE_REQUIRED", "applied": False}
            view = await job.connection.tool("player_turn", {})
            if (
                view.get("epoch") != job.epoch
                or view.get("turn") != coordinator.key[1]
                or view.get("phase") != "ACTIVE"
                or (view.get("negotiation") or {}).get("active")
            ):
                return self.reobserve_after_negotiation()
            await coordinator.synchronize_war(view)
            saved = (
                await self.io.call(coordinator.military_schedule.saved)
                if coordinator.military_schedule is not None
                else {}
            )
            review = completion_review(view, coordinator.war_state, saved)
            return await self.deliver_turn(
                job,
                view,
                name,
                arguments,
                call_id,
                {
                    "militaryCompletion": review_page(review, arguments.get("cursor", 0), arguments.get("limit", 20)),
                },
            )
        if name in ROLE_TOOLS:
            if name == "war_goals" and arguments.get("action", "read") == "read":
                state = await self.io.call(
                    self.wars.read, job.purpose, arguments, coordinator.requests if coordinator is not None else ()
                )
                episodes = state.get("episodes", {})
                if state.get("section") == "episodes":
                    episodes = {row["id"]: row for row in state["items"]}
                job.war_versions.update(goal_versions({"episodes": episodes}))
                return state
            if coordinator is None:
                return {"error": "ACTIVE_ROLE_REQUIRED", "applied": False}
            return await coordinator.role_tool(job, name, arguments, call_id)
        if coordinator is not None and name in {"finish_role", "economy_request", "city_override"}:
            if name == "economy_request":
                refresh = await self.refresh_economy_request(job, arguments, call_id)
                if refresh is not None:
                    return refresh
            return await coordinator.tool(job, name, arguments)
        if coordinator is None and name in {"finish_role", "economy_request", "city_override"}:
            return {"error": "ACTIVE_ROLE_REQUIRED", "applied": False}
        if (
            coordinator is not None
            and name == "player_command"
            and job.purpose in {"gameplay", "economy", "military"}
            and not host_handoff
        ):
            if arguments["kind"] == "END_TURN":
                if prepare_only:
                    return {
                        "error": "ROLE_FINISH_REQUIRED",
                        "applied": False,
                        "instruction": "Finish this role; the coordinator owns the native end-turn handoff.",
                    }
                # Older model integrations may still send END_TURN: it finishes only their role.
                return await coordinator.tool(
                    job,
                    "finish_role",
                    {k: arguments[k] for k in ("deferChoices", "deferReason", "initiativeDecisions") if k in arguments},
                )
            if job.observation is None:
                return self.reobserve_after_negotiation("Read player_turn before acting for your assigned role.")
            problem = coordinator.command_problem(job, arguments)
            if problem:
                return problem
        intent = arguments.pop("intent", None)
        if intent is not None:
            intent = validate(Intent, intent).model_dump()
        initiative_decisions = arguments.pop("initiativeDecisions", [])
        delivery = "auto"
        selected = None
        if name == "command_help":
            catalogue = await self._tools(job)
            command = next((tool for tool in catalogue if tool["name"] == "player_command"), None)
            kinds = set(arguments["kinds"])
            if command is None or not kinds <= set(command_kinds(command["inputSchema"])):
                return {"error": "COMMAND_NOT_PERMITTED", "applied": False}
            return {
                "description": command["description"],
                "inputSchema": command_schema(
                    kinds, military_review=job.purpose == "military" and coordinator is not None
                ),
            }
        if name == "native_advice":
            if not self.native_advice_enabled or job.purpose not in {"gameplay", "economy", "military"}:
                return {"error": "NATIVE_ADVICE_DISABLED", "applied": False}
            if job.observation is None:
                return self.reobserve_after_negotiation("Read player_turn before native_advice.")
            try:
                async with asyncio.timeout(5):
                    value = await job.connection.tool(
                        "native_advice", {**arguments, "epoch": job.epoch, "revision": job.observation["revision"]}
                    )
            except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
                value = {
                    "available": False,
                    "epoch": job.epoch,
                    "revision": job.observation["revision"],
                    "recommendations": [],
                    "limitations": ["Native advice unavailable; continue ordinary gameplay."],
                }
            self.check(job)
            if startup:
                rows = value.get("recommendations", [])
                if not isinstance(rows, list):
                    rows = []
                value = {**value, "recommendations": rows[:8], "remainingRecommendations": max(0, len(rows) - 8)}
            await self.io.call(self.memory.observe, "native_advice", value, job.id)
            return value
        if name == "chat_delivery":
            if self.routing is None:
                return {"error": "DIPLOMACY_ROUTING_DISABLED", "applied": False}
            return await self.routing.control(job, arguments)
        if name == "history_read":
            value = await self.io.call(
                self.memory.history,
                arguments["turn"],
                arguments.get("offset", 0),
                job=job.id,
                include_review=arguments.get("includeReview", False),
            )
            result = public_result(value)
            if "review" in value:
                result["review"] = value["review"]  # Preserve the authored report exactly.
            return result
        self.check(job)
        if name in DETAIL_MODELS:
            if job.observation is None:
                return self.reobserve_after_negotiation("Read player_turn before requesting details.")
            query = DetailQuery.model_validate({"tool": name, "arguments": content_arguments(arguments)})
            value = project_detail(job.observation, query)
            scope = detail_scope(query)
            version, delivered = await self.io.call(
                self.memory.observe_detail, scope, value, job.id, detail_fingerprint(job.observation, query)
            )
            job.memory["records"]["observation:" + scope] = {
                "version": version,
                "category": "observation",
                "body": native_observation(value),
            }
            if value["available"] and query not in job.details_read:
                job.details_read.append(query)
            return public_result(delivered)
        if name == "trade_prepare":
            value = await job.connection.tool("player_communication", {**arguments, "op": name, "epoch": job.epoch})
            self.check(job)
            if value.get("valid"):
                value["publish"] = {
                    "tool": "trade_offer",
                    "arguments": {"partner": arguments["partner"], "give": value["give"], "receive": value["receive"]},
                }
            return public_result(value)
        if name in {"trade_options", "trade_read", "agreement_list", "agreement_read"}:
            value = (
                await self.read_trade(job, arguments["proposalId"])
                if name == "trade_read"
                else await job.connection.tool("player_communication", {**arguments, "op": name, "epoch": job.epoch})
            )
            self.check(job)
            if "error" not in value:
                if job.purpose == "communication":
                    if name == "trade_options" and value["mode"] == "NATIVE_AI":
                        value.update(available=False, canInitiate=False, unavailableReason=NATIVE_TRADE_GAMEPLAY_ONLY)
                        value["friendship"] = {
                            **value.get("friendship", {}),
                            "available": False,
                            "unavailableReason": NATIVE_TRADE_GAMEPLAY_ONLY,
                        }
                if name == "agreement_list":
                    scope = "agreements"
                elif name == "agreement_read":
                    scope = "agreement/" + arguments["agreement"]
                elif name == "trade_options":
                    scope = "trade_options/" + arguments["partner"]
                else:
                    scope = "trade/" + arguments["proposalId"]
                if name.startswith("agreement_"):
                    value["evidence"] = "observation:" + scope
                try:
                    version = await self.io.call(self.memory.observe, scope, value, job.id)
                except StateConflict as error:
                    if name not in {"agreement_read", "agreement_list"} or str(error) != (
                        "AGREEMENT_CHANGED: refresh with " + name
                    ):
                        raise
                    return {
                        "error": "AGREEMENT_CHANGED",
                        "applied": False,
                        "recoverable": True,
                        "recovery": {"reads": [{"tool": name, "arguments": arguments}]},
                        "instruction": "A newer agreement event arrived during this read. Read the current "
                        "agreement again before deciding; the older terms were not delivered.",
                    }
                job.memory["records"]["observation:" + scope] = {
                    "version": version,
                    "category": "observation",
                    "body": native_observation(value),
                }
                if name == "trade_read":
                    job.trades[arguments["proposalId"]] = copy.deepcopy(value["trade"])
                value = self.register_external_read(job, name, arguments, value)
            return public_result(value)
        if name in READ_TOOLS:
            if job.purpose == "communication" and name not in COMMUNICATION_READ_TOOLS:
                return {"error": "TOOL_NOT_PERMITTED", "applied": False}
            if name in {"movement_preview", "mission_preview", "player_inspect"} and job.observation is None:
                return self.reobserve_after_negotiation("Read player_turn before requesting this inspection.")
            value = await job.connection.tool(name, {} if name == "player_turn" else arguments)
            while name == "player_turn" and job.purpose == "gameplay" and value.get("negotiation", {}).get("active"):
                await self.wait_for_gameplay(job)
                value = await job.connection.tool(name, {})
            self.check(job)
            if "error" not in value:
                add_decision_commands(name, value, arguments)
            if name == "player_turn" and "error" not in value:
                previous_scope = (job.overview_view, job.city_scope)
                job.city_scope = arguments.get("cities")
                selected_view = arguments.get("view", "current")
                if selected_view != "current":
                    job.overview_view = selected_view
                if previous_scope != (job.overview_view, job.city_scope):
                    job.delivered_overview = None
                if "delivery" in arguments:
                    job.compact_overviews = True
                return await self.deliver_turn(
                    job,
                    value,
                    name,
                    arguments,
                    call_id,
                    force_full=startup or previous_scope != (job.overview_view, job.city_scope),
                )
            elif name in {"movement_preview", "mission_preview", "player_inspect"} and "error" not in value:
                if value.get("revision") != job.detail_revision:
                    self.forget_observation(job)
                    return self.reobserve_after_negotiation(
                        "The game changed. Read player_turn, then request a fresh preview."
                    )
                query = DetailQuery(tool=name, arguments=arguments)
                scope = detail_scope(query)
                value = {**value, "evidence": "observation:" + scope, "query": query.model_dump()}
                version, delivered = await self.io.call(self.memory.observe_detail, scope, value, job.id)
                job.memory["records"]["observation:" + scope] = {
                    "version": version,
                    "category": "observation",
                    "body": native_observation(value),
                }
                if value.get("available") and query not in job.details_read:
                    job.details_read.append(query)
                value = delivered
            elif "error" not in value:
                if name == "player_capabilities":
                    value = {
                        **value,
                        "queries": list(
                            dict.fromkeys(
                                [query for query in value.get("queries", []) if query in READ_TOOLS]
                                + list(DETAIL_MODELS)
                            )
                        ),
                    }
                    if self.role_coordinator is not None:
                        catalogue = await self._tools(job)
                        command = next((tool for tool in catalogue if tool["name"] == "player_command"), None)
                        if command is not None:
                            allowed = command_kinds(command["inputSchema"])
                            value["commands"] = [kind for kind in value.get("commands", []) if kind in allowed]
                await self.io.call(self.memory.observe, name, value, job.id)
                if name in EXTERNAL_DETAIL_TOOLS:
                    value = self.register_external_read(job, name, arguments, value)
            return public_result(value)
        if name == "chat_read":
            return await self.chat_read(job, arguments)
        if name == "chat_history":
            page = await self.io.call(self.memory.chat_history, arguments.get("with"), arguments.get("query", ""))
            await self.io.call(
                self.memory.refresh_memory, job.id, ["event:" + str(event["id"]) for event in page["events"]]
            )
            job.seen_messages.update(event["body"]["message"] for event in page["events"] if event["kind"] == "message")
            return public_result(page)
        if name == "memory_read":
            chosen = await self.io.call(
                self.memory.refresh_memory,
                job.id,
                arguments["ids"],
                include_retired=arguments.get("includeRetired", False),
                startup=startup,
            )
            RecordStore.deliver(job.memory, chosen)
            result = memory_response(
                public_result({"records": [public_record(key, record) for key, record in chosen.items()]}),
                {key: record["version"] for key, record in chosen.items()},
                job.memory_read_pending if job.coordination_preparing else job.memory_read_seen,
                job.coordination_scope,
                job.payload_sequence + 1,
                full=startup or full_delivery(arguments),
            )
            return public_result(MemoryReadResult.model_validate(result).model_dump(exclude_unset=True))
        if name == "memory_search":
            result = await self.io.call(self.memory.search_memory, job.id, content_arguments(arguments))
            if not arguments.get("history"):
                chosen = await self.io.call(
                    self.memory.delivered_records,
                    job.id,
                    [r["id"] for r in result["records"]],
                )
                job.memory["records"].update(chosen)
                result = memory_response(
                    public_result(result),
                    {key: record["version"] for key, record in chosen.items()},
                    job.memory_read_pending if job.coordination_preparing else job.memory_read_seen,
                    job.coordination_scope,
                    job.payload_sequence + 1,
                    full=startup or full_delivery(arguments),
                )
            return public_result(MemorySearchResult.model_validate(result).model_dump(exclude_unset=True))
        if name == "memory_update":
            owners = None
            if self.role_coordinator is not None and job.purpose in {"gameplay", "economy", "military"}:
                edits_city_plan = job.purpose == "economy" or any(
                    (change.get("content") or {}).get("type") == "city_plan"
                    or job.memory["records"].get(change.get("id"), {}).get("category") == "city_plan"
                    for change in arguments["changes"]
                )
                if edits_city_plan and job.observation is None:
                    return self.reobserve_after_negotiation(
                        "Read player_turn before editing city plans for your assigned role."
                    )
                owners = {
                    city["handle"]: self.role_coordinator.owner(city["handle"])
                    for city in (job.observation or {}).get("cities", [])
                }

            def respond(result):
                return {
                    **compact_memory_receipt(public_result(result), arguments["changes"], job.memory_seen),
                    "recordScope": job.coordination_scope,
                    **self.coordination_delivery(job),
                }

            result = await self.io.call(self.memory.patch, job.id, arguments["changes"], call_id, owners, respond)
            expanded = apply_memory_receipt(arguments["changes"], result, job.memory_seen)
            for change in expanded["changes"]:
                if change["accepted"]:
                    record = change["record"]
                    job.memory["records"][change["id"]] = {
                        "version": change["version"],
                        "category": record["type"],
                        "body": record.get("body", {k: v for k, v in record.items() if k != "id"}),
                    }
            return MemoryUpdateResult.model_validate(result).model_dump(exclude_unset=True)
        if name == "player_command":
            if job.purpose not in {"gameplay", "economy", "military"}:
                return {"error": "GAMEPLAY_NOT_PERMITTED", "applied": False}
            if job.observation is None:
                return self.reobserve_after_negotiation(
                    "Read player_turn before acting. Successful commands normally return stateUpdate; a failed refresh requires this full read."
                )
            needed = required_detail(arguments)
            if needed is not None and needed not in job.details_read:
                return {
                    "error": "DETAIL_REQUIRED",
                    "applied": False,
                    "recoverable": True,
                    "recovery": {
                        "reads": [needed.model_dump()],
                        "instruction": "Read this action's full offer and consequences, then make a fresh decision.",
                    },
                }
            if coordinator is not None and (
                arguments["kind"] == "DECLARE_WAR"
                or (arguments["kind"] == "CITY_STATE_ACTION" and arguments.get("selection") == "declareWar")
            ):
                state = await self.io.call(self.wars.state)
                if arguments.get("target") not in state["drafts"]:
                    return {"error": "INITIAL_WAR_GOALS_REQUIRED", "applied": False}
            payload = {**arguments, "epoch": job.epoch, "revision": job.observation["revision"]}
            if arguments.get("kind") == "END_TURN":
                payload["reviewId"] = job.observation.get("turnReview", {}).get("id")
            transport = "player_command"
        elif name in {"trade_offer", "trade_decide", "friendship_offer"}:
            payload = {**arguments, "op": name, "epoch": job.epoch}
            selected = None
            reference = payload.pop("proposalId", None) or payload.pop("counterofferTo", None)
            if reference:
                payload["proposal"] = reference
                selected = job.trades.get(reference)
                if selected is None:
                    # Diagnose a typo without treating an internal read as the
                    # agent having inspected or consented to the terms.
                    checked = await self.read_trade(job, reference)
                    if "error" in checked:
                        return checked
                    return {
                        "error": "PROPOSAL_NOT_OBSERVED",
                        "applied": False,
                        "recoverable": True,
                        "recovery": {
                            "reads": [trade_inspection(reference)],
                            "instruction": "Read this proposal's exact terms, then make a new decision.",
                        },
                    }
                payload["version"] = selected["version"]
            if job.purpose == "communication":
                routing = selected
                if routing is None:
                    routing = await job.connection.tool(
                        "player_communication",
                        {"op": "trade_options", "epoch": job.epoch, "partner": arguments["partner"]},
                    )
                    self.check(job)
                    if "error" in routing:
                        return routing
                if routing["mode"] == "NATIVE_AI":
                    return {
                        "error": "GAMEPLAY_ONLY",
                        "applied": False,
                        "recoverable": True,
                        "instruction": NATIVE_TRADE_GAMEPLAY_ONLY,
                    }
            transport = "player_communication"
        elif name in {"chat_send", "commitment_offer", "commitment_decide"}:
            payload = {**arguments, "epoch": job.epoch}
            if name == "chat_send":
                delivery = payload.pop("delivery", "auto")
                payload["op"] = "send"
                if payload.get("nativeAction") is not None:
                    if job.observation is None:
                        return self.reobserve_after_negotiation(
                            "Read player_turn and the native diplomatic options before acting."
                        )
                    native_action = arguments["nativeAction"]
                    if not isinstance(native_action, dict):
                        raise ValueError("INVALID_ARGUMENT")
                    payload["nativeAction"] = {
                        **native_action,
                        "revision": job.observation["revision"],
                    }
                else:
                    payload.pop("nativeAction", None)
                    social = job.social if job.social is not None else await self.read_social(job, delivered=False)
                    recipients = social["chatRecipients"]
                    targets = arguments.get("recipients", [arguments["to"]] if arguments.get("to") else [])
                    if any(target not in recipients or target == self.memory.civilization for target in targets):
                        return unavailable(
                            "recipients" if "recipients" in arguments else "to",
                            "Ordinary chat needs a player who can receive it. Use trade tools or native diplomatic actions for built-in AI.",
                            chatRecipients=recipients,
                        )
                    audience = (
                        {self.memory.civilization, *targets} if targets else {self.memory.civilization, *recipients}
                    )
                    if any(name not in audience for name in arguments.get("mentions", [])):
                        return unavailable("mentions", "Mention only players receiving this message.")
                if not payload.get("to"):
                    payload.pop("to", None)
            elif name == "commitment_offer":
                social = job.social if job.social is not None else await self.read_social(job, delivered=False)
                problem = commitment_problem(self.memory.civilization, arguments, social)
                if problem:
                    return problem
                reference = payload.pop("reviseProposalId", None)
                payload["op"] = "revise" if reference else "offer"
                if reference:
                    payload["proposal"] = reference
            else:
                payload["op"] = payload.pop("decision")
                payload["proposal"] = payload.pop("proposalId")
                if "publicReason" in payload:
                    payload["reason"] = payload.pop("publicReason")
                if job.social is None or not job.social_read:
                    return {
                        "error": "PROPOSAL_NOT_OBSERVED",
                        "applied": False,
                        "recoverable": True,
                        "recovery": {
                            "reads": [{"tool": "chat_read", "arguments": {}}],
                            "instruction": "Read the current commitment terms before deciding.",
                        },
                    }
                offers = [offer for offer in job.social["offers"] if offer["proposal"] == arguments["proposalId"]]
                if not offers:
                    return unavailable("proposalId", "Copy the proposalId from a current commitment in chat_read.")
                selected = (
                    offers[-1]
                    if payload["op"] == "accept"
                    else next((offer for offer in offers if offer.get("effective")), offers[-1])
                )
                payload["version"] = selected["version"]
            transport = "player_communication"
        else:
            return {"error": "TOOL_NOT_PERMITTED", "applied": False}
        declaring = declaration_targets(self.memory.civilization, name, payload, selected, job.observation or {})
        if declaring and self.role_coordinator is not None:
            if job.purpose != "gameplay":
                return {"error": "MAIN_PLAYER_DECISION_REQUIRED", "applied": False}
            state = await self.io.call(self.wars.state)
            if not declaring <= state["drafts"].keys():
                return {"error": "INITIAL_WAR_GOALS_REQUIRED", "applied": False, "opponents": sorted(declaring)}
        if self.peace_reviews is not None:
            problem = await self.peace_reviews.authorize(
                job, name, payload, selected if name in {"trade_offer", "trade_decide", "friendship_offer"} else None
            )
            if problem:
                return problem
        if prepare_only:
            async with self.dispatch:
                self.check(job)
                ordinary_chat = name == "chat_send" and not arguments.get("nativeAction")
                if ordinary_chat:
                    update = await self.conversation_update(job, arguments)
                    if update:
                        return update
                return PreparedHostEffect(
                    name=name,
                    arguments=canonical_arguments,
                    transport=transport,
                    payload={**copy.deepcopy(payload), "requestId": host_request_id(job.id, call_id)},
                    intent=copy.deepcopy(intent),
                    initiative_decisions=copy.deepcopy(initiative_decisions),
                    routed=ordinary_chat and self.routing is not None,
                )
        if name == "chat_send" and not arguments.get("nativeAction") and self.routing is not None:
            return await self.routing.send(job, payload, call_id, immediate=delivery == "immediate")
        # State validation and persistence are short. Network execution is
        # serialized separately, never monopolizing the event inbox.
        async with self.dispatch:
            self.check(job)
            if name == "chat_send" and not arguments.get("nativeAction"):
                update = await self.conversation_update(job, arguments)
                if update:
                    return update
            request = await self.io.call(
                self.memory.queue, job.id, transport, payload, call_id, intent, initiative_decisions
            )
            if request["result"] is not None:
                return {
                    **json.loads(request["result"]),
                    **({"intentFeedback": request["intentFeedback"]} if "intentFeedback" in request else {}),
                }
            self.check(job)
            self.in_flight += 1
            try:
                native_effect_started()
                value = await job.connection.tool(transport, json.loads(request["payload"]))
                await self.io.call(self.memory.receipt, request["id"], value)
            except (OSError, ValueError, TimeoutError, aiohttp.ClientError):
                # A new call ID cannot duplicate a possibly committed action.
                # Recovery checks the durable request with the host first.
                job.closed = True
                await self.io.call(self.memory.uncertain, request["id"])
                raise
            finally:
                self.in_flight -= 1
            if (
                name in {"trade_offer", "trade_decide", "friendship_offer"}
                and "error" not in value
                and value.get("trade")
            ):
                for proposal in (
                    arguments.get("proposalId"),
                    arguments.get("counterofferTo"),
                    value["trade"].get("proposal"),
                ):
                    if proposal:
                        job.trades.pop(proposal, None)
            if job.suspend_for_negotiation and "error" not in value:
                task = asyncio.current_task()
                assert task is not None
                job.completed_actions[task] = {
                    "tool": name,
                    "arguments": copy.deepcopy(arguments),
                    "result": public_result(value),
                }
            if (
                (
                    transport == "player_command"
                    or name == "trade_decide"
                    and value.get("outcome")
                    or value.get("nativeOutcome")
                )
                and "error" not in value
                and job.purpose != "gameplay"
            ):
                self.invalidate_role_observation(job)
            # Controller routing (including native AI versus shared trades) uses
            # the raw receipt. The outgoing boundary projects the model response.
            return {**value, **({"intentFeedback": request["intentFeedback"]} if "intentFeedback" in request else {})}

    async def handle(self, request: web.Request):
        if not request.secure or "Origin" in request.headers or len(request.headers.getall("Authorization", [])) != 1:
            raise web.HTTPForbidden()
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        job = next((job for candidate, job in self.jobs.items() if hmac.compare_digest(candidate, token)), None)
        if job is None or job.closed:
            raise web.HTTPUnauthorized()
        body = await request.json()
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            raise web.HTTPBadRequest()
        method = body.get("method")
        if "id" not in body:
            return web.Response(status=202)
        try:
            await self._wait_for_admission(review=job.review_inputs is not None)
            self.check(job, ended_allowed=True)
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "civilization", "version": "1"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": await self.tools(job)}
            elif method == "tools/call":
                parameters = body.get("params", {})
                value = await self.invoke(job, parameters["name"], parameters.get("arguments", {}), str(body["id"]))
                result = tool_result(value)
            else:
                return web.json_response(
                    {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "Method not available"}}
                )
        except (StateConflict, ValueError, KeyError) as error:
            result = tool_result({"error": str(error)})
        except (OSError, TimeoutError, aiohttp.ClientError):
            result = tool_result(
                {"error": "The action's outcome is uncertain. Stop; do not repeat it.", "recoverable": False}
            )
        # Normalize only the outgoing representation; stored calls and receipts
        # keep their original payloads. Other MCP clients retain both forms.
        if request.headers.get(RESULT_FORMAT_HEADER) == STRUCTURED_RESULT_FORMAT:
            result = canonical_tool_result(result)
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})
