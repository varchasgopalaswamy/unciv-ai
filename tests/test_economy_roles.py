"""Role barriers and ownership are independent of model timing and native turn order."""

import asyncio
from functools import wraps
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from civ_agent.agent_service import AgentService
from civ_agent.economy_roles import TurnRoles
from civ_agent.game_config import GameConfig, Player
from civ_agent.memory_records import MemoryReadResult, MemoryUpdateResult
from civ_agent.war_state import WarStore
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


@run_async
async def test_unchanged_native_construction_choices_return_to_main_for_explicit_decision():
    roles, jobs = coordinator()
    pending = [{"kind": "CHOOSE_CONSTRUCTION", "city": "city1"}]
    view = {"canEndTurn": False, "pending": pending, "turnControls": {"canDeferChoices": True}}
    roles.service.failure = None
    roles.service.stopping = False
    roles.service.gateway = SimpleNamespace(
        role_dispatch=asyncio.Lock(),
        wait_for_gameplay=AsyncMock(),
        fresh_turn=AsyncMock(return_value=view),
        call=AsyncMock(return_value={"kind": "END_TURN"}),
    )
    roles.ready.update(gameplay=True, economy=True)
    advancing = asyncio.create_task(roles.advance())
    try:
        roles.changed.set()
        message = await asyncio.wait_for(roles.wait_for_resume(jobs["economy"]), 1)
        assert "city1" in message and roles.ready == {"gameplay": True, "economy": False, "military": True}
        assert (await roles.tool(jobs["economy"], "finish_role", {}))["roleFinished"]
        message = await asyncio.wait_for(roles.wait_for_resume(jobs["gameplay"]), 1)
        assert "deferChoices=true" in message and "city_override" in message
        assert roles.ready == {"gameplay": False, "economy": True, "military": True}
        roles.service.gateway.call.assert_not_awaited()
        # The controller does not select deferral. Main must explicitly choose it.
        assert (
            await roles.tool(
                jobs["gameplay"],
                "finish_role",
                {"deferChoices": True, "deferReason": "Preserve current production until priorities are reassessed."},
            )
        )["roleFinished"]
        await asyncio.wait_for(advancing, 1)
        assert roles.committed
        assert roles.service.gateway.call.await_args.args[2]["deferChoices"] is True
        assert not roles.resumes["economy"].is_set()
    finally:
        advancing.cancel()
        await asyncio.gather(advancing, return_exceptions=True)


@pytest.mark.parametrize("boundary", ["committed", "ended"])
@run_async
async def test_negotiation_monitor_waiting_for_dispatch_stops_after_handoff(boundary):
    roles, jobs = coordinator()
    gateway = SimpleNamespace(role_dispatch=asyncio.Lock())
    roles.service.gateway = gateway
    connection = SimpleNamespace(tool=AsyncMock(return_value={"negotiation": {"active": False}}))
    jobs["gameplay"].connection = connection
    async with gateway.role_dispatch:
        monitor = asyncio.create_task(roles.watch_negotiation())
        await asyncio.sleep(0)
        if boundary == "committed":
            roles.committed = True
        else:
            jobs["gameplay"].gameplay_ended.set()
    try:
        await asyncio.wait_for(monitor, 1)
        connection.tool.assert_not_awaited()
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)


@run_async
async def test_negotiation_monitor_pauses_during_handoff_and_resumes_after_rejection():
    roles, jobs = coordinator()
    roles.service.gateway = SimpleNamespace(role_dispatch=asyncio.Lock())

    async def read(name, arguments):
        if name == "player_communication":
            return {"negotiation": {"active": False}}
        roles.committed = True
        return {}

    connection = SimpleNamespace(tool=AsyncMock(side_effect=read))
    jobs["gameplay"].connection = connection
    roles.committing = True
    monitor = asyncio.create_task(roles.watch_negotiation())
    try:
        await asyncio.sleep(0)
        connection.tool.assert_not_awaited()
        roles.committing = False
        await asyncio.wait_for(monitor, 2)
        assert [call.args[0] for call in connection.tool.await_args_list] == [
            "player_communication",
            "player_turn",
        ]
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)


