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

from civ_agent import experiment_metrics
from civ_agent.action_templates import add_decision_commands, research_change
from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.diplomacy_tools import argument_error, commitment_problem, unavailable
from civ_agent.game_commands import add_combat_commands, command_arguments, command_error, command_schema
from civ_agent.mcp_results import RESULT_FORMAT_HEADER, STRUCTURED_RESULT_FORMAT, canonical_tool_result
from civ_agent.memory_records import (
    Intent,
    MemoryContributions,
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
from civ_agent.protocol import JsonObject
from civ_agent.schemas import validate
from civ_agent.service_io import ServiceIO
from civ_agent.social_context import social_context
from civ_agent.state_updates import apply_overview_update, changed_details, overview_update, refresh_required, stamp
from civ_agent.strategic_review import frozen_read
from civ_agent.tactical_review import strategy_review, tactical_review
from civ_agent.telemetry import JobAccounting
from civ_agent.tool_schemas import TOOL_MODELS, TOOL_OUTPUT_MODELS, tool_model

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
    delivered_overview: JsonObject | None = None
    social: JsonObject | None = None
    social_read: bool = False
    detail_revision: str | None = None
    details_read: list[DetailQuery] = field(default_factory=list)
    pending_invalidated: list[DetailQuery] = field(default_factory=list)
    trades: dict[str, JsonObject] = field(default_factory=dict)
    closed: bool = False
    verified: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    wait_started: float | None = None
    completed_wait: float = 0
    owns_connection: bool = False
    catalogue: list[JsonObject] | None = None
    gameplay_fenced: bool = False
    handoff_at: float | None = None
    gameplay_ended: asyncio.Event = field(default_factory=asyncio.Event)
    model_stopped: asyncio.Event = field(default_factory=asyncio.Event)
    review_inputs: JsonObject | None = None
    outgoing_start: int = 0
    seen_messages: set[str] = field(default_factory=set)
    pending_calls: dict[asyncio.Task, JsonObject] = field(default_factory=dict)
    negotiation_calls: dict[asyncio.Task, JsonObject] = field(default_factory=dict)
    completed_actions: dict[asyncio.Task, JsonObject] = field(default_factory=dict)
    negotiation_pending: asyncio.Event = field(default_factory=asyncio.Event)
    suspend_for_negotiation: bool = False

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
    async def admit(self, *, review=False):
        """Drain accepted calls before capture; later calls wait without model retries."""
        await self._wait_for_admission(review=review)
        self.calls_in_flight += 1
        try:
            yield
        finally:
            self.calls_in_flight -= 1

    async def _wait_for_admission(self, *, review=False):
        # MCP initialization and tool calls must use the same barrier rule.
        # Reviewers have frozen inputs and may connect during the quiet phase.
        while not self.checkpoint_released.is_set() and not (review and self.reviewing):
            await self.checkpoint_released.wait()

    def request_checkpoint(self):
        if self.routing is not None:
            self.routing.suspend()
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
                self.memory.select_startup_context, job.id, topics, job.memory.get("advisoryRanking")
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

    async def tools(self, job: JobAccess):
        async with self.admit(review=job.review_inputs is not None):
            return await self._tools(job)

    async def _tools(self, job: JobAccess):
        self.check(job, ended_allowed=True)
        if job.gameplay_fenced:
            return []
        if job.catalogue is not None:
            return copy.deepcopy(job.catalogue)
        upstream = await job.connection.list_tools()
        job.catalogue = self.tool_catalogue(
            upstream,
            job.purpose,
            routing_enabled=self.routing is not None,
            native_advice_enabled=self.native_advice_enabled,
        )
        return copy.deepcopy(job.catalogue)

    @staticmethod
    def tool_catalogue(upstream, purpose, *, routing_enabled=False, native_advice_enabled=False):
        """Project native tools without starting a gameplay or notebook job."""
        tools = []
        for tool in upstream:
            if tool["name"] in READ_TOOLS and (purpose == "gameplay" or tool["name"] in COMMUNICATION_READ_TOOLS):
                entry = copy.deepcopy(tool)
                if tool["name"] == "player_turn":
                    entry["description"] = (
                        "Read the current overview at turn startup or to resynchronize: tactics, strategy review, "
                        "city/unit roster, pending decisions, factual alerts and notifications. Inspect legal options and "
                        "consequences with focused detail tools before acting; use chat_read for social promises.\n"
                        "After successful gameplay actions, apply stateUpdate instead of reading player_turn again:\n"
                        "- delta: baseStateId must match your stateId; adopt the new stateId. set replaces whole "
                        "sections; top-level removed deletes fields. collections upsert replaces whole rows by key, "
                        "removed drops IDs, and optional order sets row order. Sections and rows absent from the update stay unchanged. "
                        "Each included row replaces the entire old row: fields absent from that replacement are removed. "
                        "Removals mean absent from this view, not necessarily destroyed. Ignore an update already "
                        "at your stateId; read player_turn for a missing/mismatched base.\n"
                        "- full: replace the overview. unavailable: keep the successful receipt and read player_turn; "
                        "refresh failure never undoes an action. Never repeat an applied action to refresh state.\n"
                        "- turn_ended: gameplay has ended; finish this conversation.\n"
                        "native.availableDemandsToSend lists actions YOU may send, not requests received or promises. "
                        "Received native messages are in alerts.nativeDiplomacy with sender, event and response options. "
                        "For city-state messages, copy an offered option's RESPOND_CITY_STATE_ALERT command. "
                        "Major-player messages supply responseTool for chat_send with nativeAction. "
                        "Inspect new alerts and decisions. Reread relevant invalidatedDetails only when needed for "
                        "the next action; unchanged inspections remain valid. An argument error with applied=false "
                        "alone needs no refresh; follow recovery.reads when supplied."
                        " Research/policy/victory/result and trade/agreement/chat reads are separate from overview "
                        "updates. When an earlier read becomes outdated, its query "
                        "appears in invalidatedDetails. Full reads and communication actions may return this list "
                        "at the top level. A full player_turn replaces the overview, not those independent reads."
                        " A response's memoryUpdates contains versioned private records, separate from the overview; "
                        "replace older copies by ID, retaining newer versions on replay. Review notices are advisory "
                        "and never require a memory edit or prohibit an action."
                    )
                    entry["outputSchema"] = TurnOverview.model_json_schema()
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
                    entry["description"] += (
                        " Copy this preview's SET_UNIT_DESTINATION command; intent is optional. "
                        "Refresh the preview after another action before using its command. "
                        "Read unit_details sections=[movement] to inspect or cancel orders. "
                        "Each turn stop's afterMove estimates remaining movement, attack/founding availability and "
                        "visible endpoint threats/own military. Context is current proximity within its radius, "
                        "not enemy reachability or guaranteed protection; unseenTiles marks incomplete visibility. "
                        "Civilian stops include civilianExposure: protection, visible capture assessment, surrounding "
                        "visibility gaps and references to known camps/recent sightings in tacticalReview. "
                        "NO_VISIBLE_CAPTURE_THREAT does not mean safe. Same-tile military travels with a civilian "
                        "only after ESCORT pairing. "
                        "For settlers, inspect settlement separately from route availability: waiting for movement "
                        "does not fix city spacing or foreign ownership. Reconsider repeatedly blocked sites."
                    )
                tools.append(entry)
            elif tool["name"] == "player_command" and purpose == "gameplay":
                entry = copy.deepcopy(tool)
                entry["inputSchema"] = command_schema()
                entry["description"] = (
                    "Execute one game action: kind (e.g. FOUND_CITY) is an argument, not a tool name. Use "
                    "offered commands when useful; intent annotations and routine detail reads are optional. Native previews remain required. Civilian movement with "
                    "captureRisk requires explicit confirmAction=true; it accepts only the current move, not later route turns. Add a "
                    "confirmationField only after reading and accepting its consequences; templates never supply "
                    "consent. The compact schema lists possible fields; the server strictly validates required and "
                    "permitted fields for the selected kind. Invalid arguments return expectedArguments without acting. "
                    "Check the reported outcome; apply stateUpdate as described by player_turn.\n"
                    "Read alert facts and consequences before responding or acknowledging. DISMISS_NOTIFICATIONS takes "
                    "targets=[read notice handles]; dismissed contents remain in receipts. "
                    "CAST_DIPLOMATIC_VOTE uses selection=candidate civilizationId from victory.ballot. "
                    "ABSTAIN_DIPLOMATIC_VOTE and ACKNOWLEDGE_DIPLOMATIC_RESULTS take kind and optional intent; "
                    "read victory.votingResults before acknowledging results. "
                    "Review the current unit roster and pending decisions before END_TURN. Idle units and tactical warnings do not block it; "
                    "top-level deferReason can optionally record why you left an action unused. "
                    "ACKNOWLEDGE_ALERT uses target, not handle. Copy the overview's alert/endTurn command. "
                    "RESPOND_CITY_STATE_ALERT uses an alert target and an offered response selection from the overview. "
                    "unit_details sections=[settlement] explains founding consequences; SET_UNIT_DESTINATION needs a matching "
                    "movement_preview(target,destination), whose command uses engine pathfinding. "
                    "Save optional private knowledge during play when useful context is absent from observations. "
                    "After END_TURN succeeds, finish immediately."
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
                    "inputSchema": (
                        MemoryContributions
                        if name == "memory_update" and purpose != "gameplay"
                        else tool_model(name, purpose)
                    ).model_json_schema(),
                }
            )
            if name in TOOL_OUTPUT_MODELS:
                tools[-1]["outputSchema"] = TOOL_OUTPUT_MODELS[name].model_json_schema()

        for name, description in {
            "unit_details": "Inspect one owned unit by its player_turn handle. Prefer explicit sections: movement (swaps/paths/orders; use movement_preview for distant destinations), combat (attacks), orders (rename, GUARD/SET_UP, ESCORT/STOP_ESCORT, EXPLORE/STOP_EXPLORATION, AUTOMATE/STOP_AUTOMATION, fortify/sleep/skip/wake), promotions (including saved city defaults), work (road connections and worker/water improvements; Work Boats are consumed), settlement (current-site native blockers, movement requirements and native promises; movement_preview also inspects prospective city sites), economy (upgrade/pillage/disband), abilities (great people/spaceship/gifts), or air (missions, bases/capacity, carried units and interception readiness; use mission_preview for special missions). Multiple sections are allowed; omitted sections or [all] returns full details. Only returned sections count as inspected. EXPLORE explores automatically; AUTOMATE uses native unit automation, including choosing and building improvements for Workers. These orders persist across turns; inspect orders to stop them. Movement captureRisk requires confirmAction=true to accept exposure for this move; a same-tile escort permits movement normally. Read availability, warnings and confirmations; copy an offered command; intent is optional. Receipts report actual effects.",
            "city_details": "Inspect one owned city by its player_turn handle. Prefer explicit sections: status (capture choices), production (queue/progress/options), citizens (growth/work tiles/specialists/focus), economy (yields/buildings/sales), purchases, or combat (bombardment). Multiple sections are allowed. Omitted sections or [all] returns full details. Each section retains native consequences; unrelated sections do not count as inspected. Prefer construction_details(names=[candidates]) for queueing/buying specific buildings or units. Copy offered commands; intent is optional.",
            "construction_details": "Compare construction in an owned city. Supply names and horizonTurns (1-100) for static build/purchase versus current-output comparisons, with assumptions and native blockers. following compares conversion now with building or buying then selecting that conversion, including native modifiers; endOfHorizon retains completed assets or unfinished work. queueOptions separates append, continue and reorder. Empire happiness and food effects are a separate immediate-completion preview. Omit names for a compact catalogue with native completion times, city-specific ongoing yield gains, maintenance and knownNames for the full ruleset. Unrecognized names return unknownNames with suggestions, separately from known but blocked choices. Supply up to 16 names for before/after yields, source changes, conditional effect descriptions, specialist slots, costs, eligibility, queue progress, purchase confirmations and copyable commands. Named victory projects and spaceship parts include native costs and required technologies/resources while still locked; their hypothetical build times exclude prerequisite research. Passive completion comparisons hold population and citizen assignments fixed; oneTime separately reports native completion grants. whileBuilding shows the immediate change from the current construction. Food is net surplus; Gold includes maintenance. Turns assume starting now, excluding earlier queue entries. Production conversions show their yield after modifiers. Read named options to compare choices; copy command with optional intent. Confirm purchases explicitly after reading consequences.",
            "map_region": "Read explored tiles and frontier within radius 0–4 of an observed coordinate, respecting world wrap. Unexplored tiles have no data. Explored tiles outside current sight retain permitted known or remembered information; check visibility and observation timestamps. Default format=compact: each tile inherits tileDefaults, with its explicit fields overriding those defaults, including null/false/empty values. All visible facts are retained; position, terrain, visibility, units and yields remain on each tile. Use format=full for expanded tile objects. Read player_details(map_index) to locate distant regions.",
            "player_details": "Read full economy and development opportunities, diplomacy (including war consequences), settlement candidates and scores, research progress, policy choices, current/previous turn receipts, or map coordinate index. Settlement scores are advice based on current/remembered knowledge; preview a route before travel. Uses the latest permitted snapshot; returned evidence identifies what was read. For CITY_STATE_ACTION inspect section=diplomacy: target=civilizationId, selection=available actions.name, and confirmAction=true only after accepting any confirmation. DECLARE_WAR also accepts city-states. Inspect receipt.cityState for actual influence and alliance changes.",
        }.items():
            if name in {"unit_details", "city_details"}:
                description += (
                    " Combat consequences put HP and damage-based remaining HP together; destructionPossible is advisory. "
                    "attackerConsumedOnAttack identifies self-destructing units independently of damage. "
                    "Early death, withdrawal or interception can change those estimates. Position contexts show current "
                    "visible proximity, not future enemy reach or guaranteed protection. Movement afterMove estimates "
                    "remaining movement and actions if the route completes; recheck after arrival."
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
            "Accept or decline the exact trade or friendship proposal you inspected with trade_read; an author can withdraw it. Copy a returned response; your private intent is optional. Acceptance applies all agreed terms together if still legal. Only ENACTED with an outcome confirms execution. Changed, expired or unavailable terms require a new decision.",
        )
        add(
            "history_read",
            "Inspect confirmed actions and detailed receipts from one earlier game turn when needed. Turn numbers are those shown by player_turn.",
        )
        add(
            "chat_read",
            "Read new messages, current social promises with their versions, pending trade IDs and active agreement summaries. conversationContext supplies bounded exact-audience excerpts, including confirmed sends, with source IDs and turns. Interpret replies alongside your private notes; use chat_history for older evidence. An effective older promise can coexist with a proposed revision; they are distinct versions. includeContext=true adds scenario, rules, economy and known diplomacy when you lack a turn overview. includeHistory=true includes the retained ledger; prefer chat_history, trade_read or agreement_read for selected older records. Inspect binding terms with trade_read before deciding. Social consent has no native game effect; interpret ambiguous promises freely.",
        )
        add(
            "chat_history",
            "Find earlier permitted messages and commitments. Returns the most recent 20 matches; narrow query for older evidence.",
        )
        add(
            "chat_send",
            "Send authenticated text to chatRecipients from chat_read. Use to for a direct message or recipients for a private group, excluding yourself; omit both for public chat. Reply to all with recipients equal to the message participants minus yourself. Each exact participant set has separate history; recipient order does not matter. Private messages notify every recipient; public mentions notify only mentioned players. Mentions must be in the audience. Groups carry text only: never combine recipients with to or nativeAction. Ordinary text has no game effects. Trade and peace use trade tools. "
            + "delivery=auto may return sent, queued or omitted with a deliveryId. Only a host receipt proves delivery. Use delivery=immediate to send now, or chat_delivery to override or cancel the original draft. "
            + (
                "Optional nativeAction executes DENOUNCE or ISSUE_DEMAND against a civilization (selection=type from native.availableDemandsToSend), or RESPOND_DIPLOMACY against an owned alert handle with an offered selection. Available demands are actions you may initiate, not incoming requests. Read player_turn first; these actions require your active turn. Inspect nativeDiplomacy sender, event, options, declaresWar and consequences before responding. Built-in AI only understands explicit native requests."
                if purpose == "gameplay"
                else ""
            ),
        )
        if routing_enabled:
            add(
                "chat_delivery",
                "Inspect an owned deliveryId, send its original text and audience immediately, or cancel an unsent draft. Repeating immediate never duplicates a delivered message. Only disposition=sent with a host receipt establishes delivery.",
            )
        if native_advice_enabled and purpose == "gameplay":
            add(
                "native_advice",
                "Read native next-action alternatives from your observed position. Optional cities/units filter owned handles. Read limitations, costs and prerequisites. Recommendations sharing a subject, destination or budget are alternatives; re-evaluate after each action. You decide whether to use them. This read neither applies actions nor simulates discoveries or combat.",
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
            "Read records by ID. Active records by default; includeRetired to inspect archived entries. Observations describe earlier facts; statements and notebook entries are assessments. After your turn ends, only information from that completed turn is available.",
        )
        add(
            "memory_search",
            "Search private records by type, subject, state or text, 20 per page. Active only by default. history=true explicitly reads reachable revisions as historical evidence, never active instructions. Use nextOffset for another page.",
        )
        add(
            "memory_update",
            (
                "Save private contributions with create for your next turn. Existing notebook entries are read-only during this exchange. "
                if purpose != "gameplay"
                else "Create, replace or retire a provisional strategy or next-turn handoff; at most one active record of each. "
                "Read before replacing; supply complete content, with no inherited fields. Handoffs are preloaded only "
                "on the following game turn; strategy ages from its own authoring turn. Notes and beliefs use amend "
                "for changed fields; omitted fields stay, lists replace. Communication contributions are immutable: "
                "use them to create/amend entries and retire them when handled. "
            )
            + "Keep knowledge, reasons and uncertainties; receipts preserve routine facts. "
            "Citations are optional historical references, at most 20 per list; "
            "unavailable references produce warnings and never become verified evidence. "
            "needsReview/reviewSources are advisory. Assessments do not authorize or prohibit actions. "
            "Retirement preserves history and does not cancel a promise.",
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
        if job.negotiation_pending.is_set():
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
            result, replayed = await self._call_once(job, name, arguments, call_id, startup=source == "startup")
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
            if name == "native_advice":
                # Advice explicitly names its observation epoch/revision. These
                # are evidence, not authority to bypass command validation.
                return result
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
        self, job: JobAccess, name: str, arguments: JsonObject, call_id: str, *, startup: bool = False
    ) -> tuple[JsonObject, bool]:
        self.check(job, ended_allowed=True)
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise ValueError("INVALID_ARGUMENT")
        if job.review_inputs is not None:
            return public_result(frozen_read(job.review_inputs, name, arguments)), False
        async with job.lock:
            self.check(job, ended_allowed=True)
            previous = await self.io.call(self.memory.call, job.id, call_id, name, arguments)
            if previous is not None:
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
                result = await self._call(job, name, arguments, call_id, startup=startup)
            result = await self.settle_gameplay(job, name, result)
            if self.native_advice_enabled:
                if name == "native_advice":
                    await self.io.call(experiment_metrics.exposure, self.memory, job, call_id, result, startup)
                elif name == "player_command":
                    await self.io.call(
                        experiment_metrics.action_match, self.memory, job, call_id, arguments, result, advice_revision
                    )
            result = await self.update_after_action(job, name, arguments, call_id, result)
            result = await self.io.call(self.memory.call, job.id, call_id, name, arguments, result)
            assert result is not None
            return result, receipt is not None

    @staticmethod
    def forget_observation(job: JobAccess) -> None:
        job.pending_invalidated.extend(query for query in job.details_read if query not in job.pending_invalidated)
        job.observation = None
        job.delivered_overview = None
        job.detail_revision = None
        job.details_read.clear()
        job.trades.clear()

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
        invalidated = list(job.pending_invalidated)
        invalidated.extend(
            query for query in changed_details(previous, value, job.details_read) if query not in invalidated
        )

        def respond(records, changed):
            current = stamp(
                {**compact, "strategyReview": strategy_review(records).model_dump()}, job.id, job.epoch, revision
            )
            result = (
                {**current, **({"invalidatedDetails": [q.model_dump() for q in invalidated]} if invalidated else {})}
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
                not call_id.removeprefix("review:").startswith("startup:"),
            )
        except BaseException:
            # A cancelled I/O await may still have committed the response. Replay
            # it from SQLite; never reuse a possibly older in-memory observation.
            self.forget_observation(job)
            raise
        job.memory["records"].update(observed)
        job.observation = value
        job.delivered_overview = (
            {key: part for key, part in result.items() if key not in {"memoryUpdates", "invalidatedDetails"}}
            if receipt is None
            else apply_overview_update(baseline, result["stateUpdate"])
        )
        job.details_read = [query for query in job.details_read if query not in invalidated]
        job.pending_invalidated.clear()
        for query in invalidated:
            if query.tool == "trade_read":
                job.trades.pop(query.arguments["proposalId"], None)
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
        if "error" in result:
            return result
        social_change = name in {"commitment_offer", "commitment_decide"}
        changes_game = (
            name == "player_command"
            or bool(result.get("nativeOutcome"))
            or (name in {"trade_offer", "friendship_offer", "trade_decide"} and bool(result.get("trade")))
        )
        if not changes_game and not social_change:
            return result
        if job.purpose != "gameplay":
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
                if job.suspend_for_negotiation and job.pending_calls:
                    # An earlier action's response may have reached the code
                    # cell but never been printed before it was interrupted.
                    # Rebase the settled response instead of referring to an
                    # overview delta the model might not have received.
                    job.delivered_overview = None
                    job.negotiation_calls.update(job.pending_calls)
                    job.negotiation_pending.set()
                waited = True
        finally:
            job.completed_wait += time.monotonic() - job.wait_started
            job.wait_started = None

    async def settle_gameplay(self, job: JobAccess, name: str, result: JsonObject) -> JsonObject:
        if job.purpose != "gameplay":
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
        social = await self.read_social(
            job,
            include_context=arguments.get("includeContext", False),
            include_history=arguments.get("includeHistory", False),
        )
        recent = await self.io.call(self.memory.snapshot, job.purpose)
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
        self, job: JobAccess, name: str, arguments: JsonObject, call_id: str, *, startup: bool = False
    ) -> JsonObject:
        self.check(job)
        arguments = copy.deepcopy(arguments)
        if not job.verified:
            identity = await job.connection.tool("player_communication", {"op": "state", "epoch": job.epoch})
            if identity.get("civilization") != self.memory.civilization or identity.get("epoch") != job.epoch:
                raise StateConflict("PLAYER_CREDENTIAL_MISMATCH")
            job.verified = True
        if BOOKKEEPING & arguments.keys():
            raise ValueError("Supply gameplay choices only; the service manages request bookkeeping.")
        if name == "memory_update":
            try:
                arguments = MemoryUpdate.model_validate(arguments).model_dump(exclude_unset=True)
            except ValidationError as error:
                return memory_argument_error(arguments, error)
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
        intent = arguments.pop("intent", None)
        if intent is not None:
            intent = validate(Intent, intent).model_dump()
        initiative_decisions = arguments.pop("initiativeDecisions", [])
        delivery = "auto"
        if name == "native_advice":
            if not self.native_advice_enabled or job.purpose != "gameplay":
                raise StateConflict("NATIVE_ADVICE_DISABLED")
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
                raise StateConflict("DIPLOMACY_ROUTING_DISABLED")
            return await self.routing.control(job, arguments)
        if name == "history_read":
            return public_result(
                await self.io.call(self.memory.history, arguments["turn"], arguments.get("offset", 0), job=job.id)
            )
        self.check(job)
        if name in DETAIL_MODELS:
            if job.observation is None:
                return self.reobserve_after_negotiation("Read player_turn before requesting details.")
            query = DetailQuery.model_validate({"tool": name, "arguments": arguments})
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
                version = await self.io.call(self.memory.observe, scope, value, job.id)
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
                raise StateConflict("TOOL_NOT_PERMITTED")
            if name in {"movement_preview", "mission_preview", "player_inspect"} and job.observation is None:
                return self.reobserve_after_negotiation("Read player_turn before requesting this inspection.")
            value = await job.connection.tool(name, arguments)
            while name == "player_turn" and job.purpose == "gameplay" and value.get("negotiation", {}).get("active"):
                await self.wait_for_gameplay(job)
                value = await job.connection.tool(name, arguments)
            self.check(job)
            if "error" not in value:
                add_decision_commands(name, value, arguments)
            if name == "player_turn" and "error" not in value:
                return await self.deliver_turn(job, value, name, arguments, call_id)
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
            return public_result(
                MemoryReadResult.model_validate(
                    {"records": [public_record(key, record) for key, record in chosen.items()]}
                ).model_dump(exclude_unset=True)
            )
        if name == "memory_search":
            result = await self.io.call(self.memory.search_memory, job.id, arguments)
            if not arguments.get("history"):
                chosen = await self.io.call(
                    self.memory.delivered_records,
                    job.id,
                    [r["id"] for r in result["records"]],
                )
                job.memory["records"].update(chosen)
            return public_result(MemorySearchResult.model_validate(result).model_dump(exclude_unset=True))
        if name == "memory_update":
            result = await self.io.call(self.memory.patch, job.id, arguments["changes"], call_id)
            for change in result["changes"]:
                if change["accepted"]:
                    record = change["record"]
                    job.memory["records"][change["id"]] = {
                        "version": change["version"],
                        "category": record["type"],
                        "body": record.get("body", {k: v for k, v in record.items() if k != "id"}),
                    }
            return MemoryUpdateResult.model_validate(result).model_dump(exclude_unset=True)
        if name == "player_command":
            if job.purpose != "gameplay":
                raise StateConflict("GAMEPLAY_NOT_PERMITTED")
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
            raise ValueError("TOOL_NOT_PERMITTED")
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
                self.forget_observation(job)
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
