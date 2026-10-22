"""Role barriers and ownership are independent of model timing and native turn order."""

import asyncio
from functools import wraps
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from civ_agent.agent_service import AgentService
from civ_agent.economy_roles import TurnRoles
from civ_agent.game_config import GameConfig, Player
from civ_agent.memory_records import MemoryReadResult, MemoryUpdateResult
from tests.test_agent_service import FakeCodex, FakeFactory
from tests.test_structured_memory import setup


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def coordinator():
    service = SimpleNamespace(_save=AsyncMock(), records={})
    record = {"epoch": "e1", "gameTurn": 4, "activationId": "attempt1"}
    roles = TurnRoles(service, record)
    jobs = {}
    for role in ("gameplay", "economy"):
        job = SimpleNamespace(
            purpose=role,
            epoch="e1",
            memory={"gameTurn": 4},
            observation={"cities": [{"handle": "city1"}]},
            pending_calls={},
            gameplay_ended=asyncio.Event(),
        )
        jobs[role] = job
        roles.register(job)
    return roles, jobs


def test_economy_model_inheritance_and_per_player_override():
    config = GameConfig()
    player = Player(
        name="One", model="custom-model", reasoning_effort="high", economy_model={"reasoning_effort": "medium"}
    )
    assert config.settings(player)["economy_model"] == {
        "provider": "openai",
        "model": "custom-model",
        "reasoning_effort": "medium",
    }
    with pytest.raises(ValidationError):
        Player(name="Native", controller="built_in_ai", economy_model={"model": "custom"})


@run_async
async def test_roles_finish_independently_and_old_attempt_cannot_finish():
    roles, jobs = coordinator()
    await roles.tool(jobs["gameplay"], "finish_role", {})
    assert roles.ready == {"gameplay": True, "economy": False}
    jobs["economy"].epoch = "abandoned"
    result = await roles.tool(jobs["economy"], "finish_role", {})
    assert result["error"] == "STALE_TURN_ATTEMPT"
    assert not roles.ready["economy"]


@run_async
async def test_override_fences_economy_without_taking_other_cities():
    roles, jobs = coordinator()
    action = {"kind": "SET_CITY_FOCUS", "target": "city1"}
    assert roles.command_problem(jobs["economy"], action) is None
    problem = roles.command_problem(jobs["gameplay"], action)
    assert problem and problem["error"] == "CITY_OWNED_BY_OTHER_ROLE"
    await roles.tool(jobs["gameplay"], "city_override", {"city": "city1", "reason": "Emergency defender"})
    assert roles.command_problem(jobs["gameplay"], action) is None
    problem = roles.command_problem(jobs["economy"], action)
    assert problem and problem["error"] == "CITY_OWNED_BY_OTHER_ROLE"
    assert roles.owner("city2") == "economy"


@run_async
async def test_late_unit_purchase_reopens_main_player():
    roles, jobs = coordinator()
    await roles.tool(jobs["gameplay"], "finish_role", {})
    await roles.after_action(
        jobs["economy"],
        {"kind": "BUY_CONSTRUCTION"},
        {"economy": {"units": [{"created": True, "handle": "unit1", "movement": "2"}]}},
    )
    assert not roles.ready["gameplay"]
    assert not jobs["gameplay"].role_finished.is_set()
    assert "purchased a unit" in await roles.wait_for_resume(jobs["gameplay"])


@run_async
async def test_urgent_request_reopens_completed_economy():
    roles, jobs = coordinator()
    await roles.tool(jobs["economy"], "finish_role", {})
    result = await roles.tool(jobs["gameplay"], "economy_request", {"text": "Buy an archer", "priority": "urgent"})
    assert result["request"]["status"] == "pending"
    assert not roles.ready["economy"]