@run_async
async def test_negotiation_monitor_failure_before_handoff_remains_fatal():
    roles, jobs = coordinator()
    roles.service.gateway = SimpleNamespace(role_dispatch=asyncio.Lock())
    jobs["gameplay"].connection = SimpleNamespace(tool=AsyncMock(side_effect=ValueError("HOST_UNAVAILABLE")))
    with pytest.raises(ValueError, match="HOST_UNAVAILABLE"):
        await roles.watch_negotiation()


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
async def test_capture_during_peacetime_military_work_reopens_main_for_disposition():
    roles, jobs = coordinator()
    roles.ready["gameplay"] = True
    assert not roles.military_active
    jobs["gameplay"].observation["alerts"] = [{"kind": "CAPTURED_CIVILIAN"}]
    await roles.after_action(jobs["gameplay"], {"kind": "ATTACK"}, {})
    assert not roles.ready["gameplay"]
    assert "capture requires" in roles.messages["gameplay"]


@run_async
async def test_roles_finish_independently_and_old_attempt_cannot_finish():
    roles, jobs = coordinator()
    await roles.tool(jobs["gameplay"], "finish_role", {})
    assert roles.ready == {"gameplay": True, "economy": False, "military": True}
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


@pytest.mark.parametrize("movement,reopen", [("2", True), ("2/2", True), ("0.5/2", True), ("0/2", False)])
@run_async
async def test_late_unit_purchase_reopens_main_player(movement, reopen):
    roles, jobs = coordinator()
    await roles.tool(jobs["gameplay"], "finish_role", {})
    await roles.after_action(
        jobs["economy"],
        {"kind": "BUY_CONSTRUCTION"},
        {"economy": {"units": [{"created": True, "handle": "unit1", "movement": movement}]}},
    )
    assert roles.ready["gameplay"] is not reopen
    assert jobs["gameplay"].role_finished.is_set() is not reopen
    if reopen:
        assert "purchased a unit" in await roles.wait_for_resume(jobs["gameplay"])


@pytest.mark.parametrize("movement,reopen", [("2/2", True), ("0.5/2", True), ("0/2", False)])
@run_async
async def test_new_unit_observation_reopens_its_owner_only_with_remaining_movement(monkeypatch, movement, reopen):
    roles, jobs = coordinator()
    await roles.tool(jobs["gameplay"], "finish_role", {})
    roles.wars = WarStore(Mock())
    monkeypatch.setattr(roles.wars, "synchronize", Mock(return_value={"episodes": {}, "escorts": {}}))
    roles.service.io = SimpleNamespace(call=AsyncMock(side_effect=lambda function, *args: function(*args)))
    roles.known_units = set()
    await roles.synchronize_war(
        {"diplomacy": {}, "units": [{"handle": "unit1", "military": False, "movement": movement}]}
    )
    assert roles.ready["gameplay"] is not reopen
    assert jobs["gameplay"].role_finished.is_set() is not reopen
    if reopen:
        assert "new unit has remaining actions" in await roles.wait_for_resume(jobs["gameplay"])


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


def test_city_plan_rejects_display_names_with_recovery_and_preserves_overrides(tmp_path):
    memory = setup(tmp_path)
    memory.start_job("econ", "economy", {}, game_turn=4)
    owners = {"city1": "economy", "city2": "gameplay"}

    def plan(city):
        result = memory.patch(
            "econ",
            [{"op": "create", "content": {"type": "city_plan", "city": city, "reasons": {"growth": "Feed citizens"}}}],
            city_owners=owners,
        )
        MemoryUpdateResult.model_validate(result)
        return result["changes"][0]

    rejected = plan("Rome (city1)")
    assert rejected["reason"] == "OWNED_CITY_HANDLE_REQUIRED"
    assert rejected["recovery"]["reads"][0]["tool"] == "player_turn"
    assert "without the city's display name" in rejected["instruction"]
    assert plan("city1")["accepted"]
    assert plan("city2")["reason"] == "CITY_OWNED_BY_OTHER_ROLE"
    memory.engine.dispose()


