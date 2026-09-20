"""Job-scoped HTTPS tools. Only this trusted controller holds host credentials."""

import asyncio
import copy
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import aiohttp
from aiohttp import web
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState, StaleAssumptions, StateConflict
from civ_agent.game_commands import add_combat_commands, command_arguments, command_error, command_schema
from civ_agent.memory_records import (
    REQUIRED_COMMANDS,
    REQUIRED_TOOLS,
    Intent,
    MemoryReadResult,
    MemorySearchResult,
    MemoryUpdateResult,
    public_record,
)
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
from civ_agent.tactical_review import TacticalReviewRequired, strategy_review, tactical_review
from civ_agent.tool_schemas import TOOL_MODELS, TOOL_OUTPUT_MODELS

BOOKKEEPING = {"requestId", "epoch", "revision", "reviewId", "socialVersion"}
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
RECOVERY_INSTRUCTIONS = (
    "STALE_ASSUMPTIONS applied nothing: follow recovery.reads, reconsider only the affected action, "
    "then make a fresh decision or skip it if another job already handled it. "
    "If it conflicts again, defer that action and continue other work; do not stop the gameplay turn for this. "
)
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
    if isinstance(value, dict):
        return {
            key: public_result(
                {name: part for name, part in item.items() if name != "id"}
                if key == "turnReview" and isinstance(item, dict)
                else item
            )
            for key, item in value.items()
            if key not in BOOKKEEPING and key not in {"peaceVersion", "_evidence", "_assumptions", "_pinned"}
        }
    if isinstance(value, list):
        return [public_result(item) for item in value]
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

    def waiting_seconds(self) -> float:
        return self.completed_wait + (time.monotonic() - self.wait_started if self.wait_started is not None else 0)


