"""Reusable model-free player transport and game-domain test fixtures."""

import asyncio
import copy
from contextlib import asynccontextmanager
from functools import wraps
from types import SimpleNamespace
from unittest.mock import AsyncMock

from civ_agent.civilization_state import CivilizationState
from civ_agent.economy_roles import TurnRoles
from civ_agent.economy_schedule import EconomySchedule
from civ_agent.memory_records import MemoryUpdateResult
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from tests.memory_helpers import stored_records


def command(kind, **arguments):
    return {"tool": "player_command", "arguments": {"command": {"kind": kind, **arguments}}}


class Host:
    def __init__(self):
        self.civilization = "Rome"
        self.revision = "rev1"
        self.offers = []
        self.consents = []
        self.executed = []
        self.seen = {}
        self.view: JsonObject = {
            "turn": 0,
            "canEndTurn": True,
            "constructionCatalogue": ["Granary", "Market", "Warrior"],
            "cities": [
                {
                    "handle": "city1",
                    "name": "Rome",
                    "constructionQueue": [],
                    "puppet": False,
                    "attacksRemaining": 1,
                    "constructionOptions": ["Market", "Granary"],
                }
            ],
            "map": {
                "units": [
                    {
                        "handle": "unit1",
                        "name": "Warrior",
                        "position": {"x": 0, "y": 0},
                        "health": 100,
                        "movement": "2",
                        "military": True,
                        "idle": True,
                        "movementSupported": True,
                        "canFoundCity": False,
                        "moves": [],
                        "combat": {"attacksRemaining": 1, "attacks": []},
                    }
                ]
            },
        }

    async def close(self):
        pass

    async def list_tools(self):
        return [
            {
                "name": "player_command",
                "inputSchema": {
                    "properties": {
                        key: {"type": "string"} for key in ("kind", "target", "requestId", "epoch", "revision")
                    },
                    "required": ["kind", "requestId", "epoch", "revision"],
                },
            }
        ]

    async def tool(self, name, args):
        if name == "player_turn":
            return {
                **copy.deepcopy(self.view),
                "epoch": "g1",
                "revision": self.revision,
                "phase": "ACTIVE",
                "turnReview": {"id": "review1"},
            }
        if name == "player_communication":
            if args["op"] == "negotiation":
                return {"negotiation": {"active": False}}
            if args["op"] == "trade_options":
                return {"partner": args["partner"], "mode": "SHARED", "give": [], "receive": []}
            if args["op"] == "state":
                return {
                    "civilization": self.civilization,
                    "epoch": "g1",
                    "offers": copy.deepcopy(self.offers),
                    "consents": copy.deepcopy(self.consents),
                    "outcomes": [],
                    "channels": ["Rome", "Greece", "France"],
                    "context": {"turn": self.view["turn"]},
                }
            return {"sent": True}
        if args["requestId"] in self.seen:
            return self.seen[args["requestId"]]
        self.executed.append(copy.deepcopy(args))
        self.revision = "rev" + str(len(self.executed) + 1)
        summary = {
            "END_TURN": "Ended turn.",
            "DISMISS_NOTIFICATIONS": f"Dismissed {len(args.get('targets', []))} ordinary notifications.",
        }.get(args["kind"], f"Executed {args['kind']} in test host.")
        result = {
            "requestId": args["requestId"],
            "revision": self.revision,
            "kind": args["kind"],
            "done": True,
            "actionSummary": summary,
        }
        self.seen[args["requestId"]] = result
        return result


class StartupHost(Host):
    """A permitted starting view for service lifecycle tests with a fake model."""

    def __init__(self, factory):
        super().__init__()
        self.factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def list_tools(self) -> list[JsonObject]:
        return [
            *await super().list_tools(),
            *[
                {"name": name, "description": name, "inputSchema": {"type": "object", "properties": {}}}
                for name in ("player_turn", "player_capabilities", "victory_progress")
            ],
        ]

    async def tool(self, name, args):
        record = self.factory.service.records[self.factory.service.active_id]
        if name == "player_capabilities":
            return {"protocolVersion": "test", "commands": ["END_TURN"], "queries": []}
        if name == "victory_progress":
            return {"turn": record["gameTurn"], "victories": []}
        result = await super().tool(name, args)
        if "epoch" in result:
            result["epoch"] = record["epoch"]
        if "turn" in result:
            result["turn"] = record["gameTurn"]
        return result


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def observation(turn=4):
    return {
        "epoch": "g1",
        "revision": "r1",
        "turn": turn,
        "phase": "ACTIVE",
        "canEndTurn": True,
        "cities": [
            {
                "handle": "city1",
                "name": "Rome",
                "puppet": False,
                "constructionQueue": ["Worker"],
                "constructionOptions": ["Worker", "Granary"],
                "constructionChoice": {"chosenTurn": 1},
                "development": {
                    "population": 2,
                    "unassignedPopulation": 0,
                    "foodStored": 5,
                    "turnsToGrowth": 4,
                    "starving": False,
                    "builtBuildings": ["Palace"],
                    "tiles": [
                        {"position": {"x": 0, "y": 0}, "worked": True, "yields": [{"stat": "Food", "amount": "2"}]}
                    ],
                },
                "management": {"specialists": [{"name": "Scientist", "slots": 0, "assigned": 0}]},
                "purchases": {"constructions": [{"name": "Granary", "available": False, "goldCost": 200}]},
            }
        ],
        "economy": {
            "treasury": 100,
            "availableGold": 100,
            "goldPerTurn": 5,
            "happiness": 4,
            "cityConnections": [{"name": "Rome", "connectedToCapital": True}],
        },
    }