def test_omitted_city_plan_is_searchable_by_its_city_without_subject_tags(tmp_path):
    from tests.test_structured_memory import create

    memory = setup(tmp_path)
    memory.start_job("previous", "economy", {}, game_turn=4)
    plan = create(
        memory,
        {"type": "city_plan", "city": "city1", "subjects": ["Cuzco", "growth"], "reasons": {"growth": "Keep growing"}},
        "previous",
    )
    create(memory, {"type": "city_plan", "city": "city11", "reasons": {"growth": "Another city"}}, "previous")
    memory.finish_job("previous", True)
    memory.start_job("worker-pass", "economy", {}, game_turn=5)
    context = memory.select_startup_context("worker-pass", set(), owned_cities=[])
    assert plan not in context["contextIds"] and plan not in context["delivered"]
    result = memory.search_memory("worker-pass", {"type": "city_plan", "subject": "city1"})
    assert [r["id"] for r in result["records"]] == [plan]
    assert result["records"][0]["subjects"] == ["Cuzco", "growth"]
    assert memory.patch("worker-pass", [{"op": "amend", "id": plan, "fields": {"priority": 2}}])["changes"][0][
        "accepted"
    ]
    history = memory.search_memory("worker-pass", {"type": "city_plan", "subject": "city1", "history": True})
    assert len(history["records"]) == 2 and all(r["id"] == plan for r in history["records"])
    memory.engine.dispose()


def test_duplicate_city_plan_returns_exact_recovery_without_granting_unseen_read_credit(tmp_path):
    from tests.test_structured_memory import create, current

    memory = setup(tmp_path)
    memory.start_job("previous", "economy", {}, game_turn=4)
    content = {"type": "city_plan", "city": "city1", "reasons": {"growth": "Keep growing"}}
    plan = create(memory, content, "previous")
    memory.finish_job("previous", True)
    memory.start_job("worker-pass", "economy", {}, game_turn=5)
    memory.select_startup_context("worker-pass", set(), owned_cities=[])
    before = current(memory, plan)
    rejected = memory.patch("worker-pass", [{"op": "create", "content": content}], "duplicate-plan")
    MemoryUpdateResult.model_validate(rejected)
    result = rejected["changes"][0]
    assert result["reason"] == "CITY_PLAN_ALREADY_EXISTS" and result["recoverable"]
    read = result["recovery"]["reads"][0]
    assert read["tool"] == "memory_read" and read["arguments"]["ids"] == [plan]
    assert current(memory, plan) == before
    edit = [{"op": "amend", "id": plan, "fields": {"priority": 2}}]
    assert memory.patch("worker-pass", edit)["changes"][0]["reason"] == "MEMORY_CONFLICT"
    memory.refresh_memory("worker-pass", [plan])
    assert memory.patch("worker-pass", edit)["changes"][0]["accepted"]
    assert memory.patch("worker-pass", [{"op": "create", "content": content}], "duplicate-plan") == rejected
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
        assert service.records["turn1"]["roles"]["ready"] == {"gameplay": True, "economy": True, "military": True}
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
    assert following.overrides == {} and following.ready == {"gameplay": False, "economy": False, "military": True}


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


