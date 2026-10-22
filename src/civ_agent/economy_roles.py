"""Civilization-local role ownership and the durable native-turn completion barrier."""

import asyncio
import copy
import secrets
from typing import Annotated, Literal

from pydantic import Field

from civ_agent.memory_records import InitiativeDecision
from civ_agent.schemas import BoundaryModel

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

    def state(self):
        return {
            "ready": self.ready,
            "requests": self.requests,
            "overrides": self.overrides,
            "key": list(self.key),
            "committed": self.committed,
            "deferral": self.deferral,
        }

    def context(self):
        return {
            "rolesFinished": dict(self.ready),
            "requests": copy.deepcopy(self.requests),
            "cityOverrides": dict(self.overrides),
        }

    async def save(self):
        self.record["roles"] = copy.deepcopy(self.state())
        await self.service._save(self.record)

    def register(self, job):
        self.jobs[job.purpose] = job
        job.role_finished = asyncio.Event()

    def owner(self, city):
        return "gameplay" if city in self.overrides else "economy"

    def command_problem(self, job, arguments):
        role, kind = job.purpose, arguments["kind"]
        if self.ready[role]:
            return {"error": "ROLE_FINISHED", "applied": False}
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
                if job.purpose == "economy" and (
                    arguments.get("deferChoices")
                    or arguments.get("deferReason")
                    or arguments.get("initiativeDecisions")
                ):
                    return {"error": "MAIN_PLAYER_DECISION_REQUIRED", "applied": False}
                if job.purpose == "economy":
                    dispositions = {entry["id"]: entry for entry in arguments.get("requests", [])}
                    for request in self.requests:
                        if request["id"] in dispositions:
                            request.update(dispositions[request["id"]])
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
            if job.purpose != "gameplay" or self.ready["gameplay"]:
                return {"error": "ROLE_ACTION_NOT_PERMITTED", "applied": False}
            if name == "city_override":
                cities = (job.observation or {}).get("cities", [])
                if arguments["city"] not in {city["handle"] for city in cities}:
                    return {"error": "OWNED_CITY_REQUIRED", "applied": False}
                self.overrides[arguments["city"]] = arguments["reason"]
                await self.save()
                return {"overridden": arguments["city"], "owner": "gameplay", "until": "end_of_turn"}
            request = {
                "id": "request" + secrets.token_hex(5),
                **arguments,
                "status": "pending",
                "turn": self.record["gameTurn"],
                "author": "gameplay",
            }
            self.requests.append(request)
            if arguments["priority"] == "urgent" and self.ready["economy"]:
                self.resume("economy", "An urgent economic request requires another pass.")
            await self.save()
            return {"request": request}

    def resume(self, role, message):
        self.ready[role] = False
        self.jobs[role].role_finished.clear()
        self.messages[role] = message
        self.resumes[role].set()
        self.changed.set()

    async def after_action(self, job, arguments, result):
        if "error" in result or arguments.get("kind") == "END_TURN":
            return
        async with self.lock:
            current = {city["handle"] for city in (job.observation or {}).get("cities", [])}
            added = current - self.known_cities
            self.known_cities = current
            changed = False
            if added and self.ready["economy"]:
                self.resume(
                    "economy",
                    "A newly owned city needs an economic pass. Read fresh city facts and choose its initial plan.",
                )
                changed = True
            if arguments.get("kind") == "BUY_CONSTRUCTION":
                units = (result.get("economy") or {}).get("units", [])
                if (
                    any(unit.get("created") and float(unit.get("movement", "0")) > 0 for unit in units)
                    and self.ready["gameplay"]
                ):
                    self.resume(
                        "gameplay",
                        "The economy manager purchased a unit. Read its fresh facts and consider its remaining actions.",
                    )
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
                if not all(self.ready.values()) or len(self.jobs) != 2:
                    continue
            job = self.jobs["gameplay"]
            gateway = self.service.gateway
            await gateway.wait_for_gameplay(job)
            view = await gateway.call(job, "player_turn", {}, "host:review:" + str(self.record.get("handoffPass", 0)))
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
