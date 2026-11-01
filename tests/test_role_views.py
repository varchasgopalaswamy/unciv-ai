"""Role projections preserve decisions, authority, exact history and recovery."""

import asyncio
import copy
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from civ_agent.civilization_state import CivilizationState
from civ_agent.economy_roles import TurnRoles
from civ_agent.game_commands import command_arguments
from civ_agent.memory_store import RecordStore, select_context
from civ_agent.player_prompts import job_instructions
from civ_agent.review_delivery import factual_baseline, review_brief
from civ_agent.review_store import ReviewStore
from civ_agent.role_view import label_commands, role_overview
from civ_agent.startup_context import decision_queries, prepare_startup, startup_prompt
from civ_agent.state_updates import apply_overview_update
from civ_agent.tool_schemas import tool_model
from tests.test_economy_optimization import managed
from tests.test_military_roles import position
from tests.test_strategic_review import assessment


def test_projection_preserves_constraints_pending_threats_and_uncertainty():
    raw = {
        "pending": [{"kind": "choose_research"}],
        "alerts": [{"handle": "alert1"}],
        "economy": {"happiness": -4, "availableGold": 0, "unitSupply": {"overSupply": 3}},
        "tacticalReview": {"visibleHostiles": [], "limits": "Unseen defenders remain unknown"},
        "militaryReadiness": {"visibleBorderForces": [{"civilizationId": "Rome"}], "foreignObservationLimits": "Fog"},
        "diplomacy": [{"civilizationId": "Rome", "atWar": False, "bilateral": {"defensivePactTurns": 2}}],
        "newSurface": {"unrecognized": True},
    }
    for role in ("main", "economy", "military"):
        view = role_overview(raw, role)
        for key in ("pending", "alerts", "economy", "tacticalReview", "diplomacy"):
            assert view[key] == raw[key]
        assert "newSurface" in view["roleBrief"]["omitted"]
        assert view["militaryReadiness"] == raw["militaryReadiness"]
        assert view["roleBrief"]["fullRead"]["arguments"] == {"view": "full"}
        assert role_overview(raw, "full")["newSurface"] == raw["newSurface"]


def test_wonder_catalogue_preloads_available_names_and_keeps_details_retrievable():
    raw = {
        "wonderOpportunities": [
            {"name": "Great Library", "benefits": ["Free technology"], "cities": [{"city": "city1", "turns": 10}]},
            {"name": "Oracle", "cities": [{"city": "city1", "queued": True}]},
            {"name": "Colossus", "cities": [], "unavailableCities": [{"reasons": ["Requires coast"]}]},
        ]
    }
    brief = role_overview(raw, "economy")
    assert brief["availableWonders"] == ["Great Library"]
    assert "wonderOpportunities" not in brief
    assert any("construction_details" in item for item in brief["roleBrief"]["omitted"])
    assert role_overview(raw, "full")["wonderOpportunities"] == raw["wonderOpportunities"]


def test_escort_and_city_ownership_share_execution_authority():
    roles = TurnRoles(SimpleNamespace(records={}), {"epoch": "e1", "gameTurn": 4, "activationId": "test"})
    raw = position()
    roles.war_state["escorts"] = {"soldier": {"civilian": "worker"}}
    roles.overrides["city1"] = "Emergency"
    full = {"units": raw["map"]["units"], "cities": raw["cities"]}
    main = role_overview(full, "main", roles=roles)
    military = role_overview(full, "military", roles=roles, purpose="military")
    assert {u["handle"] for u in main["units"]} == {"worker", "soldier"}
    assert {u["handle"] for u in military["units"]} == {"worker", "scout"}
    assert main["cities"][0]["owner"] == "gameplay"
    commands = [
        {"command": {"kind": "MOVE_UNIT", "target": unit, "destination": {"x": 2, "y": 0}}}
        for unit in ("soldier", "scout", "worker")
    ] + [{"command": {"kind": "SET_CITY_FOCUS", "target": "city1", "selection": "FoodFocus"}}]
    for role in ("gameplay", "economy", "military"):
        labeled = label_commands({"options": commands}, roles, role, raw)
        job = SimpleNamespace(purpose=role, observation=raw)
        for offer in labeled["options"]:
            assert command_arguments(offer["command"]) == offer["command"]
            assert (offer["commandAccess"]["mode"] == "direct") == (
                roles.ownership_problem(job, offer["command"]) is None
            )
    roles.overrides.clear()
    offer = label_commands({"offer": commands[-1]}, roles, "gameplay", raw)["offer"]
    assert offer["commandAccess"]["tool"] == "economy_request"
    assert offer["commandAccess"]["emergencyOverride"]["tool"] == "city_override"
    roles.war_state["escorts"].clear()
    assert roles.unit_owner(raw["map"]["units"][0]) == "military"


