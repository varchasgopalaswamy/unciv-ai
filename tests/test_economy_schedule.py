"""Event routing is deterministic; dormant roles never start a model or block handoff."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_agent.economy_roles import TurnRoles
from civ_agent.economy_schedule import EconomySchedule
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from tests.domain_fixtures import create, setup
from tests.domain_fixtures import (
    observation as observation,
)


@pytest.fixture
def scheduled(tmp_path):
    memory = setup(tmp_path)
    memory.start_job("economy", "economy", {}, game_turn=4)
    create(
        memory,
        {
            "type": "city_plan",
            "city": "city1",
            "reasons": {"construction": "Build a Worker"},
            "purchaseTargets": ["Granary"],
        },
        "economy",
    )
    memory.finish_job("economy", True)
    schedule, view = EconomySchedule(memory, 5), observation()
    schedule.complete(view, 4)
    yield schedule, view
    memory.engine.dispose()


def test_ordinary_progress_and_transport_changes_do_not_wake(scheduled):
    schedule, view = scheduled
    view.update(turn=5, revision="r2")
    view["economy"].update(treasury=105, availableGold=105, happiness=3, goldPerTurn=6)
    view["cities"][0]["development"].update(foodStored=10, turnsToGrowth=3)
    view["cities"][0]["constructionEstimates"] = {"Worker": {"turns": 2, "productionStored": 20}}
    status = schedule.assess(view, 5, [])
    assert status["lastReviewTurn"] == status["lastFullReviewTurn"] == 4
    assert status["nextReviewTurn"] == 9
    assert not status["reasons"] and not status["pendingReasons"] and not status["cities"]
    assert not EconomySchedule(schedule.memory, 5).assess(view, 5, [])["reasons"]
    assert schedule.assess(view, 9, [])["reasons"] == ["Periodic economy review due"]


def test_workers_wake_without_resetting_city_clock_and_holds_are_acknowledged(scheduled, tmp_path):
    schedule, view = scheduled
    worker = {"handle": "worker1", "worker": {"currentJob": None}, "movement": "2/2", "health": 100}
    view["map"] = {"units": [worker]}
    status = schedule.assess(view, 5, [])
    assert status["reasons"] == ["New worker under Economy control: worker1"]
    assert status["cities"] == [] and not status["fullReview"]
    assert schedule.complete(view, 5, view, cities=[], full=False)
    assert not schedule.assess(view, 5, [])["reasons"]
    status = schedule.assess(view, 6, [])
    assert status["reasons"] == ["Worker needs orders: worker1"]
    assert status["lastFullReviewTurn"] == 4 and status["nextReviewTurn"] == 9
    checkpoint = tmp_path / "workers.json"
    checksum = schedule.memory.backup(checkpoint)
    schedule.complete(view, 6, view, cities=[], full=False)
    schedule.memory.restore(checkpoint, checksum, "g2")
    view["epoch"] = "g2"
    assert not EconomySchedule(schedule.memory, 5).assess(view, 5, [])["reasons"]
    assert EconomySchedule(schedule.memory, 5).assess(view, 6, [])["reasons"] == status["reasons"]


def test_exhausted_new_worker_waits_for_orders_but_danger_still_wakes(scheduled):
    schedule, view = scheduled
    worker: JsonObject = {"handle": "captured", "worker": {}, "movement": "0/2", "health": 100}
    view["map"] = {"units": [worker]}
    assert not schedule.assess(view, 5, [])["reasons"]
    assert not schedule.assess(view, 5, [])["reasons"]
    worker["movement"] = "2/2"
    assert schedule.assess(view, 6, [])["reasons"] == ["New worker under Economy control: captured"]
    worker["movement"] = "0/2"
    worker["nearbyHostiles"] = [{"distance": 1}]
    assert schedule.assess(view, 5, [])["reasons"]
    worker["nearbyHostiles"] = []
    worker["health"] = 77
    assert schedule.assess(view, 5, [])["reasons"]


def test_worker_jobs_routes_threats_and_unseen_changes(scheduled):
    schedule, view = scheduled
    worker: JsonObject = {"handle": "w1", "worker": {"currentJob": "Farm", "remainingTurns": 4}, "movement": "2/2"}
    view["map"] = {"units": [worker]}
    schedule.complete(view, 4)
    worker["worker"]["remainingTurns"] = 3
    assert not schedule.assess(view, 5, [])["reasons"]
    observed = copy.deepcopy(view)
    worker["worker"]["currentJob"] = None
    assert schedule.assess(view, 5, [])["reasons"]
    assert not schedule.complete(view, 5, observed, cities=[], full=False)
    worker["movementOrder"] = {"destination": {"x": 3, "y": 1}}
    schedule.complete(view, 5, cities=[], full=False)
    worker["position"] = {"x": 2, "y": 1}
    assert not schedule.assess(view, 6, [])["reasons"]
    worker["movementOrder"] = None
    worker["movement"] = "0/2"
    assert "completed or interrupted" in schedule.assess(view, 6, [])["reasons"][0]
    schedule.complete(view, 6, cities=[], full=False)
    worker["nearbyHostiles"] = [{"name": "Barbarian Warrior", "distance": 1}]
    assert "visible threats changed" in schedule.assess(view, 6, [])["reasons"][0]


def test_workers_before_cities_and_explicit_escort_handoff(scheduled):
    schedule, view = scheduled
    view["cities"] = []
    worker = {"handle": "w1", "worker": {}, "movement": "2/2"}
    view["map"] = {"units": [worker]}
    assert schedule.assess(view, 5, [])["reasons"] == ["New worker under Economy control: w1"]
    schedule.complete(view, 5, cities=[], full=False)
    with schedule.memory.transaction() as db:
        schedule.memory._set(db, "war:state", {"escorts": {"soldier": {"civilian": "w1"}}})
    assert schedule.assess(view, 5, [])["reasons"] == ["Worker lost or reassigned"]
    schedule.complete(view, 5, cities=[], full=False)
    assert not schedule.assess(view, 6, [])["reasons"]
    with schedule.memory.transaction() as db:
        schedule.memory._set(db, "war:state", {"escorts": {}})
    assert schedule.assess(view, 6, [])["reasons"] == ["New worker under Economy control: w1"]


@pytest.mark.parametrize(
    ("change", "reason", "immediate"),
    [
        (lambda v: v["cities"].append({"handle": "city2", "name": "Antium"}), "Newly owned city", True),
        (lambda v: v["cities"][0]["development"].update(population=3), "Population", False),
        (lambda v: v["cities"][0]["development"].update(unassignedPopulation=1), "Unassigned citizens", True),
        (lambda v: v["cities"][0]["development"].update(starving=True), "Food deficit", True),
        (lambda v: v["cities"][0].update(constructionQueue=[]), "Construction", True),
        (lambda v: v["cities"][0]["development"]["builtBuildings"].append("Granary"), "Construction", False),
        (lambda v: v["cities"][0]["constructionChoice"].update(chosenTurn=5), "Construction", False),
        (
            lambda v: v["cities"][0]["development"].update(constructions=[{"index": 0, "productionPerTurn": 0}]),
            "Stalled construction",
            True,
        ),
        (lambda v: v["cities"][0]["constructionOptions"].append("Library"), "Economic options", True),
        (lambda v: v["cities"][0]["management"]["specialists"][0].update(slots=2), "Economic options", True),
        (lambda v: v["cities"][0]["development"]["tiles"][0].update(blockaded=True), "Workable tiles", False),
        (lambda v: v["cities"][0]["purchases"]["constructions"][0].update(available=True), "Planned purchase", True),
        (lambda v: v["economy"].update(happiness=-1), "Happiness", True),
        (lambda v: v["economy"].update(goldPerTurn=-5), "Gold income", True),
        (lambda v: v["economy"].update(resources=[{"name": "Iron", "available": 2}]), "Resource access", True),
        (lambda v: v["economy"].update(goldenAge={"turnsRemaining": 10}), "Golden age", False),
        (lambda v: v.update(research={"technologies": [{"name": "Writing", "researched": True}]}), "Technology", False),
        (
            lambda v: v.update(diplomacy={"knownCivilizations": [{"civilizationId": "Greece", "atWar": True}]}),
            "War started",
            True,
        ),
    ],
)
def test_urgent_events_wake_and_routine_changes_wait_for_review(scheduled, change, reason, immediate):
    schedule, view = scheduled
    change(view)
    status = schedule.assess(view, 5, [])
    assert bool(status["reasons"]) is immediate
    assert any(reason in r for r in status["reasons" if immediate else "pendingReasons"])
    if not immediate:
        assert schedule.assess(view, 9, [])["fullReview"]
    schedule.complete(view, 5)
    assert not schedule.assess(view, 5, [])["reasons"]


def test_routine_requests_wait_for_scheduled_review_and_explicit_dates_are_honored(scheduled):
    schedule, view = scheduled
    request = {"id": "one", "status": "pending", "priority": "routine", "turn": 5}
    assert not schedule.assess(view, 5, [request])["reasons"]
    assert not schedule.assess(view, 6, [request])["reasons"]
    assert schedule.assess(view, 6, [request])["pendingReasons"] == ["Routine economic request: one"]
    assert schedule.assess(view, 9, [request])["fullReview"]
    request.update(priority="urgent")
    assert schedule.assess(view, 5, [request])["reasons"]
    request.update(status="deferred")
    assert not schedule.assess(view, 6, [request])["reasons"]
    request.update(reviewTurn=7)
    assert not schedule.assess(view, 6, [request])["reasons"]
    assert schedule.assess(view, 7, [request])["reasons"]


def test_schedule_rewinds_with_memory_and_epoch_changes_alone_do_not_wake(scheduled, tmp_path):
    schedule, view = scheduled
    path = tmp_path / "review-checkpoint.json"
    checksum = schedule.memory.backup(path)
    schedule.complete(view, 8)
    schedule.memory.restore(path, checksum, "g2")
    view["epoch"] = "g2"
    status = schedule.assess(view, 5, [])
    assert status["lastFullReviewTurn"] == status["lastReviewTurn"] == 4
    assert status["nextReviewTurn"] == 9 and not status["reasons"]


def test_completion_cannot_acknowledge_economic_changes_the_model_never_saw(scheduled):
    schedule, view = scheduled
    observed = copy.deepcopy(view)
    view["cities"][0]["development"]["population"] = 3
    assert not schedule.complete(view, 5, observed)
    assert schedule.assess(view, 5, [])["lastReviewTurn"] == 4
    assert any("Population" in r for r in schedule.assess(view, 5, [])["pendingReasons"])
    assert schedule.complete(view, 5, view)
    assert not schedule.assess(view, 6, [])["reasons"]


def test_no_city_waits_for_founding_and_missing_plan_wakes(scheduled):
    schedule, view = scheduled
    empty = {**view, "cities": []}
    assert not schedule.assess(empty, 12, [])["reasons"]
    assert schedule.assess(empty, 12, [])["nextReviewTurn"] is None
    view["cities"][0]["handle"] = "unplanned"
    assert any("Missing city plan" in r for r in schedule.assess(view, 5, [])["reasons"])


def test_dormant_turn_wakes_for_urgent_request_and_coalesces_events(scheduled, tmp_path):
    async def run():
        schedule, view = scheduled
        view["turn"] = 5
        io = ServiceIO()
        service = SimpleNamespace(
            memory=schedule.memory, io=io, economy_interval=5, records={}, state_dir=tmp_path, _save=AsyncMock()
        )
        roles = TurnRoles(service, {"epoch": "g1", "gameTurn": 5, "activationId": "main"})
        main = SimpleNamespace(purpose="gameplay", epoch="g1", memory={"gameTurn": 5}, observation=view)
        roles.register(main)
        try:
            await roles.initialize_economy(view)
            assert roles.ready["economy"] and not roles.economy_start.is_set()
            await roles.tool(main, "economy_request", {"text": "Build a Library", "priority": "urgent"})
            assert not roles.ready["economy"] and roles.economy_start.is_set()
            assert roles.economy_status["phase"] == "running"
            # Concurrent observations cannot start a second review.
            await roles.synchronize_economy({**view, "revision": "r3"})
            assert roles.economy_requested
        finally:
            await io.close()

    asyncio.run(run())
