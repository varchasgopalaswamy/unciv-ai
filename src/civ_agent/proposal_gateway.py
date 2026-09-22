"""Job-scoped HTTPS tools. Only this trusted controller holds host credentials."""

import asyncio
import copy
import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import aiohttp
from aiohttp import web
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from civ_agent.action_templates import add_decision_commands, research_change
from civ_agent.civilization_state import CivilizationState, StaleAssumptions, StateConflict
from civ_agent.game_commands import add_combat_commands, command_arguments, command_error, command_schema
from civ_agent.mcp_results import RESULT_FORMAT_HEADER, STRUCTURED_RESULT_FORMAT, canonical_tool_result
from civ_agent.memory_records import (
    REQUIRED_COMMANDS,
    REQUIRED_TOOLS,
    Intent,
    MemoryReadResult,
    MemorySearchResult,
    MemoryUpdate,
    MemoryUpdateResult,
    memory_argument_error,
    public_record,
)
from civ_agent.model_view import model_view
from civ_agent.observations import (
    DETAIL_MODELS,
    DetailQuery,
    TurnOverview,
    detail_satisfies,
    detail_scope,
    native_observation,
    overview,
    project_detail,
    required_detail,
)
from civ_agent.protocol import JsonObject
from civ_agent.schemas import validate
from civ_agent.service_io import ServiceIO
from civ_agent.stale_recovery import REPEATED_STALE
from civ_agent.state_updates import apply_overview_update, changed_details, overview_update, refresh_required, stamp
from civ_agent.tactical_review import strategy_review, tactical_review
from civ_agent.telemetry import JobAccounting
from civ_agent.tool_schemas import TOOL_MODELS, TOOL_OUTPUT_MODELS

BOOKKEEPING = {"requestId", "epoch", "revision", "reviewId", "socialVersion"}
MEMORY_PHASE_TOOLS = {"memory_read", "memory_search", "memory_update", "history_read"}
READ_TOOLS = {
    "player_capabilities",
    "player_turn",
    "player_result",
    "research_details",
    "movement_preview",
    "mission_preview",
    "policy_details",
    "victory_progress",
    "player_summary",
    "owned_cities",
    "free_great_person_choice",
}
COMMUNICATION_READ_TOOLS = {"player_turn", "player_result", "victory_progress"}
ECONOMIC = {
    "BUY_CONSTRUCTION",
    "BUY_TILE",
    "SELL_BUILDING",
    "UPGRADE_UNIT",
    "DISBAND_UNIT",
    "QUEUE_CONSTRUCTION",
    "FOUND_CITY",
    "ACTIVATE_GREAT_PERSON",
    "ADD_SPACESHIP_PART",
    "CREATE_WATER_IMPROVEMENT",
    "CITY_STATE_ACTION",
    "GIFT_UNIT",
}


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
    delivered_overview: JsonObject | None = None
    social: JsonObject | None = None
    social_read: bool = False
    detail_revision: str | None = None
    details_read: list[DetailQuery] = field(default_factory=list)
    trades: dict[str, JsonObject] = field(default_factory=dict)
    closed: bool = False
    verified: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    wait_started: float | None = None
    completed_wait: float = 0
    owns_connection: bool = False
    catalogue: list[JsonObject] | None = None
    memory_after_handoff: bool = False
    gameplay_fenced: bool = False
    handoff_at: float | None = None
    gameplay_ended: asyncio.Event = field(default_factory=asyncio.Event)
    model_stopped: asyncio.Event = field(default_factory=asyncio.Event)

    def waiting_seconds(self) -> float:
        return self.completed_wait + (time.monotonic() - self.wait_started if self.wait_started is not None else 0)