def test_city_plan_is_durable_unique_and_economy_cannot_rewrite_strategy(tmp_path):
    memory = setup(tmp_path)
    memory.start_job("econ", "economy", {}, game_turn=4)
    result = memory.patch(
        "econ",
        [
            {
                "op": "create",
                "content": {
                    "type": "city_plan",
                    "city": "c1",
                    "reasons": {"construction": "Finish the granary to support growth"},
                    "reconsiderWhen": ["Granary completes"],
                    "subjects": ["c1"],
                },
            }
        ],
    )
    MemoryUpdateResult.model_validate(result)
    assert result["changes"][0]["accepted"]
    duplicate = memory.patch(
        "econ",
        [
            {
                "op": "create",
                "content": {"type": "city_plan", "city": "c1", "reasons": {"focus": "Inherited; not assessed"}},
            }
        ],
    )
    assert duplicate["changes"][0]["reason"] == "CITY_PLAN_ALREADY_EXISTS"
    denied = memory.patch(
        "econ", [{"op": "create", "content": {"type": "strategy", "assessment": "Change victory goal"}}]
    )
    assert denied["changes"][0]["reason"] == "ROLE_MEMORY_NOT_PERMITTED"
    record_id = result["changes"][0]["id"]
    read = memory.refresh_memory("econ", [record_id])
    assert record_id in read
    # The reader union exposes the new type using the same strict validation as other records.
    public = memory.search_memory("econ", {"type": "city_plan"})["records"]
    MemoryReadResult.model_validate({"records": public})
    memory.finish_job("econ", True)
    memory.start_job("econ-next", "economy", {}, game_turn=5)
    assert record_id in memory.snapshot("economy", 5)["records"]
    memory.engine.dispose()


@run_async
async def test_both_real_role_lifecycles_share_one_native_handoff(tmp_path):
    class FinishingCodex(FakeCodex):
        complete = False

        async def start(self, token, settings):
            await super().start(token, settings)
            self.job = self.factory.service.gateway.jobs[token]

        async def request(self, method, payload):
            result = await super().request(method, payload)
            if method == "turn/start":
                await self.factory.service.gateway.invoke(self.job, "finish_role", {}, "finish:" + result["turn"]["id"])
                await self.notification(
                    "turn/completed",
                    {"threadId": self.thread_id, "turn": {"id": result["turn"]["id"], "status": "completed"}},
                )
            return result

    class Factory(FakeFactory):
        def __call__(self, executable, workspace, mcp_url, ca_file, notification):
            process = FinishingCodex(self, workspace, notification)
            self.instances.append(process)
            return process

    factory = Factory()
    service = AgentService(
        "Rome",
        tmp_path / "workspace",
        tmp_path / "state",
        "S" * 43,
        "https://localhost:8765/mcp",
        None,
        process_factory=factory,
        connection_factory=factory.connection,
    )
    factory.service = service
    try:
        await service.start(
            {
                "activationId": "turn1",
                "epoch": "g1",
                "civilization": "Rome",
                "threadId": None,
                "gameTurn": 0,
                "handoff": 0,
                "mcpUrl": "https://localhost:8765/mcp",
                "playerToken": "P" * 43,
                "prompt": "Play",
                "model": None,
                "timeoutSeconds": 20,
                "leaseSeconds": 10,
            }
        )
        assert service.active_task is not None
        await asyncio.wait_for(asyncio.shield(service.active_task), 8)
        assert service.failure is None, service.status()
        assert service.turn_roles is not None and service.turn_roles.committed
        assert len(factory.instances) == 2
        assert {record.get("purpose", "gameplay") for record in service.records.values()} == {"gameplay", "economy"}
        assert service.records["turn1"]["roles"]["ready"] == {"gameplay": True, "economy": True}
    finally:
        await asyncio.wait_for(service.close(), 3)


