"""Civilization-local role ownership and the durable native-turn completion barrier."""

import asyncio
import copy
import secrets
import time
from typing import Annotated, Literal

from pydantic import Field

from civ_agent.economy_schedule import request_city
from civ_agent.memory_records import InitiativeDecision
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel
from civ_agent.tls import write_private_json
from civ_agent.war_state import MILITARY_COMMANDS, WarStore, goal_versions, war_opponents

ECONOMY_WAIT_SECONDS = 300

CITY_COMMANDS = frozenset(
    {
        "QUEUE_CONSTRUCTION",
        "REMOVE_CONSTRUCTION",
        "MOVE_CONSTRUCTION",
        "BUY_CONSTRUCTION",
        "BUY_TILE",
        "SET_WORKED_TILE",
        "SET_TILE_LOCKED",
        "SET_CITY_FOCUS",
        "SET_AVOID_GROWTH",
        "SET_MANUAL_SPECIALISTS",
        "ASSIGN_SPECIALIST",
        "UNASSIGN_SPECIALIST",
        "RESET_CITIZENS",
        "SELL_BUILDING",
        "REASSIGN_CITY_TILE",
    }
)


ECONOMY_TOOLS = frozenset(
    {
        "player_turn",
        "player_capabilities",
        "player_command",
        "inspect_bundle",
        "command_help",
        "player_result",
        "city_details",
        "unit_details",
        "construction_details",
        "research_details",
        "policy_details",
        "victory_progress",
        "map_region",
        "player_inspect",
        "movement_preview",
        "mission_preview",
        "memory_read",
        "memory_search",
        "memory_update",
        "history_read",
        "chat_read",
        "finish_role",
        "native_advice",
    }
)


class RequestDisposition(BoundaryModel):
    id: str
    status: Literal["completed", "deferred", "rejected"]
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    reviewTurn: int | None = Field(default=None, ge=0)


class FinishRole(BoundaryModel):
    initiativeDecisions: list[InitiativeDecision] = Field(default_factory=list, max_length=2)
    requests: list[RequestDisposition] = Field(default_factory=list, max_length=100)
    deferChoices: bool = False
    deferReason: str = Field(default="", max_length=500)


class EconomyRequest(BoundaryModel):
    city: str | None = None
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    priority: Literal["routine", "urgent"] = "routine"


class CityOverride(BoundaryModel):
    city: Annotated[str, Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1, max_length=500)]


def _has_movement(unit):
    """Native movement displays remaining/maximum, for example 0.5/2."""
    return float(str(unit.get("movement", "0")).partition("/")[0]) > 0


