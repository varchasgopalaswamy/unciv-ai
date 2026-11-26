"""Targeted reviews preserve player access, full-review cadence and request delivery."""

import asyncio
import copy
import json

import pytest
from jsonschema import Draft202012Validator

from civ_agent.coordination_updates import apply_coordination_update
from civ_agent.economy_schedule import EconomySchedule
from civ_agent.memory_store import RecordStore
from civ_agent.startup_context import prepare_startup, startup_prompt
from civ_agent.state_updates import apply_overview_update
from tests.domain_fixtures import create, observation, setup
from tests.domain_fixtures import (
    managed as managed,
)


@pytest.fixture
def two_cities(tmp_path):
    memory, view = setup(tmp_path), observation()
    second = copy.deepcopy(view["cities"][0])
    second.update(handle="city2", name="Antium")
    view["cities"].append(second)
    memory.start_job("seed-second", "economy", {}, game_turn=4)
    memory.observe_player(view, "seed-second")
    for city in ("city1", "city2"):
        create(memory, {"type": "city_plan", "city": city, "reasons": {"construction": "Keep Worker"}}, "seed-second")
    memory.finish_job("seed-second", True)
    schedule = EconomySchedule(memory, 5)
    schedule.complete(view, 4)
    yield schedule, view
    memory.engine.dispose()


def test_local_reviews_preserve_full_clock_and_other_city_baselines(two_cities):
    schedule, view = two_cities
    view["cities"][1]["development"]["unassignedPopulation"] = 1
    status = schedule.assess(view, 5, [])
    assert status["cities"] == ["city2"] and not status["fullReview"]
    # Resolving one city's problem must not acknowledge unrelated routine changes.
    observed = copy.deepcopy(view)
    view["cities"][0]["development"]["tiles"][0]["yields"] = [{"stat": "Food", "amount": "3"}]
    assert schedule.complete(view, 5, observed, cities=["city2"], full=False)
    status = schedule.assess(view, 5, [])
    assert not status["reasons"]
    assert status["pendingCities"] == ["city1"]
    assert status["lastFullReviewTurn"] == 4 and status["nextReviewTurn"] == 9
    # A further local review still does not postpone the full review.
    view["cities"][1]["development"]["unassignedPopulation"] = 0
    assert schedule.complete(view, 7, view, cities=["city2"], full=False)
    due = schedule.assess(view, 9, [])
    assert due["fullReview"] and due["cities"] == ["city1", "city2"]
    assert due["reasons"] == ["Periodic economy review due"]


def test_handled_plan_dates_do_not_require_rewriting_plans(two_cities):
    schedule, view = two_cities
    memory = schedule.memory
    memory.start_job("set-deadlines", "economy", {}, game_turn=4)
    memory.observe_player(view, "set-deadlines")
    with memory.transaction() as db:
        plans = RecordStore(db).all(categories={"city_plan"})
    keys = {r["body"]["city"]: key for key, r in plans.items()}
    memory.refresh_memory("set-deadlines", list(keys.values()))
    for city, date in (("city1", 5), ("city2", 7)):
        result = memory.patch("set-deadlines", [{"op": "amend", "id": keys[city], "fields": {"reviewTurn": date}}])
        assert result["changes"][0]["accepted"]
    memory.finish_job("set-deadlines", True)
    with memory.transaction() as db:
        before = RecordStore(db).all(categories={"city_plan"})
    assert schedule.assess(view, 5, [])["cities"] == ["city1"]
    assert schedule.complete(view, 5, view, cities=["city1"], full=False)
    assert not schedule.assess(view, 6, [])["reasons"]
    assert schedule.assess(view, 7, [])["cities"] == ["city2"]
    assert schedule.complete(view, 7, view, cities=["city2"], full=False)
    assert not schedule.assess(view, 8, [])["reasons"]
    with memory.transaction() as db:
        after = RecordStore(db).all(categories={"city_plan"})
    assert {k: r["version"] for k, r in before.items()} == {k: r["version"] for k, r in after.items()}
    assert after[keys["city1"]]["body"]["reviewTurn"] == 5
    assert after[keys["city2"]]["body"]["reviewTurn"] == 7