class ProposalGateway:
    def __init__(self, memory: CivilizationState, io: ServiceIO, workspace: Path):
        self.memory, self.io, self.workspace = memory, io, workspace
        self.jobs: dict[str, JobAccess] = {}
        self.paused = False
        self.dispatch = asyncio.Lock()
        self.in_flight = 0
        self.deferred_completions = []

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
        await self.io.call(self.memory.bind, epoch)
        snapshot = await self.io.call(self.memory.start_job, id, purpose, settings, game_turn, memory_snapshot)
        job = JobAccess(id, purpose, epoch, connection, snapshot, owns_connection=owns_connection)
        self.jobs[job.token] = job
        return job

    async def close(self, job: JobAccess, completed: bool):
        job.closed = True
        self.jobs.pop(job.token, None)
        if job.owns_connection:
            await job.connection.close()
        if self.paused:
            self.deferred_completions.append((job.epoch, job.id, completed))
            return
        await self.io.call(self.memory.finish_job, job.id, completed)
        await self.io.call(self.memory.export, self.workspace)

    def check(self, job: JobAccess):
        if self.paused or job.closed or self.jobs.get(job.token) is not job:
            raise StateConflict("JOB_PAUSED_OR_EXPIRED")

    async def tools(self, job: JobAccess):
        upstream = await job.connection.list_tools()
        tools = []
        for tool in upstream:
            if tool["name"] in READ_TOOLS and (job.purpose == "gameplay" or tool["name"] in COMMUNICATION_READ_TOOLS):
                entry = copy.deepcopy(tool)
                if tool["name"] == "player_turn":
                    entry["description"] = (
                        "Read a compact current overview: tactics, strategy review, city/unit roster, pending choices and full alert/ordinary notification text. Notifications include category, icons and event locations. Read them, update private assessments as useful, then dismiss selected notice handles with DISMISS_NOTIFICATIONS. Read this overview after each action. Inspect targets with unit_details, city_details or construction_details for legal offers and confirmation consequences before acting; map_region and player_details provide other detail from this snapshot. Weigh socialAdvisories about possibly relevant accepted promises; they are not host judgments of breach and never restrict legal actions or require an extra confirmation. Use chat_read for other commitment terms."
                    )
                    entry["outputSchema"] = TurnOverview.model_json_schema()
                tools.append(entry)
            elif tool["name"] == "player_command" and job.purpose == "gameplay":
                entry = copy.deepcopy(tool)
                entry["inputSchema"] = command_schema()
                entry["description"] = (
                    "Propose one offered game action using the schema for its kind. Combat offers include a copyable command; add intent, never preview fields. CITY_ATTACK omits attackFrom; ATTACK requires it. Consequential choices require intent: a brief reason or an active plan ID; optional expectedOutcome and assumptions. The service records intent and handles request bookkeeping. For city-states, read player_details(section=diplomacy): CITY_STATE_ACTION uses target=civilizationId and selection=an available actions.name; confirmAction=true when confirmation is present. DECLARE_WAR also accepts city-states. Read unit_details.cityStateGift before GIFT_UNIT, or greatPerson.abilities before ACTIVATE_GREAT_PERSON trade missions. Inspect receipt.cityState for actual influence and alliance changes. For distant moves read movement_preview(target,destination), then SET_UNIT_DESTINATION with the same choices and intent; native orders resume at the start of your turns. Inspect unit_details.movementOrder or CANCEL_UNIT_MOVEMENT to stop. CREATE_WATER_IMPROVEMENT uses an available unit_details.waterImprovement name and consumes the Work Boat. Read receipt.movement and receipt.waterImprovement for actual effects. For air and special actions read unit_details.air for missions, native ranges, bases/capacity, carried units and automatic interception readiness. Call mission_preview(target,mission,destination) using REBASE, PARADROP, AIR_SWEEP or NUCLEAR_STRIKE, then issue REBASE_UNIT, PARADROP, AIR_SWEEP or NUCLEAR_STRIKE with matching target/destination and intent. Nuclear strikes require confirmAction=true after reading the preview consequences; visible victims and declarations may be incomplete. Ordinary air and guided-missile strikes use ATTACK; damage previews exclude interception. Read receipt.mission and combatReports for actual effects. Interception is automatic; preserve native readiness by leaving the unit unused or skipping it. For victory, inspect unit_details.spaceshipAssembly before ADD_SPACESHIP_PART (unit target). Read victory.ballot in player_turn: CAST_DIPLOMATIC_VOTE uses selection=candidate civilizationId; ABSTAIN_DIPLOMATIC_VOTE takes no other fields except intent. Read victory.votingResults before ACKNOWLEDGE_DIPLOMATIC_RESULTS, which has no target or selection. Re-read player_turn after each action and read full alerts and confirmations before acknowledging. DISMISS_NOTIFICATIONS takes targets=[notice handles] already read in player_turn; it clears only those pending messages and retains their contents in receipts. END_TURN requires reading turnReview and a deferReason for intentionally idle mobile units or deferred tactical concerns."
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
            "unit_details": "Read an owned unit's full current moves, combat previews, orders, promotions, worker jobs, abilities and confirmations. target is its handle in player_turn.units. Read again after an action.",
            "city_details": "Read one owned city's full development, citizen controls, construction, purchases, bombardment previews and confirmation text. target is its handle in player_turn.cities.",
            "construction_details": "Inspect construction in an owned city. Omit names for a compact catalogue; supply up to 16 names for descriptions, costs, eligibility, queue progress and purchase confirmations. Read the named option before queueing or buying.",
            "map_region": "Read explored tiles and frontier within radius 0–4 of an observed coordinate, respecting world wrap. Hidden tiles have no data. Read player_details(map_index) to locate distant regions.",
            "player_details": "Read full economy, diplomacy (including war consequences), research progress, policy choices, current/previous turn receipts, or map coordinate index. Details reuse the latest player_turn snapshot; refresh that after actions. Returned evidence identifies exactly what was read.",
        }.items():
            add(name, description)

        add(
            "trade_options",
            "Inspect trade terms, city names/populations, eligibility, unavailable reasons, research funding, friendship and active agreements with a met player. WarDeclaration names a third civilization both players know; give commits you to war and receive asks the partner to declare. Your own native declaration warnings and possibly relevant social promises are included. Weigh socialAdvisories when choosing; they never restrict legal actions. give/receive are from your perspective. City names in terms are short trade IDs. Available during either player's turn.",
        )
        add(
            "trade_read",
            "Read a host-owned binding trade before accepting, declining, withdrawing or counteroffering. Every clause has an explicit from/to direction, identical for both players. Pending deal IDs appear in chat_read. Interpret socialAdvisories yourself; they are reminders, not native restrictions or host judgments of breach. Re-read after changed terms; never poll.",
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
            "Publish exact binding terms from trade_options and your consent. Only the current player can initiate; invite them in chat otherwise. For human/agent partners the offer pauses all gameplay. For a built-in AI, NATIVE_AI requests stay pending until its turn and gameplay continues. Gameplay calls for shared negotiations return after settlement; communication jobs handle replies without waiting. Copy the offered name, amount and duration for Embassy, Agreement, City, Treaty and WarDeclaration. WarDeclaration in give makes YOU declare; in receive makes the PARTNER declare. Both sides can declare on the same target for joint war. Inspect your native declaration warnings. Social promises never restrict legal game actions. Treaties must match on both sides; at war include mutual Peace Treaty. Research Agreement amount is each player's signing cost after immediate gold transfers. give/receive are from YOUR perspective. Set proposal to counter a trade inspected with trade_read, within counteroffersRemaining. No effect until acceptance. Do not automatically reopen closed negotiations.",
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
            "Read compact current context (turn, your scenario instructions, economy and known diplomatic relations), messages, social commitments, binding trade IDs and enacted agreement summaries once. Inspect a binding proposal with trade_read before deciding, or agreement_read for enacted terms and ending history. Incoming private messages, trades, agreement changes and public mentions wake communication work automatically; never poll or wait for replies.",
        )
        add(
            "chat_history",
            "Find earlier permitted messages and commitments. Returns the most recent 20 matches; narrow query for older evidence. No polling is needed.",
        )
        add(
            "chat_send",
            "Send authenticated text. Omit to for public chat; mention players to notify them. Optional nativeAction explicitly executes DENOUNCE or ISSUE_DEMAND against target civilization, or RESPOND_DIPLOMACY against an owned alert handle with an offered selection. Read player_turn first: native diplomacy requires your active turn and current observations. Inspect nativeDiplomacy.options and consequences before responding. Ordinary text has no game effects. Built-in AI only understands explicit native requests. Trade and peace use trade tools.",
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
            "Create, revise, replace or retire typed private plans, beliefs, notes and SOUL principles. Code assigns IDs and turn metadata. Read before changing existing records. Retirement removes active context, retaining history; it does not cancel a promise. Sources must have been read: record/message IDs, observation:overview, a detail response's evidence ID, or observation:communication after chat_read. Assumptions with source are ongoing dependencies; sources alone are historical evidence. Revise a plan after reviewing changed assumptions before reusing it for intent. Use subjects such as economy, Greece or entity handles for relevant context selection.",
        )
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

    async def call(self, job: JobAccess, name: str, arguments: JsonObject, call_id: str) -> JsonObject:
        self.check(job)
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise ValueError("INVALID_ARGUMENT")
        async with job.lock:
            previous = await self.io.call(self.memory.call, job.id, call_id, name, arguments)
            if previous is not None:
                return previous
            # The action receipt may be durable even if a barrier interrupted
            # saving the MCP response. Replay it without needing a new observation.
            receipt = await self.io.call(self.memory.call_receipt, job.id, call_id)
            if receipt is not None:
                result = await self.settle_gameplay(job, name, public_result(receipt))
                await self.io.call(self.memory.call, job.id, call_id, name, arguments, result)
                return result
            try:
                result = await self._call(job, name, arguments, call_id)
            except StaleAssumptions as conflict:
                # Nothing was queued or sent. Cache the rejection too: replaying
                # this call after a read must never execute an old decision.
                reads: list[JsonObject] = []
                private = [key for key in conflict.keys if not key.startswith("observation:")]
                if private:
                    evidence = await self.io.call(self.memory.review_evidence, private)
                    for offset in range(0, len(private + evidence), 20):
                        reads.append(
                            {
                                "tool": "memory_read",
                                "arguments": {
                                    "ids": (private + evidence)[offset : offset + 20],
                                    "includeRetired": True,
                                },
                            }
                        )
                agreement_keys = [key for key in conflict.keys if key.startswith("observation:agreement/")]
                for key in agreement_keys:
                    reads.append({"tool": "agreement_read", "arguments": {"agreement": key.split("/", 1)[1]}})
                if "observation:agreements" in conflict.keys:
                    reads.append({"tool": "agreement_list", "arguments": {}})
                if any(
                    key.startswith("observation:") and key not in agreement_keys and key != "observation:agreements"
                    for key in conflict.keys
                ):
                    reads.append({"tool": "player_turn", "arguments": {}})
                if name in {"trade_offer", "trade_decide", "friendship_offer"}:
                    reads.append(
                        {"tool": "trade_read", "arguments": {"proposal": arguments["proposal"]}}
                        if "proposal" in arguments
                        else {"tool": "trade_options", "arguments": {"partner": arguments["partner"]}}
                    )
                result = {
                    "error": "STALE_ASSUMPTIONS",
                    "applied": False,
                    "recoverable": True,
                    "changedRecords": conflict.keys,
                    "recovery": {"reads": reads, "instruction": RECOVERY_INSTRUCTIONS},
                }
            result = await self.settle_gameplay(job, name, result)
            await self.io.call(self.memory.call, job.id, call_id, name, arguments, result)
            return result

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
                job.observation = None
                return {
                    **result,
                    "instruction": "Native request queued. Continue gameplay; the built-in AI decides on its turn. A later event reports the outcome. Do not wait or poll.",
                }
            await self.wait_for_gameplay(job)
            job.observation = None
            # Return the settled canonical terms, even when this is recovery of a lost offer response.
            final = await self._call(job, "trade_read", {"proposal": result["trade"]["proposal"]}, "settlement")
            return {
                **final,
                "outcome": final["trade"].get("outcome"),
                "instruction": "Negotiation closed. Read player_turn and reconsider gameplay using the resulting state.",
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
        self.check(job)
        arguments = copy.deepcopy(arguments)
        if not job.verified:
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
                "instruction": "Nothing was dispatched. Supply intent with a brief reason or an active plan ID.",
            }
        if name in TOOL_MODELS:
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
            return public_result(await self.io.call(self.memory.history, arguments["turn"], arguments.get("offset", 0)))
        self.check(job)
        if name in DETAIL_MODELS:
            if job.observation is None:
                return self.reobserve_after_negotiation("Read player_turn before requesting details.")
            query = DetailQuery.model_validate({"tool": name, "arguments": arguments})
            value = project_detail(job.observation, query)
            scope = detail_scope(query)
            version = await self.io.call(self.memory.observe, scope, value, job.id)
            job.memory["records"]["observation:" + scope] = {
                "version": version,
                "category": "observation",
                "body": native_observation(value),
            }
            if value["available"] and query not in job.details_read:
                job.details_read.append(query)
            return public_result(value)
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
            if name == "player_turn" and "error" not in value:
                job.observation = None
                revision = value.get("revision")
                if not isinstance(revision, str):
                    raise StateConflict("INVALID_HOST_REVISION")
                # Cache the permitted host snapshot; only the returned projection
                # is delivered evidence. Full details require an explicit read.
                value = {**value, "communication": await self.read_social(job, delivered=False)}
                add_combat_commands(value)
                if "map" in value and "combatReports" in value:
                    value = {"tacticalReview": tactical_review(value).model_dump(exclude_none=True), **value}
                compact = public_result(overview(value))
                compact.pop("strategyReview", None)  # Private plans are not game observations.
                observed = await self.io.call(self.memory.observe_player, value, job.id, compact)
                job.memory["records"].update(observed)
                job.observation = value
                if job.detail_revision != revision:
                    job.details_read.clear()
                job.detail_revision = revision
                latest = await self.io.call(self.memory.snapshot, job.purpose)
                value = {**compact, "strategyReview": strategy_review(latest["records"]).model_dump()}
            elif name in {"movement_preview", "mission_preview"} and "error" not in value:
                if value.get("revision") != job.detail_revision:
                    return self.reobserve_after_negotiation(
                        "The game changed. Read player_turn, then request a fresh preview."
                    )
                query = DetailQuery(tool=name, arguments=arguments)
                scope = detail_scope(query)
                value = {**value, "evidence": "observation:" + scope, "query": query.model_dump()}
                version = await self.io.call(self.memory.observe, scope, value, job.id)
                job.memory["records"]["observation:" + scope] = {
                    "version": version,
                    "category": "observation",
                    "body": native_observation(value),
                }
                if value.get("available") and query not in job.details_read:
                    job.details_read.append(query)
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
            await self.io.call(self.memory.export, self.workspace)
            return MemoryUpdateResult.model_validate(result).model_dump(exclude_unset=True)
        if name == "player_command":
            if job.purpose != "gameplay":
                raise StateConflict("GAMEPLAY_NOT_PERMITTED")
            if job.observation is None:
                raise StateConflict("Read player_turn and its full content before proposing another action.")
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
                tactics = job.observation.get("tacticalReview", {})
                concerns = [
                    key
                    for key in ("cityBombardments", "civiliansNearHostiles", "injuredMilitaryNearHostiles")
                    if tactics.get(key)
                ]
                if concerns and not arguments.get("deferReason", "").strip():
                    return TacticalReviewRequired.model_validate({"concerns": concerns}).model_dump()
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
                transport == "player_command"
                or name == "trade_decide"
                and value.get("outcome")
                or value.get("nativeOutcome")
            ) and "error" not in value:
                job.observation = None
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
            self.check(job)
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
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})
