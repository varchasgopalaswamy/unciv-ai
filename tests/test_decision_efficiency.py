"""Fewer repeated inspections must retain facts, fences and confirmed receipts."""

import copy

import pytest

from civ_agent.military_completion import completion_problem
from civ_agent.military_schedule import MilitarySchedule
from civ_agent.observations import DetailQuery
from civ_agent.response_compaction import expand_response
from civ_agent.startup_context import decision_queries, prepare_startup
from civ_agent.state_updates import apply_player_turn
from tests.test_economy_optimization import managed
from tests.test_economy_roles import run_async
from tests.test_military_completion import hold


@run_async
async def test_sibling_refresh_retains_unchanged_details_but_fences_changed_decisions(tmp_path):
    async with managed(tmp_path) as m:
        await prepare_startup(m.gateway, m.economy)
        m.gateway.commit_startup_coordination(m.economy)
        queries = [DetailQuery(tool="city_details", arguments={"target": city}) for city in ("city1", "city2")]
        for index, query in enumerate(queries):
            await m.gateway.call(m.economy, query.tool, query.arguments, f"inspect-{index}")
        baseline = copy.deepcopy(m.economy.delivered_overview)
        m.host.view["cities"][1]["constructionQueue"] = ["Library"]
        await m.gateway.call(m.main, "player_command", {"kind": "DISMISS_NOTIFICATIONS", "targets": ["n1"]}, "other")
        # Repeated sibling changes must retain the original comparison baseline.
        await m.gateway.call(m.main, "player_command", {"kind": "DISMISS_NOTIFICATIONS", "targets": ["n2"]}, "other-2")
        assert m.economy.observation is None and m.economy.prior_role_observation is not None
        arguments = {"kind": "QUEUE_CONSTRUCTION", "target": "city2", "selection": "Granary"}
        result = expand_response(await m.gateway.call(m.economy, "player_command", arguments, "stale"))
        assert result["error"] == "OBSERVATION_REQUIRED" and not result["applied"]
        assert queries[0] in m.economy.details_read and queries[1] not in m.economy.details_read
        assert queries[0].model_dump() not in result["stateUpdate"]["invalidatedDetails"]
        assert queries[1].model_dump() in result["stateUpdate"]["invalidatedDetails"]
        assert apply_player_turn(baseline, result) == m.economy.delivered_overview
        assert m.economy.prior_role_observation is None
        assert expand_response(await m.gateway.call(m.economy, "player_command", arguments, "stale")) == result


@run_async
async def test_sibling_refresh_invalidates_native_previews_and_ownership_labels(tmp_path):
    async with managed(tmp_path) as m:
        await m.gateway.call(m.economy, "player_turn", {}, "start")
        native = DetailQuery(tool="movement_preview", arguments={"target": "unit1", "destination": {"x": 1, "y": 0}})
        m.economy.details_read.append(native)
        city = DetailQuery(tool="city_details", arguments={"target": "city2"})
        await m.gateway.call(m.economy, city.tool, city.arguments, "city")
        await m.gateway.call(m.main, "player_command", {"kind": "DISMISS_NOTIFICATIONS", "targets": ["n1"]}, "other")
        result = expand_response(await m.gateway.call(m.economy, "player_turn", {}, "refresh"))
        invalidated = result.get("invalidatedDetails", result.get("stateUpdate", {}).get("invalidatedDetails", []))
        assert native.model_dump() in invalidated
        assert city in m.economy.details_read
        await m.gateway.call(m.main, "city_override", {"city": "city2", "reason": "Emergency"}, "override")
        await m.gateway.call(m.economy, "player_turn", {}, "owner-refresh")
        assert city not in m.economy.details_read
        denied = await m.gateway.call(
            m.economy,
            "player_command",
            {"kind": "QUEUE_CONSTRUCTION", "target": "city2", "selection": "Granary"},
            "denied",
        )
        assert denied["error"] == "CITY_OWNED_BY_OTHER_ROLE"


@run_async
async def test_resume_delta_requires_a_model_delivered_baseline(tmp_path):
    async with managed(tmp_path) as m:
        await prepare_startup(m.gateway, m.main)
        m.gateway.commit_startup_coordination(m.main)
        assert m.gateway.resume_delivery(m.main) == "auto"
        baseline = copy.deepcopy(m.main.delivered_overview)
        m.gateway.invalidate_role_observation(m.main)
        resumed = await m.gateway.call(m.main, "player_turn", {"delivery": m.gateway.resume_delivery(m.main)}, "resume")
        assert apply_player_turn(baseline, resumed) == m.main.delivered_overview
        await m.gateway.call(m.main, "player_turn", {"delivery": "full"}, "host:review", source="host")
        assert m.gateway.resume_delivery(m.main) == "full"
        full = await m.gateway.call(m.main, "player_turn", {"delivery": "full"}, "resume-full")
        assert "stateId" in full and m.gateway.resume_delivery(m.main) == "auto"
        m.gateway.reset_coordination(m.main)
        assert m.gateway.resume_delivery(m.main) == "full"
        assert await m.gateway.call(m.main, "player_turn", {"delivery": "full"}, "resume-full") == full
        assert m.gateway.resume_delivery(m.main) == "full"  # An old receipt cannot restore a lost baseline.