def test_local_completion_rejects_unseen_target_or_empire_changes(two_cities):
    schedule, view = two_cities
    observed = copy.deepcopy(view)
    view["cities"][1]["development"]["unassignedPopulation"] = 1
    assert not schedule.complete(view, 5, observed, cities=["city2"], full=False)
    view["cities"][1]["development"]["unassignedPopulation"] = 0
    view["economy"]["happiness"] = -1
    assert not schedule.complete(view, 5, observed, cities=["city2"], full=False)
    assert schedule.assess(view, 5, [])["fullReview"]


def test_unresolved_conditions_are_acknowledged_once_per_turn(two_cities):
    schedule, view = two_cities
    view["cities"][1]["development"]["starving"] = True
    assert schedule.assess(view, 5, [])["cities"] == ["city2"]
    assert schedule.complete(view, 5, view, cities=["city2"], full=False)
    assert not schedule.assess(view, 5, [])["reasons"]
    assert schedule.assess(view, 6, [])["cities"] == ["city2"]


def test_partial_review_timestamps_rewind_with_the_checkpoint(two_cities, tmp_path):
    schedule, view = two_cities
    checkpoint = tmp_path / "review.json"
    checksum = schedule.memory.backup(checkpoint)
    assert schedule.complete(view, 7, view, cities=["city2"], full=False)
    schedule.memory.restore(checkpoint, checksum, "g2")
    view["epoch"] = "g2"
    with schedule.memory.transaction() as db:
        saved = schedule.memory._meta(db, "economy:schedule")
    assert saved["cityReviewTurns"] == {"city1": 4, "city2": 4}
    assert saved["lastFullReviewTurn"] == 4


def test_policy_change_only_wakes_when_native_economic_options_change(two_cities):
    schedule, view = two_cities
    view["culture"] = {"branches": [{"adopted": ["Tradition"]}]}
    assert not schedule.assess(view, 5, [])["reasons"]
    assert "Adopted policies changed" in schedule.assess(view, 5, [])["pendingReasons"]
    view["cities"][1]["development"]["constructions"] = [
        {"name": "Worker", "productionCost": 40, "productionPerTurn": 5}
    ]
    assert schedule.assess(view, 5, [])["cities"] == ["city2"]
    assert schedule.complete(view, 5, view, cities=["city2"], full=False)
    # The old global policy baseline must not make ordinary later production
    # changes look like another new policy for this already reviewed city.
    view["cities"][1]["development"]["constructions"][0]["productionPerTurn"] = 6
    assert not schedule.assess(view, 6, [])["reasons"]


def test_routine_changes_batch_without_delaying_an_urgent_city_review(two_cities):
    schedule, view = two_cities
    passes = []
    for turn in range(5, 10):
        view.update(turn=turn, revision=f"revision-{turn}")
        view["cities"][0]["development"]["tiles"][0]["yields"] = [{"stat": "Food", "amount": str(turn)}]
        view["culture"] = {"branches": [{"adopted": ["Tradition", f"Policy {turn}"]}]}
        view["cities"][0]["development"]["foodStored"] = turn
        if turn == 6:
            view["cities"][1]["development"]["unassignedPopulation"] = 1
        status = schedule.assess(view, turn, [])
        if status["reasons"]:
            passes.append((turn, status["cities"], status["fullReview"]))
            view["cities"][1]["development"]["unassignedPopulation"] = 0
            assert schedule.complete(view, turn, view, cities=status["cities"], full=status["fullReview"])
    assert passes == [(6, ["city2"], False), (9, ["city1", "city2"], True)]