class TurnRoles:
    def __init__(self, service, record):
        self.service = service
        self.record = record
        self.key = (record["epoch"], record["gameTurn"], record["activationId"])
        self.jobs = {}
        self.ready = {"gameplay": False, "economy": False}
        self.resumes = {role: asyncio.Event() for role in self.ready}
        self.messages = {}
        self.lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.committed = False
        self.committing = False
        previous = sorted(
            (
                r
                for r in service.records.values()
                if r is not record
                and r.get("purpose", "gameplay") == "gameplay"
                and r.get("epoch") == record["epoch"]
                and r.get("gameTurn", -1) < record["gameTurn"]
                and r.get("committedHandoff")
            ),
            key=lambda r: r["gameTurn"],
        )
        self.requests = copy.deepcopy(
            [
                request
                for request in (previous[-1].get("roles", {}).get("requests", []) if previous else [])
                if request["status"] in {"pending", "deferred"}
            ]
        )
        self.overrides = {}
        self.deferral = {}
        self.known_cities = set()
        self.native_call_id = None
        self.advance_task: asyncio.Task | None = None
        self.wars = WarStore(service.memory) if hasattr(service, "memory") else None
        self.war_state: JsonObject = {"episodes": {}, "escorts": {}}
        self.military_active = False
        self.military_tasks = []
        self.start_military = None
        self.known_units = None
        self.economy_start = asyncio.Event()
        self.economy_schedule = None
        self.economy_status = {}
        self.economy_revision = None
        self.economy_requested = False
        self.economy_observation = None
        self.economy_completions = 0
        self.economy_wait_record = None
        self.economy_wait_deadline = 0.0
        self.economy_changed = asyncio.Event()
        self.main_attention = 0

    def state(self):
        return {
            "ready": self.ready,
            "requests": self.requests,
            "overrides": self.overrides,
            "key": list(self.key),
            "committed": self.committed,
            "deferral": self.deferral,
            "militaryActive": self.military_active,
            "warState": copy.deepcopy(self.war_state),
            "economySchedule": copy.deepcopy(self.economy_status),
            "economyCompletions": self.economy_completions,
            "economyWait": copy.deepcopy(self.economy_wait_record),
        }

    def context(self):
        return {
            "rolesFinished": dict(self.ready),
            "requests": copy.deepcopy(self.requests),
            "cityOverrides": dict(self.overrides),
            "militaryActive": self.military_active,
            "warState": copy.deepcopy(self.war_state),
            "economySchedule": copy.deepcopy(self.economy_status),
        }

    async def save(self):
        self.record["roles"] = copy.deepcopy(self.state())
        await self.service._save(self.record)
        if self.economy_schedule is not None:
            await self.service.io.call(
                write_private_json,
                self.service.state_dir / "economy-status.json",
                {"epoch": self.key[0], "gameTurn": self.key[1], **copy.deepcopy(self.economy_status)},
            )
        self.economy_changed.set()

    def needs_attention(self, role):
        return self.ready.get(role, False) or (role == "gameplay" and self.economy_wait_record is not None)

    def economy_wait_outcome(self, wait):
        requests = {request["id"]: request for request in self.requests}
        ids = wait["requestIds"]
        if ids and all(requests.get(id, {}).get("status") in {"completed", "deferred", "rejected"} for id in ids):
            return "resolved"
        if self.main_attention != wait["attention"]:
            return "attention"
        if self.economy_completions != wait["completion"]:
            return "pass_finished"
        if self.ready["economy"]:
            return "not_running" if ids else "idle"
        return None

    async def economy_wait(self, job, arguments, call_id) -> JsonObject:
        """Register a wait; the gateway arms it only after persisting its receipt."""
        async with self.lock:
            if job.purpose != "gameplay":
                return {"error": "MAIN_PLAYER_DECISION_REQUIRED", "applied": False}
            if (
                self.jobs.get("gameplay") is not job
                or (job.epoch, job.memory.get("gameTurn"), self.record["activationId"]) != self.key
                or self.committed
                or self.committing
            ):
                return {"error": "STALE_TURN_ATTEMPT", "applied": False}
            if self.ready["gameplay"]:
                return {"error": "ROLE_FINISHED", "applied": False}
            if self.economy_wait_record is not None:
                return {"error": "ROLE_WAIT_ACTIVE", "applied": False}
            ids = arguments.get("requestIds", [])
            known = {request["id"] for request in self.requests}
            if any(id not in known for id in ids):
                return {"error": "UNKNOWN_ECONOMY_REQUEST", "applied": False}
            seen_messages = {
                message["id"]: message
                for message in ((getattr(job, "economy_seen", None) or {}).get("warState") or {}).get("messages", [])
            }
            if any(
                message.get("to") == "gameplay" and seen_messages.get(message["id"]) != message
                for message in self.war_state.get("messages", [])
            ):
                # The wait receipt carries fresh coordination. Let Main read a
                # question that arrived before registration instead of sleeping
                # while economy is waiting for its answer.
                return {"economyWait": {"id": call_id, "state": "attention", "requestIds": list(ids)}}
            wait = {
                "id": call_id,
                "requestIds": list(ids),
                "completion": self.economy_completions,
                "attention": self.main_attention,
            }
            outcome = self.economy_wait_outcome(wait)
            if outcome is not None:
                return {"economyWait": {"id": call_id, "state": outcome, "requestIds": list(ids)}}
            self.economy_wait_record = wait
            self.economy_wait_deadline = time.monotonic() + ECONOMY_WAIT_SECONDS
            await self.save()
            return {
                "economyWait": {
                    "id": call_id,
                    "state": "waiting",
                    "requestIds": list(ids),
                    "timeoutSeconds": ECONOMY_WAIT_SECONDS,
                },
                "instruction": "Inference will pause until economy responds or your attention is needed. Your role remains unfinished.",
            }

    def arm_economy_wait(self, job, result, *, replayed=False):
        """A replay can recover an unarmed registration, never resurrect a consumed wait."""
        receipt = result.get("economyWait", {})
        current = self.economy_wait_record
        if (
            job.purpose != "gameplay"
            or self.jobs.get("gameplay") is not job
            or current is None
            or receipt.get("state") != "waiting"
            or receipt.get("id") != current["id"]
            or job.role_wait_pending.is_set()
        ):
            return
        job.role_wait_calls.update(job.pending_calls)
        job.role_wait_pending.set()

    async def wait_for_economy(self, job):
        """Sleep outside gateway admission and dispatch; recheck after subscribing."""
        wait = self.economy_wait_record
        if wait is None:
            return "cancelled"
        while True:
            self.economy_changed.clear()
            self.service.gateway.check(job, ended_allowed=True)
            if self.service.failure:
                from civ_agent.agent_service import ServiceError

                raise ServiceError(self.service.failure)
            if self.committed or job.gameplay_ended.is_set():
                return "turn_ended"
            if job.negotiation_pending.is_set():
                return "negotiation"
            async with self.service.gateway.role_dispatch:
                outcome = self.economy_wait_outcome(wait)
            if outcome is not None:
                return outcome
            remaining = self.economy_wait_deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            changed = asyncio.create_task(self.economy_changed.wait())
            negotiated = asyncio.create_task(job.negotiation_pending.wait())
            ended = asyncio.create_task(job.gameplay_ended.wait())
            try:
                await asyncio.wait({changed, negotiated, ended}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (changed, negotiated, ended):
                    task.cancel()
                await asyncio.gather(changed, negotiated, ended, return_exceptions=True)

    async def finish_economy_wait(self, job, outcome):
        async with self.lock:
            wait = self.economy_wait_record
            self.economy_wait_record = None
            job.role_wait_pending.clear()
            # Attention delivered on this continuation is not a second request
            # to reopen Main after it subsequently finishes.
            self.resumes["gameplay"].clear()
            self.messages.pop("gameplay", None)
            self.record["lastEconomyWait"] = {**(wait or {}), "state": outcome}
            await self.save()
        return {"id": (wait or {}).get("id"), "state": outcome, "requestIds": (wait or {}).get("requestIds", [])}

    async def initialize_economy(self, view):
        from civ_agent.economy_schedule import EconomySchedule

        self.economy_schedule = EconomySchedule(self.service.memory, self.service.economy_interval)
        self.ready["economy"] = True
        self.known_cities = {city["handle"] for city in view.get("cities", [])}
        await self.synchronize_economy(view)
        await self.save()

    async def synchronize_economy(self, view):
        if self.economy_schedule is None or self.committed or self.committing:
            return
        if (
            view.get("phase") != "ACTIVE"
            or view.get("epoch") != self.key[0]
            or view.get("turn") != self.key[1]
            or (view.get("negotiation") or {}).get("active")
        ):
            return
        revision = (view.get("epoch"), view.get("turn"), view.get("revision"))
        if revision == self.economy_revision:
            return
        self.economy_revision = revision
        status = await self.service.io.call(self.economy_schedule.assess, view, self.key[1], self.requests)
        status["phase"] = "sleeping"
        if not self.ready["economy"]:
            if status["reasons"]:
                if self.extend_economy_scope(status["cities"], status["fullReview"], status["reasons"]):
                    await self.save()
            return
        if status["reasons"]:
            self.economy_status = copy.deepcopy(status)
            self.resume("economy", "; ".join(status["reasons"]), reasons=status["reasons"])
            await self.save()
            return
        elif self.economy_status.get("phase") == "finished":
            status["phase"] = "finished"
        if status != self.economy_status:
            self.economy_status = {**status, "phase": "running" if not self.ready["economy"] else status["phase"]}
            await self.save()

    def economy_arguments(self):
        return (
            {"cities": list(self.economy_status["cities"])}
            if not self.economy_status.get("fullReview", True) and "cities" in self.economy_status
            else {}
        )

    def extend_economy_scope(self, cities, full=False, reasons=()):
        previous = copy.deepcopy(self.economy_status)
        self.economy_status["fullReview"] = full or self.economy_status.get("fullReview", False)
        self.economy_status["cities"] = sorted(set(self.economy_status.get("cities", [])) | set(cities))
        self.economy_status["reasons"] = list(dict.fromkeys([*self.economy_status.get("reasons", []), *reasons]))
        job = self.jobs.get("economy")
        if job is not None and getattr(job, "city_scope", None) is not None:
            job.city_scope = self.economy_arguments().get("cities")
        return previous != self.economy_status

    def register(self, job):
        self.jobs[job.purpose] = job
        job.role_finished = asyncio.Event()
        job.role_wait_pending = asyncio.Event()
        job.role_wait_calls = {}
        if job.purpose == "economy":
            self.economy_observation = None
            # The initial wake is delivered in startup context. It must not
            # masquerade as a second request after the first pass finishes.
            self.resumes["economy"].clear()
            self.messages.pop("economy", None)

    async def synchronize_war(self, view):
        if self.committed or self.committing or self.wars is None or "diplomacy" not in view:
            return
        previous_episodes = set(self.war_state["episodes"])
        self.war_state = await self.service.io.call(self.wars.synchronize, self.key[0], view)
        added_episodes = set(self.war_state["episodes"]) - previous_episodes
        if added_episodes and self.needs_attention("gameplay"):
            self.resume("gameplay", "A new war needs your initial goals. Read war_goals and current observations.")
        units = {u["handle"]: u for u in view.get("units", (view.get("map") or {}).get("units", []))}
        if self.known_units is not None:
            for handle in units.keys() - self.known_units:
                owner = self.unit_owner(units[handle])
                if self.needs_attention(owner) and _has_movement(units[handle]):
                    self.resume(owner, "A new unit has remaining actions. Read its fresh facts before issuing orders.")
        self.known_units = set(units)
        active = bool(war_opponents(view))
        if active == self.military_active:
            return
        self.military_active = active
        if active:
            self.ready["military"] = False
            self.resumes["military"] = asyncio.Event()
            if self.start_military is not None:
                self.military_tasks.append(self.start_military())
        else:
            self.ready["military"] = True
            if "military" in self.jobs:
                self.jobs["military"].gameplay_ended.set()
            if self.needs_attention("gameplay"):
                self.resume("gameplay", "The final war ended. You now control all units; review remaining actions.")
        self.changed.set()
        await self.save()

    def unit_owner(self, unit):
        return (
            "military"
            if self.military_active and unit.get("military") and unit["handle"] not in self.war_state["escorts"]
            else "gameplay"
        )

    def unit_problem(self, job, arguments):
        role, kind = job.purpose, arguments["kind"]
        if role == "military" and (not self.military_active or kind not in MILITARY_COMMANDS):
            return {"error": "ROLE_ACTION_NOT_PERMITTED", "applied": False}
        if kind in {"AUTOPLAY", "MOVE_AUTOMATED_UNITS"}:
            return {"error": "ROLE_ACTION_NOT_PERMITTED", "applied": False}
        if kind == "CITY_ATTACK":
            owner = "military" if self.military_active else "gameplay"
            return {"error": "ACTION_OWNED_BY_OTHER_ROLE", "owner": owner, "applied": False} if role != owner else None
        units = {
            u["handle"]: u
            for u in (job.observation or {}).get("units", ((job.observation or {}).get("map") or {}).get("units", []))
        }
        affected = set()
        target = units.get(arguments.get("target"))
        if kind in MILITARY_COMMANDS and kind != "CITY_ATTACK" and target is None:
            return {"error": "OWNED_UNIT_REQUIRED", "applied": False}
        if target:
            affected.add(target["handle"])
            escort = target.get("escort") or {}
            if escort.get("active"):
                affected.add(escort["partner"])
            if kind == "SWAP_UNITS":
                offered = next(
                    (
                        swap
                        for swap in (target.get("controls") or {}).get("swaps", [])
                        if swap.get("destination") == arguments.get("destination")
                    ),
                    None,
                )
                if offered is not None:
                    affected.update(offered["partners"])
                else:
                    affected.update(
                        u["handle"]
                        for u in units.values()
                        if u.get("position") == arguments.get("destination")
                        and (escort.get("active") or u.get("military") == target.get("military"))
                    )
            if kind == "SET_UNIT_ORDER" and arguments.get("selection") == "ESCORT":
                affected.update(u["handle"] for u in units.values() if u.get("position") == target.get("position"))
            # Moving a carrier can move embarked aircraft with it.
            affected.update(
                u["handle"] for u in units.values() if (u.get("air") or {}).get("carrier") == target["handle"]
            )
        if role == "gameplay" and kind in {"NUCLEAR_STRIKE", "GIFT_UNIT"}:
            return None
        for handle in affected:
            if handle not in units or self.unit_owner(units[handle]) != role:
                return {
                    "error": "UNIT_OWNED_BY_OTHER_ROLE",
                    "unit": handle,
                    "owner": self.unit_owner(units[handle]) if handle in units else "unknown",
                    "applied": False,
                }
        return None

    async def role_tool(self, job, name, arguments, call_id) -> JsonObject:
        if self.wars is None:
            return {"error": "WAR_STORE_UNAVAILABLE", "applied": False}
        if self.jobs.get(job.purpose) is not job or self.committed or self.committing:
            return {"error": "STALE_TURN_ATTEMPT", "applied": False}
        if self.ready.get(job.purpose, True):
            return {"error": "ROLE_FINISHED", "applied": False}
        if name == "escort_assignment":
            if job.purpose != "gameplay":
                return {"error": "MAIN_PLAYER_DECISION_REQUIRED", "applied": False}
            if job.observation is None:
                return {
                    "error": "OBSERVATION_REQUIRED",
                    "applied": False,
                    "recoverable": True,
                    "recovery": {
                        "reads": [{"tool": "player_turn", "arguments": {}}],
                        "instruction": "Read player_turn before assigning or releasing escort duty, then reconsider the current units.",
                    },
                }
            units = {
                u["handle"]: u
                for u in (job.observation or {}).get(
                    "units", ((job.observation or {}).get("map") or {}).get("units", [])
                )
            }
            if not units.get(arguments["unit"], {}).get("military"):
                return {"error": "OWNED_MILITARY_UNIT_REQUIRED", "applied": False}
            if not arguments.get("release") and (
                arguments.get("civilian") not in units or units[arguments["civilian"]].get("military")
            ):
                return {"error": "OWNED_CIVILIAN_REQUIRED", "applied": False}
            if arguments.get("release") and (units[arguments["unit"]].get("escort") or {}).get("active"):
                return {"error": "UNPAIR_ESCORT_BEFORE_RELEASE", "applied": False}
        result = await self.service.io.call(
            self.wars.apply, job.epoch, job.purpose, name, arguments, job.id + ":" + call_id
        )
        self.war_state = await self.service.io.call(self.wars.state)
        if "error" not in result:
            targets = (
                [arguments["to"]]
                if name == "role_message"
                else (
                    [r for r in ("gameplay", "military") if r != job.purpose]
                    if name in {"war_goals", "escort_assignment"}
                    else []
                )
            )
            for role in targets:
                if (role in self.jobs or role == "economy") and self.needs_attention(role):
                    self.resume(
                        role,
                        "New internal coordination requires attention. Apply the next coordinationUpdate or full "
                        "economyCoordination snapshot before choosing an action. Read war_goals for additional history.",
                    )
            await self.save()
        return result

    def owner(self, city):
        return "gameplay" if city in self.overrides else "economy"

    def command_problem(self, job, arguments):
        role, kind = job.purpose, arguments["kind"]
        if self.ready[role]:
            return {"error": "ROLE_FINISHED", "applied": False}
        if role == "military" and getattr(job, "war_versions", {}) != goal_versions(self.war_state):
            job.war_versions = goal_versions(self.war_state)
            return {
                "error": "WAR_GOALS_CHANGED",
                "applied": False,
                "warState": copy.deepcopy(self.war_state),
                "instruction": "Reconsider your next action using these current accepted goals before issuing it again.",
            }
        problem = self.unit_problem(job, arguments)
        if problem:
            return problem
        if kind == "AUTOPLAY" or (role == "economy" and kind not in CITY_COMMANDS):
            return {"error": "ROLE_ACTION_NOT_PERMITTED", "applied": False}
        if kind == "REASSIGN_CITY_TILE":
            city = next(
                (
                    city
                    for city in (job.observation or {}).get("cities", [])
                    if city["handle"] == arguments.get("target")
                ),
                {},
            )
            tile = next(
                (
                    tile
                    for tile in (city.get("management") or {}).get("sharedTiles", [])
                    if tile.get("position") == arguments.get("destination")
                ),
                {},
            )
            if tile.get("workingCityHandle") and self.owner(tile["workingCityHandle"]) != role:
                return {
                    "error": "CITY_OWNED_BY_OTHER_ROLE",
                    "applied": False,
                    "owner": self.owner(tile["workingCityHandle"]),
                }
        if kind in CITY_COMMANDS and self.owner(arguments.get("target")) != role:
            return {
                "error": "CITY_OWNED_BY_OTHER_ROLE",
                "applied": False,
                "owner": self.owner(arguments.get("target")),
                "instruction": "Send an economy_request, or use an explicit city_override for an emergency.",
            }
        return None

    async def tool(self, job, name, arguments):
        async with self.lock:
            if job.purpose not in self.ready:
                return {"error": "ROLE_ACTION_NOT_PERMITTED", "applied": False}
            if (
                self.jobs.get(job.purpose) is not job
                or (job.epoch, job.memory.get("gameTurn"), self.record["activationId"]) != self.key
            ):
                return {"error": "STALE_TURN_ATTEMPT", "applied": False}
            if name == "finish_role":
                if job.purpose == "gameplay" and self.economy_wait_record is not None:
                    return {"error": "ROLE_WAIT_ACTIVE", "applied": False}
                if job.purpose == "gameplay" and any(
                    e["active"] and e["version"] == 0 for e in self.war_state["episodes"].values()
                ):
                    return {
                        "error": "INITIAL_WAR_GOALS_REQUIRED",
                        "applied": False,
                        "recoverable": True,
                        "opponents": [
                            e["opponent"]
                            for e in self.war_state["episodes"].values()
                            if e["active"] and e["version"] == 0
                        ],
                        "recovery": {
                            "reads": [{"tool": "war_goals", "arguments": {}}],
                            "instruction": "As Main, establish initial war_goals for each listed opponent with nonempty goals and a reason, then finish_role again.",
                        },
                    }
                if job.purpose != "gameplay" and (
                    arguments.get("deferChoices")
                    or arguments.get("deferReason")
                    or arguments.get("initiativeDecisions")
                ):
                    return {"error": "MAIN_PLAYER_DECISION_REQUIRED", "applied": False}
                if job.purpose == "economy":
                    view = {}
                    dispositions = {entry["id"]: entry for entry in arguments.get("requests", [])}
                    if any(
                        entry.get("reviewTurn") is not None and entry["reviewTurn"] <= self.key[1]
                        for entry in dispositions.values()
                    ):
                        return {"error": "FUTURE_REVIEW_TURN_REQUIRED", "applied": False}
                    if any(
                        request["status"] == "pending"
                        and request.get("priority") == "urgent"
                        and request["id"] not in dispositions
                        for request in self.requests
                    ):
                        return {
                            "error": "URGENT_REQUEST_DISPOSITION_REQUIRED",
                            "applied": False,
                            "instruction": "Include completed, deferred or rejected dispositions for pending urgent request IDs in finish_role.requests.",
                            "requests": copy.deepcopy(self.requests),
                        }
                    if self.economy_schedule is not None:
                        # Acknowledge the final native position, including this
                        # role's own actions, rather than its initial preload.
                        view = await job.connection.tool("player_turn", {})
                        if (
                            "error" in view
                            or view.get("phase") != "ACTIVE"
                            or view.get("epoch") != self.key[0]
                            or view.get("turn") != self.key[1]
                        ):
                            return {"error": "ECONOMY_REFRESH_REQUIRED", "applied": False}
                        await self.synchronize_economy(view)
                        if self.economy_observation is None or not await self.service.io.call(
                            self.economy_schedule.complete,
                            view,
                            self.key[1],
                            self.economy_observation,
                            cities=self.economy_status.get("cities", []),
                            full=self.economy_status.get("fullReview", True),
                        ):
                            return {
                                "error": "ECONOMY_OBSERVATION_CHANGED",
                                "applied": False,
                                "recoverable": True,
                                "recovery": {
                                    "reads": [{"tool": "player_turn", "arguments": self.economy_arguments()}],
                                    "instruction": "Economic facts changed since your last delivered view. Read player_turn and reconsider before finishing.",
                                },
                            }
                        self.economy_revision = (view.get("epoch"), view.get("turn"), view.get("revision"))
                    for request in self.requests:
                        if request["id"] in dispositions:
                            request.update(dispositions[request["id"]], reviewedTurn=self.key[1])
                    if self.economy_schedule is not None:
                        updated = await self.service.io.call(
                            self.economy_schedule.assess, view, self.key[1], self.requests
                        )
                        self.economy_status.update(
                            phase="finished",
                            lastReviewTurn=updated["lastReviewTurn"],
                            lastFullReviewTurn=updated["lastFullReviewTurn"],
                            cityReviewTurns=updated["cityReviewTurns"],
                            nextReviewTurn=updated["nextReviewTurn"],
                            pendingReasons=updated["pendingReasons"],
                            pendingCities=updated["pendingCities"],
                            reviewedCities=list(self.economy_status.get("cities", [])),
                        )
                if job.purpose == "economy":
                    self.economy_completions += 1
                if not self.known_cities:
                    self.known_cities = {city["handle"] for city in (job.observation or {}).get("cities", [])}
                self.ready[job.purpose] = True
                if job.purpose == "gameplay":
                    self.deferral = {
                        k: v
                        for k, v in arguments.items()
                        if k in {"deferChoices", "deferReason", "initiativeDecisions"}
                    }
                await self.save()
                job.role_finished.set()
                self.changed.set()
                return {
                    "roleFinished": True,
                    "role": job.purpose,
                    "instruction": "Stop issuing actions unless the host requests another pass.",
                }
            if job.purpose not in {"gameplay", "military"} or self.ready[job.purpose]:
                return {"error": "ROLE_ACTION_NOT_PERMITTED", "applied": False}
            if name == "city_override":
                if job.purpose != "gameplay":
                    return {"error": "MAIN_PLAYER_DECISION_REQUIRED", "applied": False}
                cities = (job.observation or {}).get("cities", [])
                if arguments["city"] not in {city["handle"] for city in cities}:
                    return {"error": "OWNED_CITY_REQUIRED", "applied": False}
                self.overrides[arguments["city"]] = arguments["reason"]
                await self.save()
                return {"overridden": arguments["city"], "owner": "gameplay", "until": "end_of_turn"}
            for existing in self.requests:
                if (
                    existing["status"] == "pending"
                    and existing.get("city") == arguments.get("city")
                    and existing["text"] == arguments["text"]
                ):
                    if arguments.get("priority") == "urgent" and existing.get("priority", "routine") != "urgent":
                        existing["priority"] = "urgent"
                        self.include_economy_request(job, existing)
                        await self.save()
                    return {"request": copy.deepcopy(existing), "alreadyPending": True}
            request = {
                "id": "request" + secrets.token_hex(5),
                **arguments,
                "status": "pending",
                "turn": self.record["gameTurn"],
                "author": job.purpose,
            }
            self.requests.append(request)
            self.include_economy_request(job, request)
            await self.save()
            return {"request": request}

    def include_economy_request(self, job, request):
        if request.get("priority", "routine") != "urgent" and self.ready["economy"]:
            return
        if self.ready["economy"]:
            self.economy_status.update(cities=[], fullReview=False, reasons=[])
        view = job.observation or (self.jobs.get("gameplay").observation if self.jobs.get("gameplay") else {}) or {}
        target = request_city(view, request.get("city"))
        self.extend_economy_scope(
            [target] if target else [city["handle"] for city in view.get("cities", [])],
            target is None,
            ["Economic request requires review: " + request["id"]],
        )
        if self.ready["economy"]:
            self.resume("economy", "An urgent economic request requires another pass.", scoped=True)

    def resume(self, role, message, *, reasons=None, scoped=False):
        if role == "military" and not self.military_active:
            # Durable war records retain messages for the next activation. An
            # exited military conversation cannot satisfy this turn's barrier.
            return
        self.ready[role] = False
        if role == "gameplay":
            self.main_attention += 1
        if role in self.jobs:
            self.jobs[role].role_finished.clear()
        if role == "economy":
            self.economy_requested = True
            self.economy_start.set()
            self.economy_status.update(phase="running")
            if reasons is None and not scoped:
                view = (self.jobs.get("gameplay").observation if self.jobs.get("gameplay") else {}) or {}
                self.extend_economy_scope([city["handle"] for city in view.get("cities", [])], full=True)
            if reasons is not None:
                self.economy_status["reasons"] = list(reasons)
            elif message not in self.economy_status.get("reasons", []):
                self.economy_status.setdefault("reasons", []).append(message)
        self.messages[role] = message
        self.resumes[role].set()
        self.changed.set()

    async def after_action(self, job, arguments, result):
        if "error" in result or arguments.get("kind") == "END_TURN":
            return
        if job.observation:
            await self.synchronize_war(job.observation)
            await self.synchronize_economy(job.observation)
        async with self.lock:
            current = {city["handle"] for city in (job.observation or {}).get("cities", [])}
            added = current - self.known_cities
            self.known_cities = current
            changed = False
            if added and self.ready["economy"] and self.economy_schedule is None:
                self.resume(
                    "economy",
                    "A newly owned city needs an economic pass. Read fresh city facts and choose its initial plan.",
                )
                changed = True
            if arguments.get("kind") == "BUY_CONSTRUCTION":
                units = (result.get("economy") or {}).get("units", [])
                owned = {u["handle"]: u for u in (job.observation or {}).get("map", {}).get("units", [])}
                for unit in units:
                    if not unit.get("created") or not _has_movement(unit):
                        continue
                    owner = self.unit_owner(owned[unit["handle"]]) if unit["handle"] in owned else "gameplay"
                    if self.needs_attention(owner):
                        self.resume(
                            owner,
                            "The economy manager purchased a unit. Read its fresh facts and consider its remaining actions.",
                        )
                        changed = True
            if (
                self.military_active
                and self.needs_attention("gameplay")
                and "CAPTURE" in str((job.observation or {}).get("alerts", {})).upper()
            ):
                self.resume("gameplay", "A capture requires your city disposition decision. Read the alerts.")
                changed = True
            if changed:
                await self.save()

    async def wait_for_resume(self, job):
        resumed = asyncio.create_task(self.resumes[job.purpose].wait())
        ended = asyncio.create_task(job.gameplay_ended.wait())
        try:
            await asyncio.wait({resumed, ended}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            resumed.cancel()
            ended.cancel()
            await asyncio.gather(resumed, ended, return_exceptions=True)
        self.resumes[job.purpose].clear()
        return self.messages.pop(job.purpose, "Continue from fresh observations.")

    async def watch_negotiation(self):
        while not self.committed:
            job = self.jobs["gameplay"]
            async with self.service.gateway.role_dispatch:
                # A queued poll can acquire the lock after END_TURN changed the
                # native player. Stop before reading that completed activation.
                if self.committed or job.gameplay_ended.is_set():
                    return
                # The native handoff and its durable receipt span separate
                # dispatch sections. Resume polling if the handoff is rejected.
                if not self.committing:
                    state = await job.connection.tool(
                        "player_communication", {"op": "negotiation", "epoch": job.epoch, "waitSeconds": 0}
                    )
                    active = state.get("negotiation", {}).get("active")
                    if "error" in state or type(active) is not bool:
                        raise ValueError("NEGOTIATION_STATUS_UNAVAILABLE")
                    if active:
                        for role, participant in self.jobs.items():
                            if not self.ready[role]:
                                self.service.gateway.suspend_negotiation(participant)
                    view = await job.connection.tool("player_turn", {})
                    if "error" not in view:
                        await self.synchronize_war(view)
                        await self.synchronize_economy(view)
            await asyncio.sleep(0.5)

    async def advance(self):
        """Only this trusted controller can turn role completion into a native handoff."""
        while not self.committed:
            await self.changed.wait()
            self.changed.clear()
            if not all(self.ready.values()):
                continue
            while any(job.pending_calls for job in self.jobs.values()):
                await asyncio.sleep(0.01)
            async with self.lock:
                required = self.ready.keys() - ({"economy"} if not self.economy_requested else set())
                if not all(self.ready.values()) or not required <= self.jobs.keys():
                    continue
            job = self.jobs["gameplay"]
            gateway = self.service.gateway
            await gateway.wait_for_gameplay(job)
            view = await gateway.call(
                job,
                "player_turn",
                {"delivery": "full"},
                "host:review:" + str(self.record.get("handoffPass", 0)),
                source="host",
            )
            async with self.service.gateway.role_dispatch:
                await self.synchronize_war(job.observation or {})
                await self.synchronize_economy(job.observation or {})
            async with self.lock:
                if not all(self.ready.values()):
                    continue
                if view.get("canEndTurn") is False and not self.deferral.get("deferChoices"):
                    pending = view.get("pending", [])
                    role = "economy" if any("CONSTRUCTION" in str(entry).upper() for entry in pending) else "gameplay"
                    self.resume(
                        role,
                        "The host's final review still has required decisions. Read player_turn, resolve them, then finish_role again.",
                    )
                    self.record["handoffPass"] = self.record.get("handoffPass", 0) + 1
                    await self.save()
                    continue
                if self.service.failure or self.service.stopping:
                    return
                self.committing = True
            result = await gateway.call(
                job,
                "player_command",
                {"kind": "END_TURN", **self.deferral},
                self.native_call_id or "host:end-turn:" + str(self.record.get("handoffPass", 0)),
                source="host",
            )
            async with self.lock:
                if "error" in result:
                    self.committing = False
                    self.record["handoffPass"] = self.record.get("handoffPass", 0) + 1
                    self.resume(
                        "gameplay",
                        "The final position changed. Read player_turn and reconsider the remaining decisions.",
                    )
                    await self.save()
                    continue
                self.committed = True
                for participant in self.jobs.values():
                    participant.gameplay_ended.set()
                await self.save()