@pytest.mark.parametrize("purpose,view", [("gameplay", "main"), ("economy", "economy"), ("military", "military")])
def test_startup_refresh_actions_switches_and_receipt_replay_keep_selected_view(tmp_path, purpose, view):
    async def run():
        async with managed(tmp_path) as m:
            job = m.main if purpose == "gameplay" else m.economy
            if purpose == "military":
                job = await m.gateway.open("military", purpose, "g1", m.host, {}, 0)
                m.roles.register(job)
            prepared = await prepare_startup(m.gateway, job)
            delivered = prepared["reads"]["player_turn"]
            assert delivered["roleBrief"]["view"] == view
            assert "strategicAdvice" not in prepared
            assert "END_TURN" not in startup_prompt(prepared, purpose, "Play").instructions
            assert "endTurn" not in delivered
            assert all(
                q[1].get("target") not in {u["handle"] for u in delivered.get("units", []) if u.get("owner") != purpose}
                for q in decision_queries(delivered, purpose)
            )
            catalogue = await m.gateway.tools(job)
            schema = next(t["outputSchema"] for t in catalogue if t["name"] == "player_turn")
            Draft202012Validator(schema).validate(delivered)
            m.gateway.commit_startup_coordination(job)
            m.host.revision = "external"
            refreshed = await m.gateway.call(job, "player_turn", {}, "refresh")
            assert job.overview_view == view
            assert job.delivered_overview["roleBrief"]["view"] == view
            if "stateUpdate" in refreshed:
                assert apply_overview_update(delivered, refreshed["stateUpdate"]) == job.delivered_overview
            full = await m.gateway.call(job, "player_turn", {"view": "full"}, "expand")
            assert "roleBrief" not in full and "militaryReadiness" not in full.get("roleBrief", {})
            saved = copy.deepcopy(job.delivered_overview)
            assert await m.gateway.call(job, "player_turn", {"view": "full"}, "expand") == full
            assert job.delivered_overview == saved
            focused = await m.gateway.call(job, "player_turn", {"view": view}, "focus")
            assert focused["roleBrief"]["view"] == view
            assert "stateUpdate" not in focused
            if purpose == "economy":
                command = {"kind": "SET_CITY_FOCUS", "target": "city2", "selection": "FoodFocus"}
                result = await m.gateway.call(job, "player_command", command, "focus-city")
                assert "error" not in result
                assert job.delivered_overview["roleBrief"]["view"] == view
                after = copy.deepcopy(job.delivered_overview)
                assert await m.gateway.call(job, "player_command", command, "focus-city") == result
                assert job.delivered_overview == after

    asyncio.run(run())


