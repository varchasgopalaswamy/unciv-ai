"""Production RoleAgent joins native player notes, role plans, and game authority."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from pydantic_ai.messages import ModelRequest, ToolReturnPart

from civ_agent.economy_roles import TurnRoles
from civ_agent.operator_connection import Operator
from civ_agent.orchestration.authored_documents import AuthoredDocuments
from civ_agent.orchestration.contracts import (
    GatewayJob,
    ModelSelection,
    Scope,
    TurnTicket,
)
from civ_agent.orchestration.coordinator import NativeCoordinator
from civ_agent.orchestration.host_effect_service import NativeCommandService
from civ_agent.orchestration.providers import ProviderClients
from civ_agent.orchestration.store import WorkflowStore
from civ_agent.runtime.player import LocalGateway
from tests.domain_fixtures import StartupHost


def identity(request):
    return GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)


def returns(messages, name):
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == name
    ]


class FixtureGateway(LocalGateway):
    """Production local stores plus the coordinator's ordinary command invoker."""

    async def __call__(self, deps, call_id, model_step, decision):
        return await self.root.registry(deps, call_id, model_step, decision)


@asynccontextmanager
async def production_stores(tmp_path, *, native_commands=False):
    store = WorkflowStore(tmp_path / "worker", player="p0")
    root = NativeCoordinator(
        game="game",
        player="p0",
        civilization="Rome",
        state_dir=tmp_path / "coordinator",
        store=store,
        operator=cast(Operator, SimpleNamespace()),
        host_ca=tmp_path / "unused-ca.pem",
    )
    database = tmp_path / "authored.sqlite3"
    root.configure_authored_memory(AuthoredDocuments(database))
    scope = Scope(game="game", player="p0", epoch="g1", activation="turn0")
    ticket = TurnTicket(scope=scope, turn=0, handoff=0, index=0)
    root.memory.bind("g1")
    store.bind_epoch("game", "g1")
    record = {
        "activationId": scope.activation,
        "epoch": scope.epoch,
        "gameTurn": 0,
        "settings": {},
        "roleJobs": {},
        "requests": {},
        "bootstraps": {},
        "ticket": ticket.model_dump(mode="json"),
    }
    root.record, root.records[scope.activation] = record, record
    root.game_state["request"] = {"selection": ModelSelection(model="test").model_dump(), "request_limit": 24}
    root.turn_roles = TurnRoles(root, record)
    root.gateway.role_coordinator = root.turn_roles
    root.turn_roles.overrides["city1"] = "Main is managing this city."
    host = StartupHost(SimpleNamespace(service=SimpleNamespace(records=root.records, active_id=scope.activation)))
    host.view["cities"][0]["constructionQueue"] = ["Worker"]
    second = copy.deepcopy(host.view["cities"][0])
    second.update(handle="city2", name="Antium", constructionQueue=["Granary"])
    host.view["cities"].append(second)
    root.load_turn = AsyncMock(return_value=host)
    gateway = FixtureGateway(root, database)
    authored, plans = gateway.memory, gateway.plans
    commands = NativeCommandService(root.registry, workflow_store=store, authored_store=authored.store)
    probes = []
    if native_commands:
        original_tool = host.tool

        async def native_host(name, args):
            if name == "player_receipt":
                probes.append(copy.deepcopy(args))
                previous = host.seen.get(args["requestId"])
                if previous is not None:
                    assert args in host.executed
                return copy.deepcopy(previous or {"error": "NOT_ENACTED"})
            result = await original_tool(name, args)
            if name == "player_command":
                result["epoch"] = args["epoch"]
                result["privateHostNote"] = "Only the host and Temporal retain this receipt."
                host.seen[args["requestId"]] = copy.deepcopy(result)
                assert args["kind"] == "SET_CITY_FOCUS" and args["target"] == "city1"
                host.view["cities"][0]["focus"] = args["selection"]
            return result

        host.tool = native_host
    clients = ProviderClients()
    try:
        yield SimpleNamespace(
            root=root,
            store=store,
            ticket=ticket,
            gateway=gateway,
            clients=clients,
            authored=authored,
            plans=plans,
            commands=commands,
            host=host,
            probes=probes,
        )
    finally:
        await clients.close()
        await root.close()
        store.close()