def test_handled_deferred_deadline_waits_for_a_new_event_or_date(two_cities):
    schedule, view = two_cities
    request = {"id": "later", "city": "city2", "status": "deferred", "reviewTurn": 5}
    assert schedule.assess(view, 5, [request])["cities"] == ["city2"]
    request["reviewedTurn"] = 5
    assert schedule.complete(view, 5, view, cities=["city2"], full=False)
    assert not schedule.assess(view, 6, [request])["reasons"]
    request["reviewTurn"] = 7
    assert schedule.assess(view, 7, [request])["cities"] == ["city2"]


def test_legacy_schedule_metadata_retains_its_full_review_deadline(two_cities):
    schedule, view = two_cities
    with schedule.memory.transaction() as db:
        saved = schedule.memory._meta(db, "economy:schedule")
        saved.pop("lastFullReviewTurn")
        saved.pop("cityReviewTurns")
        for city in saved["baseline"]["cities"].values():
            city.pop("constructionTerms")
            city.pop("discoveryState")
        schedule.memory._set(db, "economy:schedule", saved)
    assert not schedule.assess(view, 5, [])["reasons"]
    assert schedule.complete(view, 7, view, cities=["city2"], full=False)
    status = schedule.assess(view, 8, [])
    assert status["lastFullReviewTurn"] == 4 and status["nextReviewTurn"] == 9
    assert status["cityReviewTurns"] == {"city1": 4, "city2": 7}


