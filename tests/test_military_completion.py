"""Completion acknowledges current choices without dictating tactics or outcomes."""

import copy

import pytest
from pydantic import ValidationError

from civ_agent.military_completion import completion_problem, completion_review, review_page
from civ_agent.military_schedule import MilitarySchedule
from civ_agent.tool_schemas import tool_model
from tests.domain_fixtures import managed, run_async
from tests.test_military_schedule import fixture


def hold(review):
    return {
        "basis": review["basis"],
        "decisions": [
            {
                "targets": [i["target"] for i in review["items"]],
                "reason": "Hold to protect the city; accept the visible risk.",
            }
        ],
    }


def test_pagination_and_holds_cover_every_current_target_and_exclude_escorts(tmp_path):
    _, view, state = fixture(tmp_path)
    template = view["map"]["units"][0]
    template["nearbyHostiles"] = [{"distance": 1}]
    view["map"]["units"] = [{**copy.deepcopy(template), "handle": f"u{i}"} for i in range(23)]
    view["cities"][0]["attacks"] = [{"destination": {"x": 3, "y": 1}}]
    state["escorts"] = {"u0": {"civilian": "worker"}}
    review = completion_review(view, state)
    first = review_page(review)
    second = review_page(review, first["nextCursor"])
    assert first["total"] == 23 and len(first["items"]) == 20 and len(second["items"]) == 3
    assert second["nextCursor"] is None and first["basis"] == second["basis"]
    assert "u0" not in {i["target"] for i in review["items"]}
    assert completion_problem(review, hold(first))
    assert completion_problem(review, hold(review)) is None
    duplicate = hold(review)
    duplicate["decisions"][0]["targets"].append("u1")
    assert completion_problem(review, duplicate)
    with pytest.raises(ValidationError):
        tool_model("finish_role", "military").model_validate(
            {"militaryReview": {"basis": review["basis"], "decisions": [{"targets": ["u1"], "reason": " "}]}}
        )


def test_capture_checks_use_visible_native_offers_and_never_infer_reach(tmp_path):
    _, view, state = fixture(tmp_path)
    unit = view["map"]["units"][0]
    tile = {
        "visible": True,
        "position": {"x": 2, "y": 1},
        "units": [{"military": False, "atWar": True, "capture": {"unitIfKept": "Worker"}}],
    }
    view["map"]["tiles"] = [tile]
    assert not completion_review(view, state)["items"]
    unit["moves"] = [{"destination": tile["position"]}]
    assert completion_review(view, state)["items"][0]["captureDestinations"] == [tile["position"]]
    tile["visible"] = False
    assert not completion_review(view, state)["items"]
    tile["visible"] = True
    tile["units"][0]["atWar"] = False
    assert not completion_review(view, state)["items"]


def test_changed_facts_and_rewind_require_reconsideration_but_successful_actions_need_no_repeat(tmp_path):
    schedule, view, state = fixture(tmp_path)
    unit = view["map"]["units"][0]
    unit["nearbyHostiles"] = [{"distance": 1}]
    original = completion_review(view, state)
    decision = hold(original)
    assert schedule.complete(view, 4, state, completion=decision)
    assert MilitarySchedule(schedule.memory).saved()["completionReview"] == decision
    checkpoint = tmp_path / "completion.json"
    digest = schedule.memory.backup(checkpoint)
    unit["health"] = 77
    changed = completion_review(view, state, schedule.saved())
    assert changed["observedProgress"]["unitsWithChangedHealth"] == 1
    assert completion_problem(changed, decision)
    unit["nearbyHostiles"] = []
    assert completion_problem(completion_review(view, state), None) is None
    schedule.memory.restore(checkpoint, digest, "g2")
    view["epoch"] = "g2"
    unit["health"] = 100
    unit["nearbyHostiles"] = [{"distance": 1}]
    assert completion_problem(completion_review(view, state), decision)


@run_async
async def test_gateway_checks_fresh_sibling_facts_accepts_risk_and_replays_finish_receipt(tmp_path):
    async with managed(tmp_path) as m:
        unit = m.host.view["map"]["units"][0]
        unit["nearbyHostiles"] = [{"distance": 1}]
        m.host.view["diplomacy"] = {"knownCivilizations": [], "cityStates": []}
        job = await m.gateway.open("military", "military", "g1", m.host, {}, 0)
        m.roles.register(job)
        m.roles.military_schedule = MilitarySchedule(m.memory)
        m.roles.resume("military", "Threat requires review")
        await m.gateway.call(job, "player_turn", {}, "start")
        first = await m.gateway.call(job, "finish_role", {}, "unchecked")
        assert first["error"] == "MILITARY_COMPLETION_REVIEW_REQUIRED" and not m.roles.ready["military"]
        check = await m.gateway.call(job, "military_review", {}, "check")
        review = check["militaryCompletion"]
        assert review["items"][0]["target"] == "unit1"
        unit["health"] = 77
        m.host.revision = "sibling-action"
        stale = await m.gateway.call(job, "finish_role", {"militaryReview": hold(review)}, "stale")
        assert stale["error"] == "MILITARY_COMPLETION_REVIEW_REQUIRED"
        fresh = await m.gateway.call(job, "military_review", {}, "fresh")
        arguments = {"militaryReview": hold(fresh["militaryCompletion"])}
        finished = await m.gateway.call(job, "finish_role", arguments, "finish")
        assert finished["roleFinished"] and m.roles.ready["military"]
        assert m.roles.military_schedule.saved()["completionReview"] == arguments["militaryReview"]
        m.roles.resume("military", "New request after completion")
        assert await m.gateway.call(job, "finish_role", arguments, "finish") == finished
        assert not m.roles.ready["military"]


@pytest.mark.parametrize("change", ["unrelated", "military", "coordination"])
@run_async
async def test_active_attention_preserves_completion_basis_without_granting_fresh_read(tmp_path, change):
    async with managed(tmp_path) as m:
        m.host.view["map"]["units"][0]["nearbyHostiles"] = [{"distance": 1}]
        m.host.view["diplomacy"] = {"knownCivilizations": [], "cityStates": []}
        job = await m.gateway.open("military", "military", "g1", m.host, {}, 0)
        m.roles.register(job)
        m.roles.military_schedule = MilitarySchedule(m.memory)
        m.roles.resume("military", "Initial review")
        await m.gateway.call(job, "player_turn", {}, "start")
        check = await m.gateway.call(job, "military_review", {}, "check")
        if change == "coordination":
            m.roles.war_state.setdefault("messages", []).append({"to": "military", "text": "Changed request"})
        m.roles.resume("military", "Another coordination message during the same pass")
        assert (m.roles.military_observation is None) is (change == "coordination")
        m.gateway.invalidate_role_observation(job)
        m.host.revision = "sibling-action"
        if change == "military":
            m.host.view["map"]["units"][0]["health"] = 77
        else:
            m.host.view.setdefault("economy", {})["treasury"] = 200
        result = await m.gateway.call(
            job, "finish_role", {"militaryReview": hold(check["militaryCompletion"])}, "finish"
        )
        assert bool(result.get("roleFinished")) is (change == "unrelated")
        assert m.roles.ready["military"] is (change == "unrelated")
        if change != "unrelated":
            assert result["error"] in {"MILITARY_COMPLETION_REVIEW_REQUIRED", "MILITARY_OBSERVATION_CHANGED"}
        assert job.observation is None and job.role_refresh_required