def test_peace_and_transfer_withhold_review_without_retiring_authored_plans(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            raw = copy.deepcopy(m.host.view)
            raw["turn"] = 0
            raw["diplomacy"] = {"knownCivilizations": [{"civilizationId": "Greece", "atWar": True}]}
            review = assessment()
            store = ReviewStore(m.memory)
            store.prepare("review", {"turn": 0, "observation": raw}, {}, 60)
            store.complete("review", review)
            await m.gateway.close(m.main, True)
            m.host.view["diplomacy"] = copy.deepcopy(raw["diplomacy"])
            m.main = await m.gateway.open("next", "gameplay", "g1", m.host, {}, 1)
            m.roles.register(m.main)
            before = await prepare_startup(m.gateway, m.main)
            assert before["reads"]["player_turn"]["strategicReview"]["status"] == "CURRENT_BASELINE"
            m.gateway.commit_startup_coordination(m.main)
            checkpoint = tmp_path / "before-peace.json"
            digest = m.memory.backup(checkpoint)
            m.host.view["diplomacy"]["knownCivilizations"][0].update(atWar=False, peaceTreatyTurns=10)
            m.host.view["cities"].pop()
            m.host.revision = "peace-and-transfer"
            await m.gateway.call(m.main, "player_turn", {}, "peace")
            notice = m.main.delivered_overview["strategicReview"]
            assert notice["status"] == "RECONSIDER" and "assessment" not in notice
            assert {r["surface"] for r in notice["firstChange"]["changedFacts"]} >= {"relations", "cities"}
            history = await m.gateway.call(m.main, "history_read", {"turn": 0, "includeReview": True}, "review-read")
            assert history["review"] == review
            assert "review" not in await m.gateway.call(m.main, "history_read", {"turn": 0}, "actions-only")
            with m.memory.transaction() as db:
                assert RecordStore(db).all(keys=[m.strategy])[m.strategy]["body"]["state"] == "active"
            # Restart retains the reconsideration; rewind restores its prior boundary.
            reopened = CivilizationState(m.memory.root, "Rome")
            try:
                with reopened.transaction() as db:
                    assert reopened._meta(db, "reviewReconsiderations")["0"]["changedFacts"]
            finally:
                reopened.engine.dispose()
            m.memory.restore(checkpoint, digest, "restored")
            with m.memory.transaction() as db:
                assert not m.memory._meta(db, "reviewReconsiderations", {})
            assert m.memory.history(0, include_review=True)["review"] == review

    asyncio.run(run())


def test_city_override_and_sibling_action_refresh_the_same_projection(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            for job in (m.main, m.economy):
                await prepare_startup(m.gateway, job)
                m.gateway.commit_startup_coordination(job)
            before = copy.deepcopy(m.main.delivered_overview)
            m.host.view["cities"][1]["constructionQueue"] = ["Warrior"]
            changed = await m.gateway.call(
                m.economy,
                "player_command",
                {"kind": "QUEUE_CONSTRUCTION", "target": "city2", "selection": "Warrior"},
                "sibling-build",
            )
            assert "error" not in changed
            reply = await m.gateway.call(m.main, "player_turn", {}, "sibling-refresh")
            current = apply_overview_update(before, reply["stateUpdate"]) if "stateUpdate" in reply else reply
            assert current["roleBrief"]["view"] == "main"
            assert current["cities"][1]["constructionQueue"] == ["Warrior"]
            override = await m.gateway.call(
                m.main, "city_override", {"city": "city2", "reason": "Emergency defense"}, "override"
            )
            assert "error" not in override
            after = apply_overview_update(current, override["stateUpdate"])
            assert after["cities"][1]["owner"] == "gameplay"
            await m.gateway.call(m.economy, "player_turn", {}, "ownership-refresh")
            assert m.economy.delivered_overview["cities"][1]["owner"] == "gameplay"
            saved = copy.deepcopy(m.main.delivered_overview)
            assert (
                await m.gateway.call(
                    m.main, "city_override", {"city": "city2", "reason": "Emergency defense"}, "override"
                )
                == override
            )
            assert m.main.delivered_overview == saved

    asyncio.run(run())


def test_old_reviews_and_treaty_countdowns_do_not_invent_baseline_facts():
    raw = position(war=False)
    raw["diplomacy"]["knownCivilizations"][0]["bilateral"] = {"peaceTreatyTurns": 5}
    advice = {"asOfTurn": 4, **assessment(), "_contextFacts": factual_baseline(raw)}
    raw["diplomacy"]["knownCivilizations"][0]["bilateral"]["peaceTreatyTurns"] = 4
    assert (review_brief(advice, raw, "gameplay") or {})["status"] == "CURRENT_BASELINE"
    raw["diplomacy"]["knownCivilizations"][0]["bilateral"]["peaceTreatyTurns"] = 0
    assert (review_brief(advice, raw, "gameplay") or {})["status"] == "RECONSIDER"
    advice.pop("_contextFacts")
    assert (review_brief(advice, raw, "gameplay") or {})["status"] == "BASELINE_UNAVAILABLE"


def test_role_selection_keeps_unrelated_pending_contributions_retrievable():
    records = {
        "old": {
            "category": "contribution",
            "body": {
                "type": "note",
                "text": "Diplomatic followup",
                "subjects": ["Egypt"],
                "state": "active",
                "basis": {"purpose": "communication"},
            },
        },
        "current": {
            "category": "note",
            "body": {"type": "note", "text": "City growth", "subjects": ["city1"], "state": "active"},
        },
    }
    before = copy.deepcopy(records)
    selected, omitted = select_context(
        {"records": records, "purpose": "economy", "gameTurn": 10, "contextTopics": ["city1"], "events": []}, 10000
    )
    assert set(selected) == {"current"} and omitted == 1
    assert records == before


def test_managed_recovery_guidance_is_consistent():
    for role in ("gameplay", "economy", "military"):
        text = job_instructions(role)
        assert "END_TURN" not in text and "stop if it happens again" not in text
        assert "finish decision" in text and "Reconcile uncertain execution before retrying" in text


def test_economy_only_advertises_permitted_war_goal_reads():
    schema = tool_model("war_goals", "economy").model_json_schema()
    Draft202012Validator(schema).validate({"section": "operations"})
    assert schema["properties"]["action"]["const"] == "read"