@run_async
async def test_chat_preserves_economy_observations_and_stale_city_plan_edits_request_refresh(tmp_path):
    from tests.test_diplomacy_contracts import opened

    async with opened(tmp_path, "gameplay") as (gateway, main, host):
        service = SimpleNamespace(records={}, _save=AsyncMock())
        roles = TurnRoles(service, {"epoch": "g1", "gameTurn": 0, "activationId": main.id})
        gateway.role_coordinator = roles
        roles.register(main)
        economy = await gateway.open("economy", "economy", "g1", host, {}, 0)
        roles.register(economy)
        await gateway.call(main, "player_turn", {}, "main-start")
        await gateway.call(economy, "player_turn", {}, "economy-start")
        sent = await gateway.call(main, "chat_send", {"to": "Greece", "text": "Peaceful greetings"}, "message")
        assert "error" not in sent
        assert economy.observation is not None
        created = await gateway.call(
            economy,
            "memory_update",
            {
                "changes": [
                    {
                        "op": "create",
                        "content": {"type": "city_plan", "city": "city1", "reasons": {"growth": "Keep the city fed"}},
                    }
                ]
            },
            "plan",
        )
        assert created["changes"][0]["accepted"]
        key = created["changes"][0]["id"]
        # A real gameplay change can still invalidate a role's observation.
        gateway.forget_observation(economy)
        arguments = {"changes": [{"op": "amend", "id": key, "fields": {"reasons": {"growth": "Grow while fed"}}}]}
        stale = await gateway.call(economy, "memory_update", arguments, "stale-plan")
        assert stale["error"] == "OBSERVATION_REQUIRED" and not stale["applied"]
        assert stale["recovery"]["reads"][0]["tool"] == "player_turn"
        await gateway.call(economy, "player_turn", {}, "refresh")
        revised = await gateway.call(economy, "memory_update", arguments, "revised-plan")
        assert revised["changes"][0]["accepted"]
        await roles.tool(main, "city_override", {"city": "city1", "reason": "Emergency defender"})
        denied = await gateway.call(economy, "memory_update", arguments, "overridden-plan")
        assert denied["changes"][0]["reason"] == "CITY_OWNED_BY_OTHER_ROLE"
        catalogue = await gateway.tools(economy)
        description = next(tool["description"] for tool in catalogue if tool["name"] == "memory_update")
        assert "amend" in description and "Existing notebook entries are read-only" not in description


@pytest.mark.parametrize("read_first", [True, False])
@run_async
async def test_sibling_actions_deliver_fresh_details_and_require_reconsideration_of_edits(tmp_path, read_first):
    from civ_agent.observations import DetailResult
    from civ_agent.state_updates import apply_overview_update
    from tests.test_diplomacy_contracts import opened

    async with opened(tmp_path, "gameplay") as (gateway, main, host):
        service = SimpleNamespace(records={}, _save=AsyncMock())
        roles = TurnRoles(service, {"epoch": "g1", "gameTurn": 0, "activationId": main.id})
        gateway.role_coordinator = roles
        roles.register(main)
        economy = await gateway.open("economy", "economy", "g1", host, {}, 0)
        roles.register(economy)
        await gateway.call(main, "player_turn", {}, "main-start")
        baseline = await gateway.call(economy, "player_turn", {}, "economy-start")
        await gateway.call(economy, "city_details", {"target": "city1"}, "old-city")
        # Model a changed city and a native command from the other role. The
        # city's old snapshot must never authorize a stale city-plan edit.
        host.view["cities"][0]["constructionQueue"] = ["Granary"]
        action = {"kind": "DISMISS_NOTIFICATIONS", "targets": ["n1"]}
        assert "error" not in await gateway.call(main, "player_command", action, "main-action")
        assert economy.observation is None and economy.role_refresh_required
        edit = {
            "changes": [
                {"op": "create", "content": {"type": "city_plan", "city": "city1", "reasons": {"growth": "Grow"}}}
            ]
        }
        if read_first:
            result = await gateway.call(economy, "city_details", {"target": "city1", "full": True}, "fresh-city")
            DetailResult.model_validate(result)
            assert "error" not in result and result["data"]["entity"]["constructionQueue"] == ["Granary"]
        else:
            result = await gateway.call(economy, "memory_update", edit, "stale-edit")
            assert result["error"] == "OBSERVATION_REQUIRED" and not result["applied"]
            assert not result["recovery"].get("reads")
            assert not any(r["type"] == "city_plan" for r in gateway.memory.search_memory(economy.id, {})["records"])
        update = result["stateUpdate"]
        assert update["kind"] == "delta"
        refreshed = apply_overview_update(baseline, update)
        assert refreshed["cities"][0]["constructionQueue"] == ["Granary"]
        assert update["invalidatedDetails"] == [{"tool": "city_details", "arguments": {"target": "city1"}}]
        assert not economy.role_refresh_required
        # Replaying the call returns its original update without extra actions.
        tool, args, call_id = (
            ("city_details", {"target": "city1", "full": True}, "fresh-city")
            if read_first
            else ("memory_update", edit, "stale-edit")
        )
        assert await gateway.call(economy, tool, args, call_id) == result
        assert len(host.executed) == 1
        created = await gateway.call(economy, "memory_update", edit, "reconsidered-edit")
        assert created["changes"][0]["accepted"]


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
