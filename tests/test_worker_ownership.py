"""Worker authority, advertised commands and escort handoffs agree."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from civ_agent.economy_roles import TurnRoles
from civ_agent.role_view import label_commands, role_overview
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import decision_queries
from civ_agent.unit_ownership import unit_owner
from tests.domain_fixtures import managed, run_async
from tests.test_military_roles import position, store


def test_worker_capability_and_linked_moves_define_authority():
    roles = TurnRoles(SimpleNamespace(records={}), {"epoch": "e1", "gameTurn": 4, "activationId": "main"})
    raw = position()
    worker = raw["map"]["units"][2]
    worker["worker"] = {}
    move = {"kind": "MOVE_UNIT", "target": "worker", "destination": {"x": 2, "y": 0}}
    for role in ("gameplay", "economy", "military"):
        job = SimpleNamespace(purpose=role, observation=raw)
        assert (roles.ownership_problem(job, move) is None) == (role == "economy")
        access = label_commands({"command": move}, roles, role, raw)["commandAccess"]
        assert access["owner"] == "economy"
        assert "emergencyOverride" not in access
    assert roles.unit_owner({"handle": "great-person", "worker": None}) == "gameplay"
    assert roles.unit_owner({"handle": "named-worker", "name": "Worker"}) == "gameplay"
    worker["escort"] = {"active": True, "partner": "soldier"}
    raw["map"]["units"][0]["escort"] = {"active": True, "partner": "worker"}
    assert roles.ownership_problem(SimpleNamespace(purpose="economy", observation=raw), move)["owner"] == "gameplay"
    assert roles.ownership_problem(SimpleNamespace(purpose="gameplay", observation=raw), move) is None


def test_worker_details_follow_handoffs_in_focused_views():
    roles = TurnRoles(SimpleNamespace(records={}), {"epoch": "e1", "gameTurn": 4, "activationId": "main"})
    worker = {
        "handle": "worker",
        "military": False,
        "canBuildImprovements": True,
        "inspections": {
            "START_WORKER_JOB": {"tool": "unit_details", "arguments": {"target": "worker", "sections": ["work"]}}
        },
    }
    for state, owner in (({}, "economy"), ({"soldier": {"civilian": "worker"}}, "gameplay")):
        roles.war_state["escorts"] = state
        assert unit_owner(worker, roles.war_state) == owner
        for view, role in (("main", "gameplay"), ("economy", "economy"), ("military", "military")):
            brief = role_overview({"units": [worker]}, view, roles=roles, purpose=role)
            assert bool(decision_queries(brief, role)) == (role == owner)


@run_async
async def test_gateway_advertises_worker_commands_and_preloads_work_in_targeted_pass(tmp_path):
    from civ_agent.startup_context import prepare_startup

    async with managed(tmp_path) as m:
        worker = m.host.view["map"]["units"][0]
        worker.update(
            name="Worker", military=False, worker={"currentJob": None, "options": [{"name": "Farm", "available": True}]}
        )
        m.host.revision = "worker-added"
        catalogue = await m.gateway.tools(m.economy)
        kinds = next(t for t in catalogue if t["name"] == "player_command")["inputSchema"]["discriminator"]["mapping"]
        assert {"START_WORKER_JOB", "STOP_WORKER_JOB", "CONNECT_ROAD", "MOVE_UNIT"} <= set(kinds)
        prepared = await prepare_startup(m.gateway, m.economy)
        brief = prepared["reads"]["player_turn"]
        assert brief["units"][0]["canBuildImprovements"] and brief["units"][0]["owner"] == "economy"
        assert any(
            name == "unit_details" and "work" in args["sections"] for name, args in decision_queries(brief, "economy")
        )


@run_async
async def test_new_worker_after_full_review_starts_a_worker_only_pass(tmp_path):
    async with managed(tmp_path) as m:
        m.host.view["diplomacy"] = {"knownCivilizations": [], "cityStates": []}
        await m.roles.synchronize_war(await m.host.tool("player_turn", {}))
        m.roles.economy_status.update(phase="finished", fullReview=True, cities=["city1", "city2"])
        m.roles.ready["economy"] = True
        m.economy.city_scope = None
        m.host.view["map"]["units"].append({"handle": "new-worker", "military": False, "worker": {}, "movement": "2/2"})
        await m.roles.synchronize_war(await m.host.tool("player_turn", {}))
        assert not m.roles.ready["economy"]
        assert not m.roles.economy_status["fullReview"]
        assert m.roles.economy_arguments() == {"cities": []}
        assert m.economy.city_scope == []


@run_async
async def test_compact_worker_capability_can_be_saved_in_a_completed_strategic_review(tmp_path):
    from civ_agent.review_store import ReviewStore
    from civ_agent.startup_context import prepare_startup
    from tests.test_strategic_review import assessment

    async with managed(tmp_path) as m:
        worker = m.host.view["map"]["units"][0]
        worker.update(name="Worker", military=False, worker={"currentJob": "Farm", "remainingTurns": 2})
        m.host.revision = "worker-review"
        prepared = await prepare_startup(m.gateway, m.economy)
        brief = prepared["reads"]["player_turn"]
        assert "worker" not in brief["units"][0]
        assert brief["units"][0]["canBuildImprovements"]
        review = ReviewStore(m.memory)
        review.prepare(
            "worker-review", {"turn": 0, "observation": m.economy.observation, "reads": {"player_turn": brief}}, {}, 60
        )
        review.complete("worker-review", assessment())
        completed = review.at_turn(0)
        assert completed is not None and completed["status"] == "COMPLETED"
        with m.memory.transaction() as db:
            baseline = m.memory._meta(db, "advisories")["reviewMaterialBaseline"]
        assert baseline["facts"]["units"][0] == ["unit1", "Worker", "Farm"]


@run_async
async def test_worker_handoff_reopens_roles_and_replayed_assignment_preserves_authority(tmp_path):
    wars = store(tmp_path)
    io = ServiceIO()
    service = SimpleNamespace(memory=wars.memory, io=io, records={}, _save=AsyncMock())
    roles = TurnRoles(service, {"epoch": "e1", "gameTurn": 4, "activationId": "main"})
    view = position(war=False)
    view["map"]["units"][2]["worker"] = {}
    job = SimpleNamespace(
        id="main",
        purpose="gameplay",
        epoch="e1",
        observation=view,
        memory={"gameTurn": 4},
        gameplay_ended=asyncio.Event(),
    )
    roles.register(job)
    try:
        await roles.synchronize_war(view)
        args = {"unit": "soldier", "civilian": "worker", "reason": "Protect road crew"}
        first = await roles.role_tool(job, "escort_assignment", args, "assign")
        assert roles.unit_owner(view["map"]["units"][2]) == "gameplay"
        assert not roles.ready["economy"]
        assert await roles.role_tool(job, "escort_assignment", args, "assign") == first
        roles.ready["economy"] = roles.ready["military"] = True
        await roles.role_tool(
            job, "escort_assignment", {"unit": "soldier", "release": True, "reason": "Road secure"}, "release"
        )
        assert roles.unit_owner(view["map"]["units"][2]) == "economy"
        assert not roles.ready["economy"] and not roles.ready["military"]
    finally:
        await io.close()
        wars.memory.engine.dispose()