@run_async
async def test_new_city_reopens_economy_and_routine_requests_survive_next_turn():
    roles, jobs = coordinator()
    await roles.tool(jobs["economy"], "finish_role", {})
    request = await roles.tool(jobs["gameplay"], "economy_request", {"text": "Plan a library", "priority": "routine"})
    assert roles.ready["economy"]
    jobs["gameplay"].observation["cities"].append({"handle": "city2"})
    await roles.after_action(jobs["gameplay"], {"kind": "FOUND_CITY"}, {})
    assert not roles.ready["economy"]
    assert "newly owned city" in await roles.wait_for_resume(jobs["economy"])
    roles.record["committedHandoff"] = 1
    roles.service.records = {"attempt1": roles.record}
    following = TurnRoles(roles.service, {"epoch": "e1", "gameTurn": 5, "activationId": "next"})
    assert following.requests[0]["id"] == request["request"]["id"]
    assert following.overrides == {} and not any(following.ready.values())


@run_async
async def test_shared_tile_reassignment_respects_both_city_owners():
    roles, jobs = coordinator()
    await roles.tool(jobs["gameplay"], "city_override", {"city": "city1", "reason": "Emergency"})
    jobs["economy"].observation["cities"].append(
        {
            "handle": "city2",
            "management": {"sharedTiles": [{"position": {"x": 1, "y": 1}, "workingCityHandle": "city1"}]},
        }
    )
    result = roles.command_problem(
        jobs["economy"], {"kind": "REASSIGN_CITY_TILE", "target": "city2", "destination": {"x": 1, "y": 1}}
    )
    assert result and result["error"] == "CITY_OWNED_BY_OTHER_ROLE"


def test_city_plan_confirmation_requires_a_successful_native_receipt(tmp_path):
    from tests.memory_helpers import stored_records
    from tests.test_structured_memory import create

    memory = setup(tmp_path)
    memory.start_job("econ", "economy", {}, game_turn=4)
    plan = create(memory, {"type": "city_plan", "city": "c1", "reasons": {"focus": "Build a granary sooner"}}, "econ")
    command = {"kind": "SET_CITY_FOCUS", "target": "c1", "selection": "Production"}
    failed = memory.queue(
        "econ", "player_command", command, "failed", {"cityPlan": plan, "reason": "Build a granary sooner"}
    )
    memory.receipt(failed["id"], {"error": {"code": "STALE_REVISION"}})
    assert not stored_records(memory)[plan]["body"].get("_confirmedActions")
    confirmed = memory.queue(
        "econ", "player_command", command, "confirmed", {"cityPlan": plan, "reason": "Build a granary sooner"}
    )
    memory.receipt(confirmed["id"], {"kind": "SET_CITY_FOCUS", "actionSummary": "Production focus selected."})
    saved = stored_records(memory)[plan]["body"]["_confirmedActions"]["SET_CITY_FOCUS"]
    assert saved["receipt"] == "receipt:" + confirmed["id"]
    assert saved["selection"] == "Production"
    memory.engine.dispose()


@run_async
async def test_economy_preload_includes_every_owned_city_plan_and_denies_diplomacy(tmp_path):
    import copy
    import json

    from civ_agent.civilization_state import CivilizationState
    from civ_agent.proposal_gateway import ProposalGateway
    from civ_agent.service_io import ServiceIO
    from civ_agent.startup_context import prepare_startup
    from tests.test_agent_service import StartupHost
    from tests.test_structured_memory import create

    memory, io = CivilizationState(tmp_path / "memory", "Rome", 1000), ServiceIO()
    memory.bind("g1")
    memory.start_job("previous", "economy", {}, game_turn=0)
    plans = {
        create(
            memory,
            {"type": "city_plan", "city": "city" + str(n), "reasons": {"focus": "Grow toward the next useful tile"}},
            "previous",
        )
        for n in range(1, 8)
    }
    lost = create(memory, {"type": "city_plan", "city": "lost", "reasons": {"focus": "Old plan"}}, "previous")
    memory.finish_job("previous", True)
    host = StartupHost(
        SimpleNamespace(service=SimpleNamespace(active_id="econ", records={"econ": {"epoch": "g1", "gameTurn": 1}}))
    )
    city = host.view["cities"][0]
    host.view["cities"] = [{**copy.deepcopy(city), "handle": "city" + str(n)} for n in range(1, 8)]
    gateway = ProposalGateway(memory, io, tmp_path / "workspace")
    role_service = SimpleNamespace(records={}, _save=AsyncMock(), failure=None, stopping=False)
    roles = TurnRoles(role_service, {"epoch": "g1", "gameTurn": 1, "activationId": "root"})
    gateway.role_coordinator = roles
    try:
        job = await gateway.open("econ", "economy", "g1", host, {}, 1)
        roles.register(job)
        prepared = await prepare_startup(gateway, job)
        records = json.loads(prepared["memoryContext"].split("\n", 1)[1])["records"]
        selected = {record["id"] for record in records}
        assert plans <= selected and lost not in selected
        assert memory.delivered_records(job.id, plans).keys() == plans
        denied = await gateway.call(job, "chat_send", {"to": "Greece", "text": "An offer"}, "denied")
        assert denied["error"] == "ROLE_ACTION_NOT_PERMITTED"
        assert "chat_send" not in {tool["name"] for tool in await gateway.tools(job)}
    finally:
        await io.close()
        memory.engine.dispose()