@run_async
async def test_military_command_returns_review_without_forwarding_flag_or_repeating_action(tmp_path):
    async with managed(tmp_path) as m:
        unit = m.host.view["map"]["units"][0]
        unit["nearbyHostiles"] = [{"distance": 1}]
        m.host.view["diplomacy"] = {"knownCivilizations": [], "cityStates": []}
        job = await m.gateway.open("military", "military", "g1", m.host, {}, 0)
        m.roles.register(job)
        m.roles.military_schedule = MilitarySchedule(m.memory)
        m.roles.resume("military", "Threat requires review")
        await prepare_startup(m.gateway, job)
        m.gateway.commit_startup_coordination(job)
        baseline = copy.deepcopy(job.delivered_overview)
        received = []
        original = m.host.tool

        async def tool(name, arguments):
            if name == "player_command":
                received.append(copy.deepcopy(arguments))
            return await original(name, arguments)

        m.host.tool = tool
        arguments = {"kind": "SET_UNIT_ORDER", "target": unit["handle"], "selection": "Fortify", "reviewAfter": True}
        result = await m.gateway.call(job, "player_command", arguments, "hold-action")
        assert "error" not in result and len(received) == 1
        assert expand_response(result)["stateUpdate"]["kind"] == "delta"
        assert apply_player_turn(baseline, result) == job.delivered_overview
        assert "reviewAfter" not in received[0]
        review = expand_response(result)["militaryCompletion"]
        assert review["items"] and completion_problem(review, hold(review)) is None
        unit["health"] = 70
        assert await m.gateway.call(job, "player_command", arguments, "hold-action") == result
        assert len(received) == 1
        bundled = await m.gateway.call(
            job, "inspect_bundle", {"queries": [{"tool": "military_review", "arguments": {}}]}, "fresh-review"
        )
        current = expand_response(bundled)["results"][0]["result"]["militaryCompletion"]
        assert current["basis"] != review["basis"]
        assert current["items"][0]["health"] == 70


@run_async
async def test_review_after_permission_and_argument_errors_never_dispatch(tmp_path):
    async with managed(tmp_path) as m:
        action = {"kind": "DISMISS_NOTIFICATIONS", "targets": ["n1"], "reviewAfter": True}
        denied = await m.gateway.call(m.main, "player_command", action, "wrong-role")
        assert denied["error"] == "ACTIVE_MILITARY_ROLE_REQUIRED" and not denied["applied"]
        invalid = await m.gateway.call(m.main, "player_command", {**action, "reviewAfter": "yes"}, "bad-flag")
        assert invalid["error"] == "INVALID_COMMAND_ARGUMENTS" and not invalid["applied"]
        forbidden = await m.gateway.call(
            m.economy, "inspect_bundle", {"queries": [{"tool": "military_review", "arguments": {}}]}, "wrong-bundle"
        )
        assert forbidden["error"] == "TOOL_NOT_PERMITTED"


@run_async
async def test_review_refresh_failure_preserves_confirmed_action_and_replayed_receipt(tmp_path):
    async with managed(tmp_path) as m:
        job = await m.gateway.open("military", "military", "g1", m.host, {}, 0)
        m.roles.register(job)
        m.roles.resume("military", "Review")
        await m.gateway.call(job, "player_turn", {}, "start")
        original, executed = m.host.tool, []

        async def tool(name, arguments):
            if name == "player_turn" and executed:
                raise TimeoutError("Refresh unavailable")
            if name == "player_command":
                executed.append(copy.deepcopy(arguments))
            return await original(name, arguments)

        m.host.tool = tool
        arguments = {"kind": "SET_UNIT_ORDER", "target": "unit1", "selection": "Fortify", "reviewAfter": True}
        result = await m.gateway.call(job, "player_command", arguments, "action")
        assert result["stateUpdate"]["kind"] == "unavailable" and "militaryCompletion" not in result
        assert len(executed) == 1
        assert await m.gateway.call(job, "player_command", arguments, "action") == result
        assert len(executed) == 1 and job.observation is None and job.prior_role_observation is None


@pytest.mark.parametrize(
    "trigger", [{"constructionQueue": []}, {"turnsToStarvation": 2}, {"citizens": {"unassignedPopulation": 1}}]
)
def test_immediate_city_preloads_are_bounded_and_keep_existing_construction_options(trigger):
    city = {"handle": "city1", "owner": "economy", "constructionQueue": ["Library"], **trigger}
    cities = [{**city, "handle": f"city{i}"} for i in range(5)]
    cities[-1]["constructionChoice"] = {"availableSinceChoice": ["University"]}
    queries = decision_queries({"cities": cities}, "economy")
    assert [args["target"] for tool, args in queries if tool == "city_details"] == ["city0", "city1"]
    assert ("construction_details", {"target": "city4", "names": ["University"]}) in queries
    assert not decision_queries({"cities": [{**city, "owner": "gameplay"}]}, "economy")
    assert not decision_queries({"cities": cities}, "military")


@run_async
async def test_city_preload_follows_scope_and_seeds_read_baselines(tmp_path):
    async with managed(tmp_path) as m:
        prepared = await prepare_startup(m.gateway, m.economy)
        detail = next(d for d in prepared["decisionDetails"] if d["query"]["tool"] == "city_details")
        assert detail["query"]["arguments"] == {"target": "city2", "sections": ["citizens", "production"]}
        m.gateway.commit_startup_coordination(m.economy)
        repeated = await m.gateway.call(m.economy, "city_details", detail["query"]["arguments"], "repeat")
        assert repeated["detailUpdate"]["kind"] == "unchanged"
        m.roles.economy_status.update(cities=[], fullReview=False)
        worker_pass = await prepare_startup(m.gateway, m.economy, call_prefix="worker-start:")
        assert not any(d["query"]["tool"] == "city_details" for d in worker_pass["decisionDetails"])
