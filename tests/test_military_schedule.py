"""Military planning cadence, immediate execution and bounded early-review triage."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import request_body, request_id
from civ_agent.economy_roles import TurnRoles
from civ_agent.military_schedule import MilitarySchedule, military_facts
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from tests.domain_fixtures import run_async


def fixture(tmp_path):
    memory = CivilizationState(tmp_path, "Rome")
    memory.bind("g1")
    schedule = MilitarySchedule(memory, 5)
    view: JsonObject = {
        "epoch": "g1",
        "turn": 4,
        "phase": "ACTIVE",
        "revision": "r1",
        "cities": [{"handle": "city1", "health": 200}],
        "diplomacy": {"knownCivilizations": [{"civilizationId": "Greece", "atWar": False}]},
        "economy": {"unitSupply": {"productionPenaltyPercent": 0}},
        "map": {
            "units": [
                {
                    "handle": "unit1",
                    "name": "Warrior",
                    "military": True,
                    "health": 100,
                    "movement": "2/2",
                    "idle": False,
                    "position": {"x": 1, "y": 1},
                    "combat": {"order": {"action": "Fortify"}},
                }
            ],
            "tiles": [],
        },
    }
    state: JsonObject = {"episodes": {}, "escorts": {}, "operations": {}, "messages": []}
    return schedule, view, state


def test_first_and_periodic_planning_with_targeted_execution_on_an_independent_clock(tmp_path):
    schedule, view, state = fixture(tmp_path)
    first = schedule.assess(view, 4, state)
    assert first["fullReview"] and first["reasons"]
    assert schedule.complete(view, 4, state)
    assert not schedule.assess(view, 5, state)["reasons"]
    view["map"]["units"][0]["idle"] = True
    targeted = schedule.assess(view, 6, state)
    assert not targeted["fullReview"] and targeted["reasons"] == ["Combat unit needs orders: unit1"]
    assert schedule.complete(view, 6, state, full=False)
    assert not schedule.assess(view, 6, state)["reasons"]
    assert schedule.assess(view, 7, state)["nextReviewTurn"] == 9
    assert schedule.assess(view, 9, state)["fullReview"]
    assert MilitarySchedule(schedule.memory, 5).assess(view, 9, state)["fullReview"]


def test_destination_completion_and_hostile_contact_wake_execution_without_fog_leaks(tmp_path):
    schedule, view, state = fixture(tmp_path)
    unit = view["map"]["units"][0]
    unit["movementOrder"] = {"destination": {"x": 3, "y": 2}}
    schedule.complete(view, 4, state)
    del unit["movementOrder"]
    assert "Military movement completed or was interrupted: unit1" in schedule.assess(view, 5, state)["reasons"]
    schedule.complete(view, 5, state, full=False)
    contact = {"name": "Spearman", "civilization": "Barbarians", "military": True, "atWar": True, "health": 100}
    view["map"]["tiles"] = [{"position": {"x": 2, "y": 1}, "visible": False, "units": [contact]}]
    assert not military_facts(view, state)["material"]["contacts"]
    assert not schedule.assess(view, 6, state)["reasons"]
    view["map"]["tiles"][0]["visible"] = True
    assert "Visible hostile forces changed" in schedule.assess(view, 6, state)["reasons"]


def test_war_always_gets_execution_and_peace_keeps_the_same_schedule(tmp_path):
    schedule, view, state = fixture(tmp_path)
    schedule.complete(view, 4, state)
    view["diplomacy"]["knownCivilizations"][0]["atWar"] = True
    assert schedule.assess(view, 5, state)["fullReview"]
    schedule.complete(view, 5, state)
    sixth = schedule.assess(view, 6, state)
    assert sixth["atWar"] and not sixth["fullReview"]
    assert "Wartime execution requires review" in sixth["reasons"]
    schedule.complete(view, 6, state, full=False)
    assert not schedule.assess(view, 6, state)["reasons"]
    view["diplomacy"]["knownCivilizations"][0]["atWar"] = False
    assert schedule.assess(view, 7, state)["fullReview"]


def test_damage_city_loss_and_explicit_operation_deadlines_are_immediate(tmp_path):
    schedule, view, state = fixture(tmp_path)
    schedule.complete(view, 4, state)
    view["map"]["units"][0]["health"] = 60
    view["cities"][0]["health"] = 150
    reasons = schedule.assess(view, 5, state)["reasons"]
    assert "Combat units took damage" in reasons and "Owned city took damage" in reasons
    view["cities"] = []
    assert "Owned city lost" in schedule.assess(view, 5, state)["reasons"]
    state["operations"]["frontier"] = {"id": "frontier", "reviewTurn": 6}
    assert "Planned military review due: frontier" in schedule.assess(view, 6, state)["reasons"]
    schedule.complete(view, 6, state)
    assert not schedule.assess(view, 7, state)["reasons"]


def test_renewed_native_bombardment_does_not_wait_for_planning_or_a_diplomatic_war(tmp_path):
    schedule, view, state = fixture(tmp_path)
    view["cities"][0]["attacks"] = [{"destination": {"x": 2, "y": 1}}]
    schedule.complete(view, 4, state)
    fifth = schedule.assess(view, 5, state)
    assert not fifth["atWar"] and not fifth["fullReview"]
    assert "Native attacks require tactical review" in fifth["reasons"]
    schedule.complete(view, 5, state, full=False)
    assert not schedule.assess(view, 5, state)["reasons"]
    assert "Native attacks require tactical review" in schedule.assess(view, 6, state)["reasons"]


def test_freshness_and_checkpoint_restore_preserve_cadence_and_baselines(tmp_path):
    schedule, view, state = fixture(tmp_path / "memory")
    observed = copy.deepcopy(view)
    view["map"]["units"][0]["health"] = 90
    assert not schedule.complete(view, 4, state, observed)
    assert schedule.complete(view, 4, state, view)
    checkpoint = tmp_path / "boundary.sqlite3"
    digest = schedule.memory.backup(checkpoint)
    schedule.complete(view, 8, state)
    schedule.memory.restore(checkpoint, digest, "g2")
    assert schedule.assess(view, 5, state)["nextReviewTurn"] == 9
    view["epoch"] = "g2"
    assert schedule.complete(view, 5, state, full=False)
    assert schedule.assess(view, 5, state)["lastFullReviewTurn"] == 4


def changed_context(tmp_path):
    schedule, view, state = fixture(tmp_path)
    schedule.complete(view, 4, state)
    view["militaryReadiness"] = {"visibleBorderForces": [{"civilization": "Greece", "name": "Crossbowman"}]}
    status = schedule.assess(view, 5, state)
    assert status["materialChanged"] and not status["fullReview"] and not status["reasons"]
    return schedule, view, state, status


def test_decisions_only_selects_planning_and_unchanged_wait_is_not_reclassified(tmp_path):
    schedule, view, state, status = changed_context(tmp_path)
    evidence = schedule.triage_input(view, 5, state, status)
    assert evidence is not None
    body = request_body(evidence, DecisionsSettings())
    assert body["questions"][0]["name"] == "military_review"
    assert "not whether to declare war" in body["questions"][0]["instructions"]
    assert not schedule.triage_result(evidence, {"answers": [{"name": "military_review", "choice": "wait"}]})
    assert schedule.triage_input(view, 6, state, schedule.assess(view, 6, state)) is None
    assert MilitarySchedule(schedule.memory, 5).triage_input(view, 6, state, status) is None
    assert schedule.assess(view, 9, state)["fullReview"]


def test_uncertain_request_reconciles_original_evidence_and_changed_context_has_new_identity(tmp_path):
    schedule, view, state, status = changed_context(tmp_path)
    evidence = schedule.triage_input(view, 5, state, status)
    schedule.triage_result(evidence, {"answers": [], "fallback": "completion_unknown"})
    recovered = schedule.triage_input(view, 6, state, status)
    assert recovered == evidence and request_id(recovered) == request_id(evidence)
    view["economy"]["unitSupply"]["productionPenaltyPercent"] = -40
    other = schedule.triage_input(view, 6, state, status)
    assert other is not None and request_id(other) != request_id(evidence)


def test_a_stale_or_superseded_classifier_response_cannot_request_a_review(tmp_path):
    schedule, view, state, status = changed_context(tmp_path)
    evidence = schedule.triage_input(view, 5, state, status)
    result = {"answers": [{"name": "military_review", "choice": "review"}]}
    view["militaryReadiness"]["visibleBorderForces"] = []
    assert not schedule.triage_result(evidence, result, view, state)
    schedule.complete(view, 6, state)
    assert not schedule.triage_result(evidence, result)


def test_own_movement_and_observation_clocks_do_not_reclassify_unchanged_material(tmp_path):
    schedule, view, state = fixture(tmp_path)
    view["militaryReadiness"] = {
        "visibleBorderForces": [{"civilization": "Greece", "name": "Archer", "observedTurn": 4}]
    }
    view["map"]["tiles"] = [
        {
            "visible": True,
            "position": {"x": 1, "y": 1},
            "units": [{"civilization": "Rome", "name": "Warrior", "military": True, "health": 100}],
        }
    ]
    schedule.complete(view, 4, state)
    view["map"]["units"][0]["position"] = {"x": 2, "y": 1}
    view["map"]["tiles"][0]["position"] = {"x": 2, "y": 1}
    view["militaryReadiness"]["visibleBorderForces"][0]["observedTurn"] = 5
    status = schedule.assess(view, 5, state)
    assert not status["materialChanged"] and not status["reasons"]
    assert schedule.triage_input(view, 5, state, status) is None


def test_visible_capture_opportunities_and_new_recruitment_can_request_early_planning(tmp_path):
    schedule, view, state = fixture(tmp_path)
    schedule.complete(view, 4, state)
    view["map"]["tiles"] = [
        {
            "visible": True,
            "position": {"x": 3, "y": 1},
            "units": [
                {"civilization": "Greece", "name": "Settler", "military": False, "capture": {"unitIfKept": "Worker"}}
            ],
        }
    ]
    view["militaryReadiness"] = {"recruitment": [{"city": "city1", "units": ["Cannon"]}]}
    status = schedule.assess(view, 5, state)
    assert status["materialChanged"] and not status["reasons"]
    evidence = schedule.triage_input(view, 5, state, status)
    assert evidence is not None
    current = evidence["militaryReview"]["current"]
    assert current["captureTargets"][0]["name"] == "Settler"
    assert current["recruitment"][0]["units"] == ["Cannon"]


@pytest.mark.parametrize("outcome", ["review", "wait", "unavailable", "stale"])
@run_async
async def test_coordinator_applies_triage_only_to_current_optional_planning(tmp_path, outcome):
    schedule, view, state, _ = changed_context(tmp_path / "memory")
    view.update(turn=5, revision="r2")
    latest = copy.deepcopy(view)
    if outcome == "stale":
        latest.update(revision="r3")
        latest["militaryReadiness"]["visibleBorderForces"] = []
    answer = {
        "answers": [{"name": "military_review", "choice": "review" if outcome in {"review", "stale"} else "wait"}]
    }
    if outcome == "unavailable":
        answer = {"answers": [], "fallback": "budget_exhausted"}
    advisories = SimpleNamespace(evaluate=AsyncMock(return_value=answer))
    io = ServiceIO()
    service = SimpleNamespace(
        memory=schedule.memory,
        io=io,
        military_interval=5,
        records={},
        _save=AsyncMock(),
        gateway=SimpleNamespace(advisories=advisories),
    )
    roles = TurnRoles(service, {"epoch": "g1", "gameTurn": 5, "activationId": "main"})
    roles.register(
        SimpleNamespace(
            id="main",
            purpose="gameplay",
            memory={"gameTurn": 5},
            gameplay_ended=asyncio.Event(),
            connection=SimpleNamespace(tool=AsyncMock(return_value=latest)),
        )
    )
    try:
        await roles.initialize_military(view)
        advisories.evaluate.assert_awaited_once()
        assert roles.ready["military"] is (outcome != "review")
        assert roles.military_status["fullReview"] is (outcome == "review")
        assert roles.military_schedule is not None
        assert roles.military_schedule.saved()["lastFullReviewTurn"] == 4
    finally:
        await io.close()


@run_async
async def test_main_request_starts_a_dormant_peacetime_manager_once(tmp_path):
    schedule, view, _ = fixture(tmp_path / "memory")
    io = ServiceIO()
    service = SimpleNamespace(memory=schedule.memory, io=io, records={}, _save=AsyncMock())
    roles = TurnRoles(service, {"epoch": "g1", "gameTurn": 4, "activationId": "main"})
    main = SimpleNamespace(id="main", purpose="gameplay", epoch="g1", memory={"gameTurn": 4}, observation=view)
    roles.register(main)
    roles.start_military = Mock(return_value=object())
    request = {"to": "military", "text": "Scout the northern approach and assess staging options."}
    try:
        assert roles.ready["military"] and "military" not in roles.jobs
        result = await roles.role_tool(main, "role_message", request, "recon")
        assert "error" not in result
        assert not roles.ready["military"] and roles.military_requested
        roles.start_military.assert_called_once()
        again = await roles.role_tool(main, "role_message", request, "recon")
        assert again == result and len(roles.war_state["messages"]) == 1
        roles.start_military.assert_called_once()
    finally:
        await io.close()