@pytest.mark.parametrize("failure", ["retry", "shutdown"])
@run_async
async def test_economy_retries_only_after_confirmed_shutdown(tmp_path, failure):
    from civ_agent.agent_service import ServiceError

    class Factory(FakeFactory):
        def __init__(self):
            super().__init__()
            self.economy_attempts = 0
            self.refuse_shutdown = failure == "shutdown"
            self.complete = failure == "retry"

        def __call__(self, executable, workspace, mcp_url, ca_file, notification):
            process = super().__call__(executable, workspace, mcp_url, ca_file, notification)
            if not process.economy:
                return process
            self.economy_attempts += 1
            first = self.economy_attempts == 1
            request, close = process.request, process.close

            async def failed_request(method, payload):
                if first and failure == "retry" and method == "turn/start":
                    raise ServiceError("TEST_PROVIDER_FAILED")
                return await request(method, payload)

            async def guarded_close():
                if self.refuse_shutdown:
                    raise ServiceError("TEST_STOP_FAILED")
                return await close()

            process.request, process.close = failed_request, guarded_close
            return process

    factory = Factory()
    service = AgentService(
        "Rome",
        tmp_path / "workspace",
        tmp_path / "state",
        "S" * 43,
        "https://localhost:8765/mcp",
        None,
        process_factory=factory,
        connection_factory=factory.connection,
    )
    factory.service = service
    payload = {
        "activationId": "main",
        "epoch": "g1",
        "civilization": "Rome",
        "threadId": None,
        "gameTurn": 0,
        "handoff": 0,
        "mcpUrl": service.mcp_url,
        "playerToken": "P" * 43,
        "prompt": "Play",
        "model": None,
        "timeoutSeconds": 20,
        "leaseSeconds": 10,
    }
    try:
        await service.start(payload)
        assert service.active_task is not None
        if failure == "retry":
            await asyncio.wait_for(asyncio.shield(service.active_task), 8)
            assert service.status()["phase"] == "COMPLETED", service.status()
            assert factory.economy_attempts == 2
            children = [record for record in service.records.values() if record.get("purpose") == "economy"]
            assert [record["phase"] for record in children] == ["FAILED", "COMPLETED"]
        else:
            async with asyncio.timeout(4):
                while service.status()["phase"] == "STARTING":
                    await asyncio.sleep(0.005)
            with pytest.raises(ServiceError, match="STOP_UNCONFIRMED"):
                await service.interrupt("main")
            assert service.status()["phase"] == "RUNNING"
            problem = service.health()["runFailure"]
            assert problem is not None and problem["error"] == "STOP_UNCONFIRMED"
            assert factory.economy_attempts == 1
            with pytest.raises(ServiceError, match="STOP_UNCONFIRMED"):
                await service.start({**payload, "activationId": "later"})
            assert service.economy_runtime is not None and service.economy_runtime.job.gameplay_fenced
    finally:
        factory.refuse_shutdown = False
        await service.close()