def strategy(**changes):
    return {
        "type": "strategy",
        "assessment": "Defend Rome",
        "reasons": ["Hold the border"],
        "reconsiderWhen": ["Rome survives"],
        **changes,
    }


def replacement(memory, key, **changes):
    # Explicitly carry forward chosen fields in a complete replacement.
    from civ_agent.memory_records import CONTENT_MODELS

    old = current(memory, key)
    content = {name: old[name] for name in CONTENT_MODELS[old["type"]].model_fields if name in old}
    return {"op": "replace", "id": key, "content": {**content, **changes}}


def setup(tmp_path, limit=12000):
    memory = CivilizationState(tmp_path / "private", "Rome", limit)
    memory.bind("g1")
    memory.start_job("game", "gameplay", {}, game_turn=4)
    memory.observe_player({"turn": 4, "cities": [{"handle": "c1"}], "map": {"units": [{"handle": "u1"}]}}, "game")
    return memory


def create(memory, content, job="game"):
    result = memory.patch(job, [{"op": "create", "content": content}])
    MemoryUpdateResult.model_validate(result)
    assert result["changes"][0]["accepted"], result
    return result["changes"][0]["id"]


def current(memory, id):
    return stored_records(memory)[id]["body"]


@asynccontextmanager
async def managed(tmp_path):
    memory, io = CivilizationState(tmp_path / "memory", "Rome"), ServiceIO()
    record = {"activationId": "main", "epoch": "g1", "gameTurn": 0, "settings": {}}
    service = SimpleNamespace(
        memory=memory,
        io=io,
        records={"main": record},
        active_id="main",
        economy_interval=5,
        _save=AsyncMock(),
        state_dir=tmp_path,
        failure=None,
        stopping=False,
    )
    host = StartupHost(SimpleNamespace(service=service))
    first = host.view["cities"][0]
    first.update(constructionQueue=["Worker"], development={"population": 2, "unassignedPopulation": 0})
    second = copy.deepcopy(first)
    second.update(handle="city2", name="Antium", constructionQueue=["Granary"])
    host.view["cities"].append(second)
    gateway = ProposalGateway(memory, io, tmp_path / "workspace")
    service.gateway = gateway
    try:
        main = await gateway.open("main", "gameplay", "g1", host, {}, 0)
        await gateway.call(main, "player_turn", {}, "main-start")
        strategy = await gateway.call(
            main,
            "memory_update",
            {"changes": [{"op": "create", "content": {"type": "strategy", "assessment": "Develop both cities"}}]},
            "strategy",
        )
        seed = await gateway.open("seed", "economy", "g1", host, {}, 0)
        await gateway.call(seed, "player_turn", {}, "seed-start")
        plans = await gateway.call(
            seed,
            "memory_update",
            {
                "changes": [
                    {
                        "op": "create",
                        "content": {
                            "type": "city_plan",
                            "city": city,
                            "reasons": {"construction": "Keep the current queue"},
                        },
                    }
                    for city in ("city1", "city2")
                ]
            },
            "seed-plans",
        )
        await gateway.close(seed, True)
        view = await host.tool("player_turn", {})
        EconomySchedule(memory, 5).complete(view, 0)
        roles = TurnRoles(service, record)
        gateway.role_coordinator = roles
        roles.register(main)
        second["development"]["unassignedPopulation"] = 1
        host.revision = "review-city2"
        await roles.initialize_economy(await host.tool("player_turn", {}))
        assert roles.economy_status["cities"] == ["city2"] and not roles.economy_status["fullReview"]
        economy = await gateway.open("economy", "economy", "g1", host, {}, 0)
        roles.register(economy)
        yield SimpleNamespace(
            gateway=gateway,
            roles=roles,
            main=main,
            economy=economy,
            host=host,
            memory=memory,
            plans=[c["id"] for c in plans["changes"]],
            strategy=strategy["changes"][0]["id"],
        )
    finally:
        await io.close()
        memory.engine.dispose()