def test_targeted_preload_keeps_constraints_and_player_access(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            prepared = await prepare_startup(m.gateway, m.economy)
            view = prepared["reads"]["player_turn"]
            assert [city["handle"] for city in view["cities"]] == ["city2"]
            assert {city["handle"] for city in view["cityScope"]["ownedCities"]} == {"city1", "city2"}
            assert view["economy"] == m.main.delivered_overview["economy"]
            records = json.loads(prepared["memoryContext"].split("\n", 1)[1])["records"]
            assert {r["id"] for r in records} >= {m.plans[1], m.strategy}
            assert m.plans[0] not in {r["id"] for r in records}
            assert not m.memory.delivered_records(m.economy.id, [m.plans[0]])
            prompt = startup_prompt(prepared, "economy", "Review")
            assert "cityScope" in prompt.user
            tools = await m.gateway.tools(m.economy)
            schema = next(t["inputSchema"] for t in tools if t["name"] == "player_turn")
            Draft202012Validator(schema).validate({"cities": ["city2"]})
            detail = await m.gateway.call(m.economy, "city_details", {"target": "city1"}, "other-city")
            assert detail["available"] and detail["data"]["entity"]["handle"] == "city1"
            all_cities = await m.gateway.call(m.economy, "player_turn", {}, "full-read")
            assert len(all_cities["cities"]) == 2 and "cityScope" not in all_cities
            # Review scope is attention guidance, never an action restriction.
            action = await m.gateway.call(
                m.economy,
                "player_command",
                {"kind": "SET_CITY_FOCUS", "target": "city1", "selection": "FoodFocus"},
                "other-city-action",
            )
            assert "error" not in action
            assert m.roles.economy_status["cities"] == ["city1", "city2"]

    asyncio.run(run())


def test_unchanged_targeted_review_does_not_write_a_plan(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            await prepare_startup(m.gateway, m.economy)
            with m.memory.transaction() as db:
                before = RecordStore(db).all(categories={"city_plan"})
            finished = await m.gateway.call(m.economy, "finish_role", {}, "finish")
            assert finished["roleFinished"]
            with m.memory.transaction() as db:
                after = RecordStore(db).all(categories={"city_plan"})
                saved = m.memory._meta(db, "economy:schedule")
            assert {key: r["version"] for key, r in before.items()} == {key: r["version"] for key, r in after.items()}
            assert saved["lastFullReviewTurn"] == 0 and saved["cityReviewTurns"] == {"city1": 0, "city2": 0}
            assert not m.roles.resumes["economy"].is_set()

    asyncio.run(run())


def test_requests_join_the_active_pass_and_pending_duplicates_coalesce(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            await prepare_startup(m.gateway, m.economy)
            arguments = {"city": "city1", "text": "Consider a Scout after the Worker", "priority": "routine"}
            result = await m.gateway.call(m.main, "economy_request", arguments, "request")
            duplicate = await m.gateway.call(m.main, "economy_request", arguments, "duplicate")
            assert duplicate["alreadyPending"] and duplicate["request"]["id"] == result["request"]["id"]
            assert len(m.roles.requests) == 1 and m.roles.economy_status["cities"] == ["city1", "city2"]
            assert not m.roles.resumes["economy"].is_set()
            stale = await m.gateway.call(m.economy, "finish_role", {}, "too-early")
            assert stale["error"] == "ECONOMY_OBSERVATION_CHANGED"
            arguments = stale["recovery"]["reads"][0]["arguments"]
            current = await m.gateway.call(m.economy, "player_turn", arguments, "scope-update")
            assert len(current["cities"]) == 2 and len(current["economyCoordination"]["requests"]) == 1
            result = await m.gateway.call(m.economy, "finish_role", {}, "finished")
            assert result["roleFinished"] and not m.roles.resumes["economy"].is_set()

    asyncio.run(run())


def test_sender_sees_completed_work_before_reopening_economy(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            await prepare_startup(m.gateway, m.economy)
            initial = await m.gateway.call(
                m.main,
                "economy_request",
                {"city": "city2", "text": "Consider a Warrior", "priority": "routine"},
                "initial",
            )
            baseline = m.main.delivered_overview
            m.host.view["cities"][1]["constructionQueue"] = ["Warrior"]
            result = await m.gateway.call(
                m.economy,
                "player_command",
                {"kind": "QUEUE_CONSTRUCTION", "target": "city2", "selection": "Warrior"},
                "build",
            )
            assert "error" not in result
            done = await m.gateway.call(
                m.economy,
                "finish_role",
                {"requests": [{"id": initial["request"]["id"], "status": "completed", "reason": "Warrior is queued"}]},
                "finish",
            )
            assert done["roleFinished"]
            stale = await m.gateway.call(
                m.main,
                "economy_request",
                {"city": "city2", "text": "Queue a Warrior now", "priority": "urgent"},
                "stale-request",
            )
            assert stale["error"] == "ECONOMY_CONTEXT_CHANGED" and not stale["applied"]
            current = apply_overview_update(baseline, stale["stateUpdate"])
            assert current["cities"][1]["constructionQueue"] == ["Warrior"]
            coordination = apply_coordination_update(initial["economyCoordination"], stale)
            assert coordination is not None
            assert coordination["requests"][0]["status"] == "completed"
            assert len(m.roles.requests) == 1 and m.roles.ready["economy"]
            # A deliberately new urgent priority still reopens economy immediately.
            fresh = await m.gateway.call(
                m.main,
                "economy_request",
                {"city": "city2", "text": "Reconsider an Archer instead", "priority": "urgent"},
                "new-priority",
            )
            assert "error" not in fresh and not m.roles.ready["economy"]
            assert m.roles.economy_arguments() == {"cities": ["city2"]}

    asyncio.run(run())


def test_raising_pending_priority_preserves_request_id_and_cannot_be_downgraded(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            await prepare_startup(m.gateway, m.economy)
            arguments = {"city": "city2", "text": "Reconsider the next build", "priority": "routine"}
            first = await m.gateway.call(m.main, "economy_request", arguments, "routine")
            urgent = await m.gateway.call(m.main, "economy_request", {**arguments, "priority": "urgent"}, "urgent")
            repeated = await m.gateway.call(m.main, "economy_request", arguments, "routine-again")
            assert first["request"]["id"] == urgent["request"]["id"] == repeated["request"]["id"]
            assert urgent["alreadyPending"] and urgent["request"]["priority"] == "urgent"
            assert repeated["request"]["priority"] == "urgent" and len(m.roles.requests) == 1
            assert not m.roles.resumes["economy"].is_set()
            finish = await m.gateway.call(m.economy, "finish_role", {}, "missing-disposition")
            assert finish["error"] == "URGENT_REQUEST_DISPOSITION_REQUIRED"

    asyncio.run(run())