class ProposalGateway:
    def __init__(self, memory: CivilizationState, io: ServiceIO, workspace: Path):
        self.memory, self.io, self.workspace = memory, io, workspace
        self.jobs: dict[str, JobAccess] = {}
        self.paused = False
        self.checkpoint_requested = False
        self.checkpoint_released = asyncio.Event()
        self.checkpoint_released.set()
        self.calls_in_flight = 0
        self.dispatch = asyncio.Lock()
        self.in_flight = 0
        self.deferred_completions = []
        self.accounting: JobAccounting | None = None
        self.history = None
        self.run_failure = memory.run_failure()
        self.failed = asyncio.Event()
        if self.run_failure:
            self.failed.set()

    @asynccontextmanager
    async def admit(self):
        """Drain accepted calls before capture; later calls wait without model retries."""
        while not self.checkpoint_released.is_set():
            await self.checkpoint_released.wait()
        self.calls_in_flight += 1
        try:
            yield
        finally:
            self.calls_in_flight -= 1

    def request_checkpoint(self):
        self.checkpoint_requested = True
        self.checkpoint_released.clear()

    def release_checkpoint(self):
        self.checkpoint_requested = False
        self.checkpoint_released.set()

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
        self.jobs[job.token] = job
        return job

    async def close(self, job: JobAccess, completed: bool):
        job.closed = True
        self.jobs.pop(job.token, None)
        if job.owns_connection:
            await job.connection.close()
        if self.paused or self.checkpoint_requested:
            self.deferred_completions.append((job.epoch, job.id, completed))
            return
        await self.io.call(self.memory.finish_job, job.id, completed)
        if job.purpose != "gameplay":
            await self.io.call(self.memory.export, self.workspace)

    def check(self, job: JobAccess, *, memory_allowed=False):
        if self.run_failure:
            raise StateConflict(self.run_failure["error"])
        if self.paused or job.closed or self.jobs.get(job.token) is not job:
            raise StateConflict("JOB_PAUSED_OR_EXPIRED")
        if job.gameplay_fenced and not memory_allowed:
            raise StateConflict("MEMORY_ONLY: gameplay has ended; use private memory tools, then finish")

    async def tools(self, job: JobAccess):
        async with self.admit():
            return await self._tools(job)

    async def _tools(self, job: JobAccess):
        self.check(job, memory_allowed=True)
        if job.gameplay_fenced:
            return copy.deepcopy([tool for tool in job.catalogue or [] if tool["name"] in MEMORY_PHASE_TOOLS])
        if job.catalogue is not None:
            return copy.deepcopy(job.catalogue)
        upstream = await job.connection.list_tools()
        tools = []
        for tool in upstream:
            if tool["name"] in READ_TOOLS and (job.purpose == "gameplay" or tool["name"] in COMMUNICATION_READ_TOOLS):
                entry = copy.deepcopy(tool)
                if tool["name"] == "player_turn":
                    entry["description"] = (
                        "Read the current overview at turn startup or to resynchronize: tactics, strategy review, "
                        "city/unit roster, pending decisions, factual alerts and notifications. Inspect legal options and "
                        "consequences with focused detail tools before acting; use chat_read for social promises.\n"
                        "After successful gameplay actions, apply stateUpdate instead of reading player_turn again:\n"
                        "- delta: baseStateId must match your stateId; adopt the new stateId. set replaces whole "
                        "sections; top-level removed deletes fields. collections upsert replaces whole rows by key, "
                        "removed drops IDs, and optional order sets row order. Omitted values stay unchanged. "
                        "Removals mean absent from this view, not necessarily destroyed. Ignore an update already "
                        "at your stateId; read player_turn for a missing/mismatched base.\n"
                        "- full: replace the overview. unavailable: keep the successful receipt and read player_turn; "
                        "refresh failure never undoes an action. Never repeat an applied action to refresh state.\n"
                        "- turn_ended: gameplay has ended; follow memoryPhase if supplied, otherwise stop.\n"
                        "native.availableDemandsToSend lists actions YOU may send, not requests received or promises. "
                        "Received native messages are in alerts.nativeDiplomacy with sender, event and response options. "
                        "For city-state messages, copy an offered option's RESPOND_CITY_STATE_ALERT command. "
                        "Major-player messages supply responseTool for chat_send with nativeAction. "
                        "Inspect new alerts and decisions. Reread relevant invalidatedDetails only when needed for "
                        "the next action; unchanged inspections remain valid. An argument error with applied=false "
                        "alone needs no refresh; follow recovery.reads when supplied."
                        " A response's memoryUpdates contains versioned private records, separate from the overview; "
                        "replace older copies by ID, retaining newer versions on replay. Inspect their reviewSources "
                        "before revision; receiving a record does not read its cited evidence."
                    )
                    entry["outputSchema"] = TurnOverview.model_json_schema()
                elif tool["name"] in {"research_details", "policy_details"}:
                    entry["description"] = entry["description"].replace(
                        "re-observe afterward", "read the returned stateUpdate"
                    )
                    entry["description"] += (
                        " Prefer names=[candidate names] from the overview for detailed relevant choices and copyable "
                        "commands; omit names for broader planning. Copy command and add intent."
                    )
                    if tool["name"] == "research_details":
                        entry["description"] += (
                            " SET_RESEARCH_QUEUE replaces current research and the entire queue. "
                            "command starts the offered path now; appendToQueue preserves all queued technologies. "
                            "Inspect researchQueue and the command label before choosing. Stored science is retained."
                        )
                elif tool["name"] == "mission_preview":
                    entry["description"] += (
                        " Read unit_details sections=[air], then copy this preview's command and add intent. "
                        "Any native revision expires the preview; the chosen command needs a current matching preview. "
                        "Interception is automatic; preserve readiness by leaving the unit unused or skipping it."
                    )
                elif tool["name"] == "movement_preview":
                    entry["description"] += (
                        " Copy this preview's SET_UNIT_DESTINATION command and add intent. "
                        "Any native revision expires the preview; the chosen command needs a current matching preview. "
                        "Read unit_details sections=[movement] to inspect or cancel orders. "
                        "For settlers, inspect settlement separately from route availability: waiting for movement "
                        "does not fix city spacing or foreign ownership. Reconsider repeatedly blocked sites."
                    )
                tools.append(entry)
            elif tool["name"] == "player_command" and job.purpose == "gameplay":
                entry = copy.deepcopy(tool)
                entry["inputSchema"] = command_schema()
                entry["description"] = (
                    "Execute one game action: kind (e.g. FOUND_CITY) is an argument, not a tool name. Copy only an "
                    "inspected option's command, add intent, and follow its required inspections. Add a "
                    "confirmationField only after reading and accepting its consequences; templates never supply "
                    "consent. Use the selected kind's schema for actions without templates. The service handles "
                    "request bookkeeping. Verify the native receipt; apply stateUpdate as described by player_turn.\n"
                    "Read alert facts and consequences before responding or acknowledging. DISMISS_NOTIFICATIONS takes "
                    "targets=[read notice handles]; dismissed contents remain in receipts. "
                    "CAST_DIPLOMATIC_VOTE uses selection=candidate civilizationId from victory.ballot. "
                    "ABSTAIN_DIPLOMATIC_VOTE and ACKNOWLEDGE_DIPLOMATIC_RESULTS take only kind and intent; "
                    "read victory.votingResults before acknowledging results. "
                    "END_TURN uses the current turnReview. Idle units and tactical warnings do not block it; "
                    "top-level deferReason is optional audit context. "
                    "ACKNOWLEDGE_ALERT uses target, not handle. Copy the overview's alert/endTurn command. "
                    "RESPOND_CITY_STATE_ALERT uses an alert target and an offered response selection from the overview. "
                    "FOUND_CITY needs unit_details sections=[settlement]; SET_UNIT_DESTINATION needs a matching "
                    "movement_preview(target,destination), whose command uses engine pathfinding. "
                    "Batch useful memory updates after END_TURN when memoryPhase is supplied."
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
                            "history_read",
                            "chat_read",
                            "chat_history",
                            "memory_read",
                            "memory_search",
                            "trade_options",
                            "trade_read",
                            "agreement_list",
                            "agreement_read",
                        },
                        "destructiveHint": False,
                        "openWorldHint": False,
                    },
                    "inputSchema": TOOL_MODELS[name].model_json_schema(),
                }
            )
            if name in TOOL_OUTPUT_MODELS:
                tools[-1]["outputSchema"] = TOOL_OUTPUT_MODELS[name].model_json_schema()

        for name, description in {
            "unit_details": "Inspect one owned unit by its player_turn handle. Prefer explicit sections: movement (paths/orders; use movement_preview for distant destinations), combat (attacks), orders, promotions, work (worker/water improvements; Work Boats are consumed), settlement (current-site native blockers, movement requirements and native promises; movement_preview also inspects prospective city sites), economy (upgrade/pillage/disband), abilities (great people/spaceship/gifts), or air (missions, bases/capacity, carried units and interception readiness; use mission_preview for special missions). Multiple sections are allowed; omitted sections or [all] returns full details. Only returned sections count as inspected. Read availability, warnings and confirmations; copy an offered command and add intent. Receipts report actual effects.",
            "city_details": "Inspect one owned city by its player_turn handle. Prefer explicit sections: status (capture choices), production (queue/progress/options), citizens (growth/work tiles/specialists/focus), economy (yields/buildings/sales), purchases, or combat (bombardment). Multiple sections are allowed. Omitted sections or [all] returns full details. Each section retains native consequences; unrelated sections do not count as inspected. Prefer construction_details(names=[candidates]) for queueing/buying specific buildings or units. Copy offered commands and add your intent.",
            "construction_details": "Inspect construction in an owned city. Supply up to 16 names for descriptions, costs, eligibility, queue progress, purchase confirmations and copyable commands. Omit names for a compact catalogue only when choosing candidates. Read the named option before queueing or buying; copy command and add your intent. Confirm purchases explicitly after reading consequences.",
            "map_region": "Read explored tiles and frontier within radius 0–4 of an observed coordinate, respecting world wrap. Hidden tiles have no data. Default format=compact: each tile inherits tileDefaults, with its explicit fields overriding those defaults, including null/false/empty values. All visible facts are retained; position, terrain, visibility, units and yields remain on each tile. Use format=full for expanded tile objects. Read player_details(map_index) to locate distant regions.",
            "player_details": "Read full economy, diplomacy (including war consequences), research progress, policy choices, current/previous turn receipts, or map coordinate index. Uses the latest permitted snapshot; returned evidence identifies what was read. For CITY_STATE_ACTION inspect section=diplomacy: target=civilizationId, selection=available actions.name, and confirmAction=true only after accepting any confirmation. DECLARE_WAR also accepts city-states. Inspect receipt.cityState for actual influence and alliance changes.",
        }.items():
            add(name, description)

        add(
            "trade_options",
            "Inspect trade terms, city names/populations, eligibility, unavailable reasons, research funding, friendship and active agreements with a met player. WarDeclaration names a third civilization both players know; give commits you to war and receive asks the partner to declare. Your own native declaration warnings and possibly relevant social promises are included. Weigh socialAdvisories when choosing; they never restrict legal actions. give/receive are from your perspective. City names in terms are short trade IDs. Available during either player's turn.",
        )
        add(
            "trade_read",
            "Read a host-owned binding trade before accepting, declining, withdrawing or counteroffering. Every clause has an explicit from/to direction, identical for both players. Pending deal IDs appear in chat_read. Interpret socialAdvisories yourself; they are reminders, not native restrictions or host judgments of breach. Re-read after changed terms; never poll. Replies to NATIVE_AI offers require your active turn; shared human/agent offers support off-turn replies.",
        )
        add(
            "agreement_list",
            "List your observed enacted trade rows and native relationships, including ended agreements, stable IDs, remaining turns and native cancellation availability. These are separate from pending proposals and social promises. Read agreement_read for exact current terms and ending history. The returned evidence ID can support private memory.",
        )
        add(
            "agreement_read",
            "Read one of your enacted agreements by its agreement ID: first-observed and current give/receive terms, remaining turns, ending reasons and effects. give/receive are from your perspective. NATIVE_RELATIONSHIP terms represent one relationship; their amount is not a signing price. Use the returned evidence ID for private memory; review affected plans after lifecycle events. Fixed-duration agreements have no voluntary cancellation control in the native UI.",
        )
        add(
            "trade_offer",
            "Publish exact binding terms from trade_options and your consent. Only the current player can initiate; invite them in chat otherwise. For human/agent partners the offer pauses all gameplay. For a built-in AI, NATIVE_AI requests stay pending until its turn and gameplay continues. Gameplay calls for shared negotiations return after settlement; communication jobs handle replies without waiting. Copy the offered name, amount and duration for Embassy, Agreement, City, Treaty and WarDeclaration. WarDeclaration in give makes YOU declare; in receive makes the PARTNER declare. Both sides can declare on the same target for joint war. Inspect your native declaration warnings. Social promises never restrict legal game actions. Treaties must match on both sides; at war include mutual Peace Treaty. Research Agreement amount is each player's signing cost after immediate gold transfers. give/receive are from YOUR perspective. Set proposal to counter a trade inspected with trade_read, within counteroffersRemaining. No effect until acceptance. Avoid duplicate proposals; changed circumstances or terms may justify a new proposal.",
        )
        add(
            "friendship_offer",
            "As the current player, propose an engine Declaration of Friendship with a met player using the fixed terms shown by trade_options. With a human/agent partner this pauses gameplay until acceptance, decline or the deadline. With a built-in AI it queues a native friendship request and gameplay continues until the AI decides on its turn. Shared-negotiation gameplay calls wait in code while communication jobs handle replies. It is separate from a verbal promise and unlocks eligible research agreements and defensive pacts.",
        )
        add(
            "trade_decide",
            "Accept or decline the exact trade or friendship proposal you inspected with trade_read; an author can withdraw it. Supply only its ID and decision. Acceptance rechecks legality and enacts the stored terms atomically, including off turn. Only ENACTED with an engine outcome confirms execution. Changed, expired or unavailable terms require a new decision.",
        )
        add(
            "history_read",
            "Inspect confirmed actions and detailed receipts from one earlier game turn when needed. Turn numbers are those shown by player_turn.",
        )
        add(
            "chat_read",
            "Read current context (turn, scenario instructions, economy and known diplomacy), messages, social promises, trade IDs and enacted agreement summaries once. Inspect binding proposals with trade_read or enacted terms/history with agreement_read. Social consent has no native game effect. socialAdvisories are reminders, not breach judgments: interpret, discuss, honor or break ambiguous promises freely, without required withdrawal, reconsideration or clarification. Incoming private messages, trades, agreement changes and public mentions wake communication automatically; never poll or wait for replies.",
        )
        add(
            "chat_history",
            "Find earlier permitted messages and commitments. Returns the most recent 20 matches; narrow query for older evidence. No polling is needed.",
        )
        add(
            "chat_send",
            "Send authenticated text. Omit to for public chat; mention players to notify them. Optional nativeAction explicitly executes DENOUNCE or ISSUE_DEMAND against target civilization (selection=type from native.availableDemandsToSend), or RESPOND_DIPLOMACY against an owned alert handle with an offered selection. Available demands are actions you may initiate, not incoming requests. Read player_turn first: native diplomacy requires your active turn and current observations. Inspect the received alert's nativeDiplomacy sender, event, options, declaresWar and consequences before responding. Ordinary text has no game effects. Built-in AI only understands explicit native requests. Trade and peace use trade tools.",
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
            "Read current records by ID. Active records by default; includeRetired explicitly to inspect or replace retired records. Observations describe earlier facts; statements are claims, not world facts.",
        )
        add(
            "memory_search",
            "Search private records by type, subject, state or text, 20 per page. Active only by default. history=true explicitly reads reachable revisions as historical evidence, never active instructions. Use nextOffset for another page.",
        )
        add(
            "memory_update",
            "Create, revise, replace or retire typed private plans, beliefs, notes and SOUL principles. Code assigns IDs and turn metadata. Read before changing existing records. Notes use text and optional plan: a previously read plan ID, even if retired or needing review. This is a historical link, not an active dependency. Notes do not accept sources. Beliefs require claim, uncertainty and sources. Copy sources exactly: record/message IDs, observation:overview, a detail response's short evidence handle (e123), or observation:communication after chat_read. Do not construct evidence IDs. Revisions can retain their existing historical citations without rereading them. Explicit assumptions are ongoing dependencies: refresh those you keep; drop obsolete ones when changing strategy. Review a flagged plan before reusing it for intent. For STALE_ASSUMPTIONS, follow recovery reads and reconsider. Retirement retains history and does not cancel a promise. Use relevant subjects such as economy, Greece or entity handles.",
        )
        job.catalogue = copy.deepcopy(tools)
        return tools

    async def read_social(self, job: JobAccess, delivered: bool = True):
        result = await job.connection.tool("player_communication", {"op": "state", "epoch": job.epoch})
        if "error" in result:
            raise StateConflict("COMMUNICATION_UNAVAILABLE")
        self.check(job)
        version = await self.io.call(self.memory.observe, "communication", result, job.id if delivered else None)
        job.memory["records"]["observation:communication"] = {
            "version": version,
            "category": "observation",
            "body": native_observation(result),
        }
        job.social_read = delivered or (job.social_read and result == job.social)
        job.social = result
        return result

    async def call(
        self, job: JobAccess, name: str, arguments: JsonObject, call_id: str, *, source: str = "tool"
    ) -> JsonObject:
        async with self.admit():
            return await self._admitted_call(job, name, arguments, call_id, source=source)

    async def _admitted_call(
        self, job: JobAccess, name: str, arguments: JsonObject, call_id: str, *, source: str
    ) -> JsonObject:
        accounting = self.accounting
        counted = (
            await self.io.call(accounting.begin_call, job.id, name, arguments, source=source) if accounting else None
        )
        outcome, replayed = "incomplete", False
        ended = False
        try:
            result, replayed = await self._call_once(job, name, arguments, call_id)
            outcome = "error" if "error" in result else "success"
            if result.get("error") in (REPEATED_STALE, "STATE_UPDATE_FAILED"):
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
            ended = job.memory_after_handoff and result.get("stateUpdate", {}).get("kind") == "turn_ended"
            if ended:
                job.gameplay_fenced = True
            return public_result(result)
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
                await self.io.call(accounting.end_call, counted, outcome, replayed)
            if ended:
                # The native receipt, tool response and accounting are durable.
                # Fence further calls before notifying the lifecycle controller.
                if job.handoff_at is None:
                    job.handoff_at = time.monotonic()
                job.gameplay_ended.set()

    async def _call_once(
        self, job: JobAccess, name: str, arguments: JsonObject, call_id: str
    ) -> tuple[JsonObject, bool]:
        self.check(job, memory_allowed=True)
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise ValueError("INVALID_ARGUMENT")
        async with job.lock:
            self.check(job, memory_allowed=True)
            previous = await self.io.call(self.memory.call, job.id, call_id, name, arguments)
            if previous is not None:
                if "memoryPhase" in previous:
                    await self.io.call(self.memory.begin_memory_phase, job.id)
                    job.gameplay_fenced = True
                return previous, True
            # The action receipt may be durable even if a barrier interrupted
            # saving the MCP response. Replay it without needing a new observation.
            receipt = await self.io.call(self.memory.call_receipt, job.id, call_id)
            self.check(
                job,
                memory_allowed=name in MEMORY_PHASE_TOOLS
                or (receipt is not None and name == "player_command" and arguments.get("kind") == "END_TURN"),
            )
            if receipt is not None:
                result = public_result(receipt)
            else:
                try:
                    result = await self._call(job, name, arguments, call_id)
                except StaleAssumptions as conflict:
                    # Nothing was queued or sent. Cache the rejection too: replaying
                    # this call after a read must never execute an old decision.
                    result = {
                        "error": "STALE_ASSUMPTIONS",
                        "applied": False,
                        "recoverable": True,
                        "changedRecords": conflict.keys,
                        "conflicts": conflict.details,
                        "recovery": await self.io.call(
                            self.memory.conflict_recovery, job.id, conflict.keys, name, arguments
                        ),
                    }
            result = await self.settle_gameplay(job, name, result)
            result = await self.update_after_action(job, name, arguments, call_id, result)
            if job.memory_after_handoff and result.get("stateUpdate", {}).get("kind") == "turn_ended":
                result = {
                    **result,
                    "memoryPhase": {
                        "allowedTools": sorted(MEMORY_PHASE_TOOLS),
                        "instruction": (
                            "The next player may act now. Batch useful changed plans, beliefs, notes and retirements; "
                            "skip unchanged records. Use this conversation's evidence; no new game reads or messages. "
                            "Copy evidence handles exactly. Historical citations and note.plan links may reference "
                            "retired records; ongoing assumptions and action intent require current reviewed records. "
                            "If another job changed a record, read it before reconciling; retain review flags for newer "
                            "evidence. Finish after saving useful changes. No final narrative is needed."
                        ),
                    },
                }
            result = await self.io.call(self.memory.call, job.id, call_id, name, arguments, result)
            assert result is not None
            if "memoryPhase" in result:
                await self.io.call(self.memory.begin_memory_phase, job.id)
                job.gameplay_fenced = True
            return result, receipt is not None

    @staticmethod
    def forget_observation(job: JobAccess) -> None:
        job.observation = None
        job.delivered_overview = None
        job.detail_revision = None
        job.details_read.clear()

    async def deliver_turn(
        self,
        job: JobAccess,
        value: JsonObject,
        name: str,
        arguments: JsonObject,
        call_id: str,
        receipt: JsonObject | None = None,
    ) -> JsonObject:
        """Register only the reconstructed overview; detail contents stay opt-in."""
        self.check(job)
        revision = value.get("revision")
        if not isinstance(revision, str) or value.get("epoch") != job.epoch:
            raise StateConflict("INVALID_HOST_REVISION")
        previous, baseline = job.observation, job.delivered_overview
        value = {**value, "communication": await self.read_social(job, delivered=False)}
        add_combat_commands(value)
        if "map" in value and "combatReports" in value:
            value = {"tacticalReview": tactical_review(value).model_dump(exclude_none=True), **value}
        compact = public_result(overview(value))
        compact.pop("strategyReview", None)
        compact.pop("stateId", None)
        invalidated = changed_details(previous, value, job.details_read)

        def respond(records, changed):
            current = stamp(
                {**compact, "strategyReview": strategy_review(records).model_dump()}, job.id, job.epoch, revision
            )
            result = (
                current
                if receipt is None
                else {**receipt, "stateUpdate": overview_update(baseline, current, invalidated)}
            )
            if changed:
                result["memoryUpdates"] = [
                    {"version": record["version"], "record": public_record(key, record)}
                    for key, record in sorted(changed.items())
                ]
            return result

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
                not call_id.startswith("startup:"),
            )
        except BaseException:
            # A cancelled I/O await may still have committed the response. Replay
            # it from SQLite; never reuse a possibly older in-memory observation.
            self.forget_observation(job)
            raise
        job.memory["records"].update(observed)
        job.observation = value
        job.delivered_overview = (
            {key: part for key, part in result.items() if key != "memoryUpdates"}
            if receipt is None
            else apply_overview_update(baseline, result["stateUpdate"])
        )
        job.details_read = [query for query in job.details_read if query not in invalidated]
        job.detail_revision = revision
        return result

    async def update_after_action(
        self,
        job: JobAccess,
        name: str,
        arguments: JsonObject,
        call_id: str,
        result: JsonObject,
    ) -> JsonObject:
        if job.purpose != "gameplay" or "error" in result:
            return result
        changes_game = (
            name == "player_command"
            or bool(result.get("nativeOutcome"))
            or (name in {"trade_offer", "friendship_offer", "trade_decide"} and bool(result.get("trade")))
        )
        if not changes_game:
            return result
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
            if str(error) == "HOST_UNAVAILABLE":
                self.forget_observation(job)
                self.check(job)
                return {**result, "stateUpdate": refresh_required()}
            return await self.failed_update(job, call_id, result, error)
        except (ValueError, KeyError, TypeError) as error:
            return await self.failed_update(job, call_id, result, error)
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
            return await self.deliver_turn(job, value, name, arguments, call_id, result)
        except (OSError, ValueError, KeyError, TypeError, SQLAlchemyError) as error:
            return await self.failed_update(job, call_id, result, error)

    async def failed_update(self, job, call_id, result, error):
        self.forget_observation(job)
        self.check(job)  # Paused/expired jobs must stop, retaining the raw receipt.
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

    async def wait_for_gameplay(self, job: JobAccess) -> bool:
        """Long-poll in code, outside dispatch and the memory actor. Chat jobs remain runnable."""
        waited = False
        job.wait_started = time.monotonic()
        try:
            while True:
                self.check(job)
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
                waited = True
        finally:
            job.completed_wait += time.monotonic() - job.wait_started
            job.wait_started = None

    async def settle_gameplay(self, job: JobAccess, name: str, result: JsonObject) -> JsonObject:
        if job.purpose != "gameplay":
            return result
        if name in {"trade_offer", "friendship_offer"} and "error" not in result and result.get("trade"):
            if result["trade"].get("mode") == "NATIVE_AI":
                return {
                    **result,
                    "instruction": "Native request queued. Continue gameplay; the built-in AI decides on its turn. A later event reports the outcome. Do not wait or poll.",
                }
            await self.wait_for_gameplay(job)
            # Return the settled canonical terms, even when this is recovery of a lost offer response.
            final = await self._call(job, "trade_read", {"proposal": result["trade"]["proposal"]}, "settlement")
            return {
                **final,
                "outcome": final["trade"].get("outcome"),
                "instruction": "Negotiation closed. Read stateUpdate and reconsider gameplay using the resulting state.",
            }
        error = result.get("error")
        if name == "player_command" and isinstance(error, dict) and error.get("code") == "NEGOTIATION_ACTIVE":
            await self.wait_for_gameplay(job)
            job.observation = None
            return self.reobserve_after_negotiation()
        return result

    @staticmethod
    def reobserve_after_negotiation(
        instruction: str = "Negotiation closed. Read the changed game state and make a fresh decision.",
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

    async def _call(self, job: JobAccess, name: str, arguments: JsonObject, call_id: str) -> JsonObject:
        self.check(job, memory_allowed=name in MEMORY_PHASE_TOOLS)
        arguments = copy.deepcopy(arguments)
        if not job.verified:
            if job.gameplay_fenced:
                raise StateConflict("MEMORY_ONLY: no prepared job identity")
            identity = await job.connection.tool("player_communication", {"op": "state", "epoch": job.epoch})
            if identity.get("civilization") != self.memory.civilization or identity.get("epoch") != job.epoch:
                raise StateConflict("PLAYER_CREDENTIAL_MISMATCH")
            job.verified = True
        if BOOKKEEPING & arguments.keys():
            raise ValueError("Supply gameplay choices only; the service manages request bookkeeping.")
        kind = arguments.get("kind")
        needs_intent = (
            name in REQUIRED_TOOLS or name == "player_command" and isinstance(kind, str) and kind in REQUIRED_COMMANDS
        )
        if needs_intent and not arguments.get("intent"):
            return {
                "error": "INTENT_REQUIRED",
                "applied": False,
                "recoverable": True,
                "instruction": 'Nothing was dispatched. Add intent as an object: {"reason":"your reason"}, or {"plan":"existing active plan ID"}. This error alone requires no state refresh.',
            }
        if name == "memory_update":
            try:
                arguments = MemoryUpdate.model_validate(arguments).model_dump(exclude_unset=True)
            except ValidationError as error:
                return memory_argument_error(arguments, error)
        elif name in TOOL_MODELS:
            arguments = validate(TOOL_MODELS[name], arguments).model_dump(by_alias=True, exclude_unset=True)
        if name == "player_command":
            try:
                arguments = command_arguments(arguments)
            except ValidationError as error:
                return command_error(arguments, error)
        intent = arguments.pop("intent", None)
        if intent is not None:
            intent = validate(Intent, intent).model_dump()
        if name == "history_read":
            return public_result(
                await self.io.call(self.memory.history, arguments["turn"], arguments.get("offset", 0), job=job.id)
            )
        self.check(job, memory_allowed=name in MEMORY_PHASE_TOOLS)
        if name in DETAIL_MODELS:
            if job.observation is None:
                return self.reobserve_after_negotiation("Read player_turn before requesting details.")
            query = DetailQuery.model_validate({"tool": name, "arguments": arguments})
            value = project_detail(job.observation, query)
            scope = detail_scope(query)
            version, delivered = await self.io.call(self.memory.observe_detail, scope, value, job.id)
            job.memory["records"]["observation:" + scope] = {
                "version": version,
                "category": "observation",
                "body": native_observation(value),
            }
            if value["available"] and query not in job.details_read:
                job.details_read.append(query)
            return public_result(delivered)
        if name in {"trade_options", "trade_read", "agreement_list", "agreement_read"}:
            value = await job.connection.tool("player_communication", {**arguments, "op": name, "epoch": job.epoch})
            self.check(job)
            if "error" not in value:
                if name == "agreement_list":
                    scope = "agreements"
                elif name == "agreement_read":
                    scope = "agreement/" + arguments["agreement"]
                elif name == "trade_options":
                    scope = "trade_options/" + arguments["partner"]
                else:
                    scope = "trade/" + arguments["proposal"]
                if name.startswith("agreement_"):
                    value["evidence"] = "observation:" + scope
                version = await self.io.call(self.memory.observe, scope, value, job.id)
                job.memory["records"]["observation:" + scope] = {
                    "version": version,
                    "category": "observation",
                    "body": native_observation(value),
                }
                if name == "trade_read":
                    job.trades[arguments["proposal"]] = copy.deepcopy(value["trade"])
            return public_result(value)
        if name in READ_TOOLS:
            if job.purpose == "communication" and name not in COMMUNICATION_READ_TOOLS:
                raise StateConflict("TOOL_NOT_PERMITTED")
            if name in {"movement_preview", "mission_preview"} and job.observation is None:
                return self.reobserve_after_negotiation(
                    "Read player_turn before requesting mission or movement previews."
                )
            value = await job.connection.tool(name, arguments)
            while name == "player_turn" and job.purpose == "gameplay" and value.get("negotiation", {}).get("active"):
                await self.wait_for_gameplay(job)
                value = await job.connection.tool(name, arguments)
            self.check(job)
            if "error" not in value:
                add_decision_commands(name, value, arguments)
            if name == "player_turn" and "error" not in value:
                return await self.deliver_turn(job, value, name, arguments, call_id)
            elif name in {"movement_preview", "mission_preview"} and "error" not in value:
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
                    value = {**value, "queries": list(dict.fromkeys(value.get("queries", []) + list(DETAIL_MODELS)))}
                await self.io.call(self.memory.observe, name, value, job.id)
            return public_result(value)
        if name == "chat_read":
            social = await self.read_social(job)
            recent = await self.io.call(self.memory.snapshot, job.purpose)
            events = recent["events"]
            await self.io.call(
                self.memory.refresh_memory, job.id, ["event:" + str(event["id"]) for event in events], recent["cursor"]
            )
            return public_result({**social, "messages": events})
        if name == "chat_history":
            page = await self.io.call(self.memory.chat_history, arguments.get("with"), arguments.get("query", ""))
            await self.io.call(
                self.memory.refresh_memory, job.id, ["event:" + str(event["id"]) for event in page["events"]]
            )
            return public_result(page)
        if name == "memory_read":
            chosen = await self.io.call(
                self.memory.refresh_memory,
                job.id,
                arguments["ids"],
                include_retired=arguments.get("includeRetired", False),
            )
            job.memory["records"].update(chosen)
            return public_result(
                MemoryReadResult.model_validate(
                    {"records": [public_record(key, record) for key, record in chosen.items()]}
                ).model_dump(exclude_unset=True)
            )
        if name == "memory_search":
            result = await self.io.call(self.memory.search_memory, job.id, arguments)
            if not arguments.get("history"):
                chosen = await self.io.call(
                    self.memory.refresh_memory,
                    job.id,
                    [r["id"] for r in result["records"]],
                    include_retired=arguments.get("state") in {"all", "retired"},
                )
                job.memory["records"].update(chosen)
            return public_result(MemorySearchResult.model_validate(result).model_dump(exclude_unset=True))
        if name == "memory_update":
            result = await self.io.call(self.memory.patch, job.id, arguments["changes"], call_id)
            accepted = [change["id"] for change in result["changes"] if change["accepted"]]
            refreshed = await self.io.call(self.memory.refresh_memory, job.id, accepted, include_retired=True)
            job.memory["records"].update(refreshed)
            return MemoryUpdateResult.model_validate(result).model_dump(exclude_unset=True)
        if name == "player_command":
            if job.purpose != "gameplay":
                raise StateConflict("GAMEPLAY_NOT_PERMITTED")
            if job.observation is None:
                return self.reobserve_after_negotiation(
                    "Read player_turn, then this action's required detail before deciding. Successful commands normally return stateUpdate; a failed refresh requires this full read."
                )
            needed = required_detail(arguments)
            if needed is not None and not any(detail_satisfies(read, needed) for read in job.details_read):
                return {
                    "error": "DETAIL_REQUIRED",
                    "applied": False,
                    "recoverable": True,
                    "recovery": {
                        "reads": [needed.model_dump()],
                        "instruction": "Read this action's full offer and consequences, then make a fresh decision.",
                    },
                }
            payload = {**arguments, "epoch": job.epoch, "revision": job.observation["revision"]}
            if arguments.get("kind") == "END_TURN":
                payload["reviewId"] = job.observation.get("turnReview", {}).get("id")
            transport = "player_command"
        elif name in {"trade_offer", "trade_decide", "friendship_offer"}:
            payload = {**arguments, "op": name, "epoch": job.epoch}
            if "proposal" in arguments:
                selected = job.trades.get(arguments["proposal"])
                if selected is None:
                    raise StateConflict("Read this proposal's exact terms with trade_read before deciding.")
                payload["version"] = selected["version"]
            transport = "player_communication"
        elif name in {"chat_send", "commitment_offer", "commitment_decide"}:
            payload = {**arguments, "epoch": job.epoch}
            if name == "chat_send":
                payload["op"] = "send"
                if payload.get("nativeAction") is not None:
                    if job.observation is None:
                        raise StateConflict("Read player_turn and the native diplomatic options before acting.")
                    native_action = arguments["nativeAction"]
                    if not isinstance(native_action, dict):
                        raise ValueError("INVALID_ARGUMENT")
                    payload["nativeAction"] = {
                        **native_action,
                        "revision": job.observation["revision"],
                    }
                else:
                    payload.pop("nativeAction", None)
            elif name == "commitment_offer":
                payload["op"] = "revise" if arguments.get("proposal") else "offer"
            else:
                payload["op"] = payload.pop("decision")
                if job.social is None or not job.social_read:
                    raise StateConflict("Read the current terms with chat_read before deciding.")
                offers = [offer for offer in job.social["offers"] if offer["proposal"] == arguments.get("proposal")]
                if not offers:
                    raise StateConflict("PROPOSAL_NOT_OBSERVED")
                selected = (
                    offers[-1]
                    if payload["op"] == "accept"
                    else next((offer for offer in offers if offer.get("effective")), offers[-1])
                )
                payload["version"] = selected["version"]
            transport = "player_communication"
        else:
            raise ValueError("TOOL_NOT_PERMITTED")
        policy_keys = set()
        if name == "chat_send" and arguments.get("nativeAction"):
            policy_keys.add("observation:turn")
        if transport == "player_command":
            policy_keys.add("observation:turn")
            if arguments.get("kind") in ECONOMIC:
                policy_keys.add("observation:economy")
            if arguments.get("kind") in {"SET_RESEARCH_QUEUE", "CHOOSE_FREE_TECHNOLOGY"}:
                policy_keys.add("observation:research")
            if arguments.get("kind") in {"ADOPT_POLICY", "DISMISS_POLICY_PICKER"}:
                policy_keys.add("observation:policy")
            for scope in ("unit/", "city/", "diplomacy/"):
                key = "observation:" + scope + str(arguments.get("target"))
                if key in job.memory["records"]:
                    policy_keys.add(key)
        other = (
            (arguments.get("nativeAction") or {}).get("target")
            or arguments.get("to")
            or arguments.get("target")
            or arguments.get("partner")
        )
        war_terms = []
        if name == "trade_offer":
            war_terms = arguments.get("give", []) + arguments.get("receive", [])
        if name == "trade_decide":
            selected = job.trades[arguments["proposal"]]
            other = selected["partner"] if selected["author"] == self.memory.civilization else selected["author"]
            war_terms = [clause["item"] for clause in selected.get("clauses", [])]
        diplomatic_subjects = {other} | {item["name"] for item in war_terms if item.get("type") == "WarDeclaration"}
        for subject in diplomatic_subjects:
            key = "observation:diplomacy/" + str(subject)
            if subject and key in job.memory["records"]:
                policy_keys.add(key)
        dependencies = {key: job.memory["records"].get(key, {}).get("version", 0) for key in policy_keys}
        # State validation and persistence are short. Network execution is
        # serialized separately, never monopolizing the event inbox.
        async with self.dispatch:
            self.check(job)
            request = await self.io.call(self.memory.queue, job.id, transport, payload, dependencies, call_id, intent)
            if request["result"] is not None:
                return public_result(json.loads(request["result"]))
            self.check(job)
            self.in_flight += 1
            try:
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
                (
                    transport == "player_command"
                    or name == "trade_decide"
                    and value.get("outcome")
                    or value.get("nativeOutcome")
                )
                and "error" not in value
                and job.purpose != "gameplay"
            ):
                self.forget_observation(job)
            return public_result(value)

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
            while not self.checkpoint_released.is_set():
                await self.checkpoint_released.wait()
            self.check(job, memory_allowed=True)
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
                value = await self.call(job, parameters["name"], parameters.get("arguments", {}), str(body["id"]))
                result = tool_result(value)
            else:
                return web.json_response(
                    {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "Method not available"}}
                )
        except (StateConflict, ValueError, KeyError) as error:
            result = tool_result({"error": str(error)})
        except (OSError, TimeoutError, aiohttp.ClientError):
            result = tool_result(
                {"error": "Response uncertain; the service retained the request for reconciliation. Stop this job."}
            )
        # Normalize only the outgoing representation; stored calls and receipts
        # keep their original payloads. Other MCP clients retain both forms.
        if request.headers.get(RESULT_FORMAT_HEADER) == STRUCTURED_RESULT_FORMAT:
            result = canonical_tool_result(result)
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})
